"""Set up a new business on a deployed stack the way a real one is set up, and time every step.

One command runs it all, in this order: the checks of the deployed stack and its address, the
new workspace with its key and documents, its first question, the proof that it stays apart
from the others, the measurements against the deployed stack, and where the stack keeps its
data. Each step is timed on its own; the bootstrap and the deploy, which the operator runs
before, are timed from their CloudFormation events. A step only a person can do (handing over
the key, the page in a browser) stands in the plan as such: `--manual wait` stops there until
Enter and times it, the default `skip` records it as open and goes on.

Each step's output goes to `--out-dir`, outside the repository, with the account id replaced;
`run-<time>.json` and `run-<time>.md` there say what ran, for how long and how it ended. The
key `workspace add` prints is never written anywhere. The run stops at the first step that
fails; `--from` goes on from a step, `--only` runs single steps, `--list` prints the plan and
calls nothing. It deploys nothing and deletes nothing; the measurements create and delete their
own probe guardrails, as they do when run alone, and cost what they cost alone
(scripts/README.md).
"""

import argparse
import concurrent.futures
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.page import ASK_PATH, SECURITY_HEADERS
from infra.answer_models import REGIONS, answer_model
from scripts import utf8_output
from scripts.ask import API_KEY_ENVIRONMENT_VARIABLE, read_key
from scripts.aws import STACK_NAME, AwsProblem, client, explained, read_stack_outputs

REPOSITORY = Path(__file__).resolve().parents[1]

#: The regions whose function is its own address. Behind CloudFront the address checks
#: cannot pass: its addresses are edge locations worldwide.
RUN_REGIONS = tuple(region for region, models in REGIONS.items() if not models.behind_cloudfront)
DEFAULT_REGION = "eu-central-1"


def deployed_model_name(region: str) -> str:
    """The name of the region's answer model in its table, as `evaluate` and the probes take it."""
    return answer_model(REGIONS[region].answer_model_id, region).name


#: The answer function and its log group, as the stack names them.
FUNCTION_NAME = "andur-answer"
FUNCTION_LOG_GROUP = f"/aws/lambda/{FUNCTION_NAME}"

#: Where AWS lists the address ranges of its regions.
IP_RANGES_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"

HTTP_TIMEOUT_SECONDS = 10

#: What replaces the twelve-digit account id in every file the run writes.
ACCOUNT_PLACEHOLDER = "<account>"

#: The CloudFormation stack `cdk bootstrap` creates.
BOOTSTRAP_STACK = "CDKToolkit"

#: The stack outputs the steps and the scripts they run read; checked before anything is paid.
NEEDED_OUTPUTS = (
    "WebUrl",
    "ApiKeyParameterPath",
    "KnowledgeBaseId",
    "DataSourceId",
    "DocumentsBucketName",
    "BudgetTableName",
)

_DONE = ("CREATE_COMPLETE", "UPDATE_COMPLETE")

#: The first part of a model id that names an inference profile, which carries the call to
#: other regions.
PROFILE_PREFIXES = ("eu", "us", "apac", "jp", "au", "ca", "global")


class StepFailed(Exception):
    """A step ended in a way the run must not go on from."""


@dataclass
class Context:
    """What the steps share: the run's settings and what earlier steps found out."""

    region: str
    stack_name: str
    workspace: str
    other_workspace: str
    out_dir: Path
    expect_role: str | None
    clients: Callable[[str], Any]
    #: What the answer to the first question must contain, such as "68 Euro"; None: anything.
    fact: str | None = None
    #: The new workspace's document names, without their suffix: a section file begins with one.
    documents: frozenset[str] = frozenset()
    outputs: dict[str, str] = field(default_factory=dict)
    account: str | None = None
    started: datetime = field(default_factory=lambda: datetime.now(UTC))

    def client(self, service: str) -> Any:
        return self.clients(service)

    def output(self, name: str) -> str:
        """A stack output; read from the stack the first time one is needed."""
        if not self.outputs:
            self.outputs = read_stack_outputs(self.client("cloudformation"), self.stack_name)
        if name not in self.outputs:
            raise StepFailed(f"the stack has no output {name}")
        return self.outputs[name]


@dataclass(frozen=True)
class Step:
    id: str
    phase: str
    title: str
    #: A command line, run from the repository root with the shell's environment.
    command: tuple[str, ...] | None = None
    #: A check run in this process; returns one line for the summary, raises `StepFailed`.
    check: Callable[[Context], str] | None = None
    #: What a person does by hand; the step runs nothing.
    manual: str | None = None
    #: Reads the command's output; returns one line for the summary, raises `StepFailed`.
    reads: Callable[[str, Context], str] | None = None
    #: The exit code that means the step did what it should.
    exit_code: int = 0
    #: The command prints a key on stdout: it is thrown away, never written to the log.
    secret_stdout: bool = False
    #: Extra environment for the command, made when it starts and never logged.
    environment: Callable[[Context], Mapping[str, str]] | None = None
    #: Steps next to each other with the same group run at once, lane by lane: each lane's
    #: steps one after the other. Only measurements that share no AWS quota share a group.
    group: str | None = None
    lane: str | None = None

    @property
    def kind(self) -> str:
        return "manual" if self.manual else "check" if self.check else "command"


@dataclass
class Result:
    number: int
    id: str
    phase: str
    title: str
    kind: str
    #: passed or failed; a step by hand: done or open.
    status: str
    started: str
    seconds: float
    summary: str
    exit_code: int | None = None
    log: str | None = None
    group: str | None = None


# --- Checks run in this process ----------------------------------------------------------


def role_of(arn: str) -> str:
    """The role of an assumed-role ARN, or the user part of any other."""
    resource = arn.split(":", 5)[-1]
    if resource.startswith("assumed-role/"):
        return resource.split("/")[1]
    return resource


def check_identity(ctx: Context) -> str:
    """Who the run signs in as. The account id is kept, to be replaced in the logs."""
    with explained("Reading the signed-in identity"):
        identity = ctx.client("sts").get_caller_identity()
    ctx.account = identity["Account"]
    role = role_of(identity["Arn"])
    if ctx.expect_role and ctx.expect_role != role:
        raise StepFailed(f"signed in as {role}, not as {ctx.expect_role}: the wrong profile?")
    return f"signed in as {role}"


def check_stack(ctx: Context) -> str:
    """The stack is deployed and finished, and has the outputs the steps need."""
    with explained(f"Reading stack {ctx.stack_name}"):
        stacks = ctx.client("cloudformation").describe_stacks(StackName=ctx.stack_name)["Stacks"]
    status = stacks[0]["StackStatus"]
    if status not in _DONE:
        raise StepFailed(f"{ctx.stack_name} is {status}, not deployed and finished")
    ctx.outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    missing = [name for name in NEEDED_OUTPUTS if name not in ctx.outputs]
    if missing:
        raise StepFailed(f"{ctx.stack_name} has no output {', '.join(missing)}")
    return f"{ctx.stack_name} {status} in {ctx.region}"


def last_deploy(
    events: Iterable[Mapping[str, Any]], stack_name: str
) -> tuple[str, float, datetime] | None:
    """The stack's last create or update, how many seconds it took and when it finished, from
    its events (newest first): from the stack's own IN_PROGRESS to its COMPLETE. None while none
    has finished. A deploy that changed nothing leaves no event, so the finish says which one."""
    own = [e for e in events if e["LogicalResourceId"] == stack_name]
    done = next((e for e in own if e["ResourceStatus"] in _DONE), None)
    if done is None:
        return None
    kind = done["ResourceStatus"].split("_")[0]
    started = next(
        (
            e
            for e in own
            if e["ResourceStatus"] == f"{kind}_IN_PROGRESS" and e["Timestamp"] <= done["Timestamp"]
        ),
        None,
    )
    if started is None:
        return None
    seconds = (done["Timestamp"] - started["Timestamp"]).total_seconds()
    return kind.lower(), seconds, done["Timestamp"]


def stack_events(cloudformation: Any, stack_name: str) -> list[dict[str, Any]]:
    """Every event of a stack, newest first."""
    events: list[dict[str, Any]] = []
    pages = cloudformation.get_paginator("describe_stack_events").paginate(StackName=stack_name)
    for page in pages:
        events.extend(page["StackEvents"])
    return events


def check_deploy_time(ctx: Context) -> str:
    """How long the operator's bootstrap and deploy took, by CloudFormation's own events."""
    cloudformation = ctx.client("cloudformation")
    parts = []
    for name in (BOOTSTRAP_STACK, ctx.stack_name):
        with explained(f"Reading the events of {name}"):
            found = last_deploy(stack_events(cloudformation, name), name)
        if found is None:
            parts.append(f"{name}: none finished")
        else:
            kind, seconds, finished = found
            at = finished.astimezone().strftime("%d.%m.%Y %H:%M")
            parts.append(f"{name} {kind} {format_seconds(seconds)}, finished {at}")
    return "; ".join(parts)


def host_of(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]


def address_regions(addresses: Iterable[str], ranges: Mapping[str, Any]) -> dict[str, set[str]]:
    """For each address, the regions of the AWS ranges it lies in; empty when in none."""
    networks = [(ipaddress.ip_network(p["ip_prefix"]), p["region"]) for p in ranges["prefixes"]]
    networks += [
        (ipaddress.ip_network(p["ipv6_prefix"]), p["region"]) for p in ranges["ipv6_prefixes"]
    ]
    found: dict[str, set[str]] = {}
    for text in addresses:
        address = ipaddress.ip_address(text)
        found[text] = {
            region
            for network, region in networks
            if network.version == address.version and address in network
        }
    return found


def resolve_host(host: str) -> list[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, 443)})


def fetch_ip_ranges() -> dict[str, Any]:
    with urllib.request.urlopen(IP_RANGES_URL, timeout=30) as response:
        return json.load(response)


def check_address_region(
    ctx: Context,
    resolve: Callable[[str], list[str]] = resolve_host,
    ranges: Callable[[], Mapping[str, Any]] = fetch_ip_ranges,
) -> str:
    """Every address the web address resolves to lies in the stack's region and in no other,
    by AWS's own list of its ranges (ip-ranges.json)."""
    host = host_of(ctx.output("WebUrl"))
    addresses = resolve(host)
    if not addresses:
        raise StepFailed(f"{host} resolves to no address")
    regions = address_regions(addresses, ranges())
    elsewhere = {a: sorted(r) for a, r in regions.items() if r != {ctx.region}}
    if elsewhere:
        raise StepFailed(f"not only in {ctx.region}: {elsewhere}")
    return f"{len(addresses)} address(es), each in {ctx.region} only"


def http_request(
    scheme: str,
    host: str,
    method: str,
    path: str,
    body: bytes | None = None,
    address: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP request that follows no redirect: status, headers, body. With `address`, plain
    HTTP goes to that one address, with `host` as its Host header."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, timeout=HTTP_TIMEOUT_SECONDS
        )
    else:
        connection = http.client.HTTPConnection(address or host, 80, timeout=HTTP_TIMEOUT_SECONDS)
        headers["Host"] = host
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def missing_headers(headers: Mapping[str, str]) -> list[str]:
    """The security headers a response lacks, or carries with another value."""
    lower = {name.lower(): value for name, value in headers.items()}
    return [name for name, value in SECURITY_HEADERS.items() if lower.get(name.lower()) != value]


def plain_http(
    host: str, addresses: Sequence[str], send: Callable[..., tuple[int, dict[str, str], bytes]]
) -> str:
    """Plain HTTP to every address at once, port 80: each must refuse, time out, or send the
    request on to https. One after the other, 12 addresses took 2 minutes on 07.10.2026. An
    address this computer has no route to (no IPv6 here, say) is named as not checked."""

    def probe(address: str) -> str:
        try:
            status, headers, _ = send("http", host, "GET", "/", address=address)
        except (TimeoutError, ConnectionRefusedError, ConnectionResetError):
            return "closed"  # nothing answers unencrypted
        except http.client.HTTPException as error:
            raise StepFailed(f"plain http on {address} answers, not as HTTP: {error!r}") from error
        except OSError:
            return "not checked"
        location = {k.lower(): v for k, v in headers.items()}.get("location", "")
        if status not in (301, 302, 307, 308) or not location.startswith("https://"):
            raise StepFailed(f"plain http on {address} answers {status}: a key could travel open")
        return f"{status} to https"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(addresses))) as pool:
        answers = list(pool.map(probe, addresses))
    summary = (
        f"http: {answers.count('closed')} of {len(addresses)} address(es) without a connection"
    )
    redirects = sorted({a for a in answers if a.endswith("to https")})
    if redirects:
        summary += f", sent on: {', '.join(redirects)}"
    if unchecked := answers.count("not checked"):
        summary += f", {unchecked} not checked (no route from here)"
    return summary


def check_https_only(
    ctx: Context,
    send: Callable[..., tuple[int, dict[str, str], bytes]] = http_request,
    resolve: Callable[[str], list[str]] = resolve_host,
) -> str:
    """Plain HTTP serves nothing on any address; HTTPS serves the page and refuses a question
    without a key, both with the security headers."""
    host = host_of(ctx.output("WebUrl"))
    addresses = resolve(host)
    if not addresses:
        raise StepFailed(f"{host} resolves to no address")
    plain = plain_http(host, addresses, send)
    status, headers, page = send("https", host, "GET", "/")
    if status != 200 or b"<html" not in page.lower():
        raise StepFailed(f"https GET / gave {status}, not the page")
    if missing := missing_headers(headers):
        raise StepFailed(f"the page lacks {missing}")
    # A whole question, so that only the missing key can turn it away; nothing is counted.
    question = json.dumps({"question": "Ping?", "workspace": ctx.workspace}).encode("utf-8")
    status, headers, _ = send("https", host, "POST", ASK_PATH, question)
    if status != 401:
        raise StepFailed(f"a question without a key gave {status}, not 401")
    if missing := missing_headers(headers):
        raise StepFailed(f"the 401 lacks {missing}")
    return f"{plain}; https: page 200, no key 401, security headers on both"


def workspace_key_environment(ctx: Context) -> dict[str, str]:
    """The new workspace's key, to ask in another workspace with it."""
    key = read_key(ctx.client("ssm"), ctx.output("ApiKeyParameterPath"), ctx.workspace)
    return {API_KEY_ENVIRONMENT_VARIABLE: key}


_REGION_IN_ARN = re.compile(r"^arn:aws[\w-]*:[\w-]+:([a-z0-9-]*):")


def arn_region(arn: str) -> str:
    match = _REGION_IN_ARN.match(arn)
    return match.group(1) if match else "?"


def residency(ctx: Context) -> list[tuple[str, str]]:
    """Each place the deployed stack keeps or processes data, with the region AWS reports for
    it; ("model invocation log", "off") when Bedrock keeps no log of the model calls."""
    places: list[tuple[str, str]] = []
    with explained("Reading where the documents bucket is"):
        location = ctx.client("s3").get_bucket_location(Bucket=ctx.output("DocumentsBucketName"))
    places.append(("documents bucket", location.get("LocationConstraint") or "us-east-1"))
    with explained("Reading the knowledge base"):
        base = ctx.client("bedrock-agent").get_knowledge_base(
            knowledgeBaseId=ctx.output("KnowledgeBaseId")
        )["knowledgeBase"]
    vectors = base["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    places.append(("embedding model", arn_region(vectors["embeddingModelArn"])))
    index = base["storageConfiguration"].get("s3VectorsConfiguration", {}).get("indexArn", "")
    places.append(("vector index", arn_region(index)))
    with explained(f"Reading the configuration of {FUNCTION_NAME}"):
        function = ctx.client("lambda").get_function_configuration(FunctionName=FUNCTION_NAME)
    places.append(("answer function", arn_region(function["FunctionArn"])))
    variables = function.get("Environment", {}).get("Variables", {})
    region = arn_region(function["FunctionArn"])
    for variable, name in (("MODEL_ID", "answer model"), ("TOPIC_MODEL_ID", "topic model")):
        model = variables.get(variable)
        if not model:
            places.append((name, f"no {variable}"))
            continue
        prefix = model.split(".")[0]
        # Without a profile the function calls the model in its own region.
        places.append(
            (f"{name} {model}", f"profile {prefix}" if prefix in PROFILE_PREFIXES else region)
        )
    guardrails = (
        ("GUARDRAIL_ID", "answer guardrail"),
        ("EXCERPT_GUARDRAIL_ID", "excerpt guardrail"),
    )
    for variable, name in guardrails:
        with explained(f"Reading the {name}"):
            guardrail = ctx.client("bedrock").get_guardrail(guardrailIdentifier=variables[variable])
        profile = guardrail.get("crossRegionDetails", {}).get("guardrailProfileId")
        places.append(
            (name, f"profile {profile}" if profile else arn_region(guardrail["guardrailArn"]))
        )
    with explained("Reading the budget table"):
        table = ctx.client("dynamodb").describe_table(TableName=ctx.output("BudgetTableName"))
    replicas = [r["RegionName"] for r in table["Table"].get("Replicas", [])]
    places.append(("budget table", ", ".join(replicas) or arn_region(table["Table"]["TableArn"])))
    with explained("Reading the answer function's log group"):
        groups = ctx.client("logs").describe_log_groups(logGroupNamePrefix=FUNCTION_LOG_GROUP)
    for group in groups["logGroups"]:
        places.append((f"log {group['logGroupName']}", arn_region(group["arn"])))
    with explained("Reading the model invocation log setting"):
        logging = ctx.client("bedrock").get_model_invocation_logging_configuration()
    places.append(("model invocation log", "on" if logging.get("loggingConfig") else "off"))
    return places


def check_residency(ctx: Context) -> str:
    """Where the deployed stack keeps and processes data, as AWS reports it, written to
    residency.md; fails on anything outside the region, a profile, or a log of model calls."""
    places = residency(ctx)
    lines = ["| Place | Region |", "|---|---|", *(f"| {p} | {r} |" for p, r in places)]
    (ctx.out_dir / "residency.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    fine = {ctx.region, "off"}
    outside = [f"{place}: {where}" for place, where in places if where not in fine]
    if outside:
        raise StepFailed(f"not only in {ctx.region}: {'; '.join(outside)}")
    return f"{len(places)} places, each in {ctx.region}; no log of the model calls"


def event_counts(messages: Iterable[str]) -> dict[str, int]:
    """How often each `log_event` name stands in the function's log lines."""
    counts: dict[str, int] = {}
    for message in messages:
        # Lambda's own lines for a run that ended without the handler's word.
        if "Task timed out" in message:
            counts["timed-out"] = counts.get("timed-out", 0) + 1
            continue
        if "Runtime.ExitError" in message or "Runtime.OutOfMemory" in message:
            counts["runtime-error"] = counts.get("runtime-error", 0) + 1
            continue
        start = message.find('{"event"')
        if start < 0:
            continue
        try:
            name = json.loads(message[start:])["event"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def check_function_log(ctx: Context) -> str:
    """The answer function's log since the run began: each event and how often, written to
    function-log.json; fails on a crash."""
    messages = []
    pages = ctx.client("logs").get_paginator("filter_log_events")
    with explained("Reading the answer function's log"):
        for page in pages.paginate(
            logGroupName=FUNCTION_LOG_GROUP, startTime=int(ctx.started.timestamp() * 1000)
        ):
            messages.extend(event["message"] for event in page["events"])
    counts = event_counts(messages)
    (ctx.out_dir / "function-log.json").write_text(
        json.dumps(counts, indent=2, sort_keys=True), encoding="utf-8"
    )
    if broken := {
        k: n for k, n in counts.items() if k in ("crashed", "timed-out", "runtime-error")
    }:
        raise StepFailed(f"the function ended badly: {broken}; see its log")
    return ", ".join(f"{name} {n}" for name, n in sorted(counts.items())) or "no events"


# --- Readers of a command's output --------------------------------------------------------


_HIT = re.compile(r"^\d+\. score \S+  workspace=(\S+)", re.MULTILINE)


def hits_only_in_workspace(output: str, ctx: Context) -> str:
    """retrieve in the new workspace: at least one hit, and every hit from it."""
    hits = _HIT.findall(output)
    strangers = sorted({hit for hit in hits if hit != ctx.workspace})
    if strangers:
        raise StepFailed(f"hits from {strangers} in {ctx.workspace}")
    if not hits:
        raise StepFailed(f"no hit in {ctx.workspace}")
    return f"{len(hits)} hit(s), each from {ctx.workspace}"


def document_of(file: str) -> str:
    """The document a hit's file comes from: the part before `--` of a section, which has no
    suffix of its own, or the name without its suffix of a file uploaded whole."""
    return file.split("--")[0] if "--" in file else Path(file).stem


_HIT_FILE = re.compile(r"^\d+\. score \S+  workspace=(\S+)  layer=\S+  (\S+)$", re.MULTILINE)


def no_hits_of_workspace(output: str, ctx: Context) -> str:
    """retrieve in another workspace finds hits there, and none of them is a section of the new
    workspace's documents, whatever its metadata says: the search filter reads the metadata,
    so only the file names show a document filed under the wrong workspace."""
    hits = _HIT_FILE.findall(output)
    if not hits:
        raise StepFailed(f"no hit in {ctx.other_workspace}: nothing to tell apart")
    ours = sorted(
        file
        for workspace, file in hits
        if workspace == ctx.workspace or document_of(file) in ctx.documents
    )
    if ours:
        raise StepFailed(f"{ctx.workspace}'s {ours} show up in {ctx.other_workspace}")
    return f"{len(hits)} hit(s) in {ctx.other_workspace}, none of {ctx.workspace}'s documents"


def answered(output: str, ctx: Context) -> str:
    """ask printed an answer of the new workspace, with the expected fact when one is given."""
    if f"Answer ({ctx.workspace}):" not in output:
        raise StepFailed("no answer printed")
    answer = output.split("\nLayers cited:", 1)[0]
    if ctx.fact and ctx.fact not in answer:
        raise StepFailed(f"the answer lacks {ctx.fact!r}")
    cited = re.search(r"^Layers cited: (.*)$", output, re.MULTILINE)
    found = f"; holds {ctx.fact!r}" if ctx.fact else ""
    return f"answered{found}; layers cited: {cited.group(1) if cited else '?'}"


def refused_with_401(output: str, ctx: Context) -> str:
    """ask with the new workspace's key in another workspace was refused with 401."""
    if not re.search(r"\bHTTP 401\b", output):
        raise StepFailed("not refused with 401")
    return f"the key of {ctx.workspace} is refused in {ctx.other_workspace} (401)"


# --- The plan -----------------------------------------------------------------------------


def python(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-m", *arguments)


def build_plan(args: argparse.Namespace) -> list[Step]:
    """Every step of the run, in its order."""
    region = ("--region", args.region)
    model = ("--model", args.model)
    w = args.workspace
    source = str(args.source)

    def out(name: str) -> tuple[str, str]:
        return "--out", str(args.out_dir / name)

    plan = ("--plan", args.plan) if args.plan else ()
    key_command = (
        f"aws ssm get-parameter --name /andur/api-keys/{w} --with-decryption "
        f"--region {args.region} --query Parameter.Value --output text | clip"
    )
    # Git Bash rewrites the path /andur/... into a Windows path (ParameterNotFound)
    # unless MSYS_NO_PATHCONV=1 stands before the command; PowerShell does not know
    # that prefix. So the step names the command once for each shell.
    key_commands = (
        f"in PowerShell: {key_command}  --  in Git Bash: MSYS_NO_PATHCONV=1 {key_command}"
    )
    return [
        # Before anything else: who signs in, the stack, its address.
        Step("identity", "check", "Signed-in identity", check=check_identity),
        Step("stack", "check", "Stack deployed and finished", check=check_stack),
        Step(
            "deploy-time", "check", "Bootstrap and deploy, by their events", check=check_deploy_time
        ),
        Step("address", "check", "Every address in the region", check=check_address_region),
        Step(
            "https-only", "check", "HTTPS only, headers, no key no answer", check=check_https_only
        ),
        # The new business, set up as a real one is.
        Step(
            "documents-check",
            "setup",
            "The documents checked, no AWS",
            command=python("scripts.ingest", "--check", "--source", source, "--workspace", w),
        ),
        Step(
            "key",
            "setup",
            "Workspace and its key",
            command=python("scripts.workspace", *region, "add", w, *plan),
            secret_stdout=True,
        ),
        Step(
            "hand-over",
            "setup",
            "The key handed to the business",
            manual=f"Copy the key into the clipboard without showing it, and hand it over: "
            f"{key_commands}",
        ),
        Step(
            "documents",
            "setup",
            "The documents uploaded and indexed",
            command=python("scripts.ingest", "--source", source, "--workspace", w, *region),
        ),
        Step(
            "search",
            "setup",
            "A search in the new workspace",
            command=python("scripts.retrieve", args.question, "--workspace", w, *region),
            reads=hits_only_in_workspace,
        ),
        Step(
            "first-question",
            "setup",
            "The first question, through ask.py",
            command=python(
                "scripts.ask", args.question, "--workspace", w, "--language", "de", *region
            ),
            reads=answered,
        ),
        Step(
            "page",
            "setup",
            "The page in a browser",
            manual="Open WebUrl in a browser, enter the key, ask the same question; in the "
            "developer tools every response carries the security headers.",
        ),
        # The new business stays apart from the others.
        Step(
            "demo-documents",
            "apart",
            "The demo workspaces' documents (the measurements need them)",
            command=python("scripts.ingest", *region),
        ),
        Step(
            "search-apart",
            "apart",
            "A search in another workspace finds nothing of the new one",
            command=python(
                "scripts.retrieve", args.question, "--workspace", args.other_workspace, *region
            ),
            reads=no_hits_of_workspace,
        ),
        Step(
            "key-apart",
            "apart",
            "The new key is refused in another workspace",
            command=python(
                "scripts.ask", args.question, "--workspace", args.other_workspace, *region
            ),
            reads=refused_with_401,
            exit_code=1,
            environment=workspace_key_environment,
        ),
        # The measurements against the deployed stack. The topic model's lane and the answer
        # model's lane share no quota: topic_probe calls only the topic model, tool_probe and
        # grounding_probe never call it.
        Step(
            "topics",
            "measure",
            "topic_probe --deployed",
            command=python("scripts.topic_probe", "--deployed", *out("topics.json"), *region),
            group="topics and grounding",
            lane="topic model",
        ),
        Step(
            "tools",
            "measure",
            "tool_probe, the answer model",
            command=python("scripts.tool_probe", *model, *out("tools.json"), *region),
            group="topics and grounding",
            lane="answer model",
        ),
        Step(
            "grounding",
            "measure",
            "grounding_probe",
            command=python("scripts.grounding_probe", *out("grounding.json"), *region),
            group="topics and grounding",
            lane="answer model",
        ),
        Step(
            "answers-en",
            "measure",
            "evaluate, English on English documents",
            command=python("scripts.evaluate", *model, *out("answers-en.json"), *region),
        ),
        Step(
            "answers-de",
            "measure",
            "evaluate, German on English documents",
            command=python(
                "scripts.evaluate",
                *model,
                "--questions",
                "scripts/evaluation-questions-de.json",
                *out("answers-de.json"),
                *region,
            ),
        ),
        Step(
            "answers-de-de",
            "measure",
            "evaluate, German on German documents",
            command=python(
                "scripts.evaluate",
                *model,
                "--questions",
                "scripts/evaluation-questions-kalvenstett.json",
                *out("answers-de-de.json"),
                *region,
            ),
        ),
        Step(
            "sources",
            "measure",
            "control_eval, retrieval in the region",
            command=python(
                "scripts.control_eval",
                *model,
                "--retrieve-region",
                args.region,
                *out("sources.json"),
                *region,
            ),
        ),
        # What the deployed stack holds, and what its function logged meanwhile.
        Step("residency", "after", "Where the data lies, as AWS reports it", check=check_residency),
        Step(
            "function-log", "after", "The function's log since the start", check=check_function_log
        ),
    ]


# --- Running it ---------------------------------------------------------------------------


def run_command(
    argv: Sequence[str], log: Path, secret_stdout: bool, environment: Mapping[str, str]
) -> tuple[int, str]:
    """Run a command from the repository root and write its output to `log`; stdout is thrown
    away when it carries a secret. Returns the exit code and the output. A key set in the
    shell is not handed on: `ask` would take it over the new workspace's own."""
    inherited = {k: v for k, v in os.environ.items() if k != API_KEY_ENVIRONMENT_VARIABLE}
    completed = subprocess.run(
        list(argv),
        cwd=REPOSITORY,
        env={**inherited, **environment, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.DEVNULL if secret_stdout else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    log.write_text(" ".join(argv[1:]) + "\n\n" + output, encoding="utf-8")
    return completed.returncode, output


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


@dataclass
class Runner:
    ctx: Context
    manual_mode: str = "skip"
    run: Callable[..., tuple[int, str]] = run_command
    clock: Callable[[], float] = time.monotonic
    ask_person: Callable[[str], str] = input
    say: Callable[[str], None] = say
    #: Every step finished so far, in the plan's order.
    results: list[Result] = field(default_factory=list)

    def clean(self, text: str) -> str:
        """The text without the account id, for the screen."""
        account = self.ctx.account
        return text.replace(account, ACCOUNT_PLACEHOLDER) if account else text

    def tell(self, line: str) -> None:
        self.say(self.clean(line))

    def step(self, number: int, step: Step) -> Result:
        log = self.ctx.out_dir / f"{number:02d}-{step.id}.log"
        started, begin = now_text(), self.clock()
        result = Result(
            number, step.id, step.phase, step.title, step.kind, "passed", started, 0.0, ""
        )
        result.group = step.group
        self.tell(f"[{started}] {number:02d} {step.id}: {step.title}")
        try:
            if step.manual:
                self.tell(f"  by hand: {step.manual}")
                result.status = "open"
                if self.manual_mode == "wait":
                    answer = self.ask_person("  Enter when done, or 'skip': ")
                    result.status = "open" if answer.strip().lower() == "skip" else "done"
                result.summary = "done by hand" if result.status == "done" else "left open"
            elif step.check:
                result.summary = step.check(self.ctx)
            else:
                assert step.command is not None
                environment = step.environment(self.ctx) if step.environment else {}
                code, output = self.run(step.command, log, step.secret_stdout, environment)
                result.exit_code, result.log = code, log.name
                if code != step.exit_code:
                    raise StepFailed(f"exit code {code}, not {step.exit_code}; see {log.name}")
                result.summary = step.reads(output, self.ctx) if step.reads else f"exit code {code}"
        except (StepFailed, AwsProblem) as problem:
            result.status, result.summary = "failed", str(problem)
        except Exception as problem:  # a network break, a missing field: the step failed
            result.status = "failed"
            result.summary = f"stopped by {type(problem).__name__}: {problem}"
        result.seconds = round(self.clock() - begin, 1)
        self.tell(f"  {result.status} after {format_seconds(result.seconds)}: {result.summary}")
        return result

    def lane(self, entries: Sequence[tuple[int, Step]]) -> list[Result]:
        done: list[Result] = []
        for number, step in entries:
            done.append(self.step(number, step))
            if done[-1].status == "failed":
                break
        return done

    def group(self, members: Sequence[tuple[int, Step]]) -> list[Result]:
        """Run a group's lanes at once; the results come back in the plan's order."""
        lanes: dict[str | None, list[tuple[int, Step]]] = {}
        for number, step in members:
            lanes.setdefault(step.lane, []).append((number, step))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            futures = [pool.submit(self.lane, entries) for entries in lanes.values()]
            finished = [result for future in futures for result in future.result()]
        order = {step.id: number for number, step in members}
        return sorted(finished, key=lambda result: order[result.id])

    def run_all(
        self, steps: Sequence[tuple[int, Step]], record: Callable[[list[Result]], None]
    ) -> list[Result]:
        """Run the steps in order, a group's lanes at once; stop after the first failure."""
        results = self.results
        index = 0
        while index < len(steps):
            group = steps[index][1].group
            end = index + 1
            while group and end < len(steps) and steps[end][1].group == group:
                end += 1
            members = steps[index:end]
            batch = self.group(members) if group else [self.step(*members[0])]
            results.extend(batch)
            record(results)
            if any(result.status == "failed" for result in batch):
                break
            index = end
        return results


def select(
    plan: Sequence[Step], start: str | None, only: Sequence[str] | None
) -> list[tuple[int, Step]]:
    """The steps to run, numbered by their place in the plan. The identity always comes first:
    without it the logs could not be cleared of the account id."""
    ids = [step.id for step in plan]
    for wanted in [start, *(only or [])]:
        if wanted is not None and wanted not in ids:
            raise ValueError(f"no step {wanted!r}; the steps: {', '.join(ids)}")
    numbered = list(enumerate(plan, start=1))
    if only:
        chosen = [(n, s) for n, s in numbered if s.id in only]
    elif start:
        chosen = numbered[ids.index(start) :]
    else:
        chosen = numbered
    if chosen[0][1].id != ids[0]:
        chosen = [numbered[0], *chosen]
    return chosen


def format_seconds(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s" if minutes else f"{rest} s"


def summary_lines(results: Sequence[Result], wall_seconds: float) -> list[str]:
    """The run as a Markdown table, then the time per phase and of the whole run."""
    lines = ["| # | Step | Kind | Status | Took | Result |", "|---|---|---|---|---|---|"]
    for r in results:
        took = format_seconds(r.seconds) + (" (at once)" if r.group else "")
        summary = r.summary.replace("|", "\\|")
        lines.append(f"| {r.number} | {r.id} | {r.kind} | {r.status} | {took} | {summary} |")
    lines += ["", "| Phase | Steps | Their seconds added up |", "|---|---|---|"]
    phases: dict[str, list[Result]] = {}
    for r in results:
        phases.setdefault(r.phase, []).append(r)
    for phase, members in phases.items():
        added = format_seconds(sum(m.seconds for m in members))
        lines.append(f"| {phase} | {len(members)} | {added} |")
    lines += ["", f"Wall clock of the run: {format_seconds(wall_seconds)}."]
    still_open = [r.id for r in results if r.status == "open"]
    if still_open:
        lines.append(f"By hand and still open: {', '.join(still_open)}.")
    return lines


def scrub(folder: Path, account: str | None) -> int:
    """Replace the account id in every text file of the folder; returns how many it changed."""
    if not account:
        return 0
    changed = 0
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if account in text:
            path.write_text(text.replace(account, ACCOUNT_PLACEHOLDER), encoding="utf-8")
            changed += 1
    return changed


def inside_repository(folder: Path) -> bool:
    resolved = folder.resolve()
    return resolved == REPOSITORY or REPOSITORY in resolved.parents


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.rehearsal",
        description="Set up a new business on the deployed stack, measure it, time every step.",
    )
    parser.add_argument("--workspace", required=True, help="the new business's workspace")
    parser.add_argument(
        "--source", type=Path, required=True, help="its folder: <workspace>/<layer>/<document>"
    )
    parser.add_argument("--question", required=True, help="its first question, asked in German")
    parser.add_argument("--fact", help="what the first answer must contain, such as '68 Euro'")
    parser.add_argument(
        "--out-dir", type=Path, required=True, help="logs and results, outside the repository"
    )
    parser.add_argument(
        "--other-workspace",
        default="quillmoor",
        help="a workspace it must stay apart from (default: quillmoor)",
    )
    parser.add_argument("--plan", choices=("betrieb", "firma"), help="its key's plan; default demo")
    parser.add_argument(
        "--model", help="the measured answer model; default the region's own, as the stack has it"
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        choices=RUN_REGIONS,
        help=f"a region whose function is its own address; default {DEFAULT_REGION}",
    )
    parser.add_argument("--stack-name", default=STACK_NAME, help=f"default: {STACK_NAME}")
    parser.add_argument("--expect-role", help="stop unless signed in with this role")
    parser.add_argument(
        "--manual",
        choices=("skip", "wait"),
        default="skip",
        help="steps by hand: record them as open (default), or wait for Enter and time them",
    )
    parser.add_argument("--from", dest="start", help="begin at this step")
    parser.add_argument("--only", action="append", help="run only this step (may repeat)")
    parser.add_argument("--list", action="store_true", help="print the plan and call nothing")
    args = parser.parse_args(argv)
    # The commands run from the repository root, wherever the run was started.
    args.source, args.out_dir = args.source.resolve(), args.out_dir.resolve()
    args.model = args.model or deployed_model_name(args.region)
    return args


def document_names(source: Path, workspace: str) -> frozenset[str]:
    """The names, without their suffix, of the workspace's documents under `source`."""
    folder = source / workspace
    if not folder.is_dir():
        return frozenset()
    sidecar = ".metadata.json"
    return frozenset(
        path.stem
        for path in folder.rglob("*")
        if path.is_file() and not path.name.endswith(sidecar)
    )


def plan_lines(chosen: Sequence[tuple[int, Step]]) -> list[str]:
    lines = []
    for number, step in chosen:
        lane = f" [at once: {step.lane}]" if step.group else ""
        what = step.manual or (" ".join(step.command[1:]) if step.command else "in this process")
        lines.append(f"{number:2d}. {step.phase:<7} {step.id:<15} {step.kind:<7}{lane} {what}")
    return lines


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)
    plan = build_plan(args)
    try:
        chosen = select(plan, args.start, args.only)
    except ValueError as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    if args.list:
        print("\n".join(plan_lines(chosen)))
        return 0
    if inside_repository(args.out_dir):
        print("error: --out-dir must lie outside the repository", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Context(
        region=args.region,
        stack_name=args.stack_name,
        workspace=args.workspace,
        other_workspace=args.other_workspace,
        out_dir=args.out_dir,
        expect_role=args.expect_role,
        clients=lambda service: client(service, args.region),
        fact=args.fact,
        documents=document_names(args.source, args.workspace),
    )
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    begin = time.monotonic()

    def record(results: list[Result]) -> None:
        run = {
            "region": args.region,
            "workspace": args.workspace,
            "steps": list(map(asdict, results)),
        }
        text = json.dumps(run, indent=2, ensure_ascii=False)
        (args.out_dir / f"run-{tag}.json").write_text(text, encoding="utf-8")

    runner = Runner(ctx, manual_mode=args.manual)
    finished = False
    try:
        results = runner.run_all(chosen, record)
        finished = True
    finally:
        # Also after Ctrl+C: the steps done so far are summed up, and no file keeps the account.
        done = runner.results
        lines = summary_lines(done, time.monotonic() - begin)
        if not finished:
            lines.append("Stopped during the step after the last one listed.")
        (args.out_dir / f"run-{tag}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        scrub(args.out_dir, ctx.account)
        print(runner.clean("\n".join(lines)))
    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())

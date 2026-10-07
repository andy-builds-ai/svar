"""The checks of a rehearsal (scripts/rehearsal.py) that run in this process: against the
deployed stack, its address and its log."""

import concurrent.futures
import http.client
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from app.page import ASK_PATH, SECURITY_HEADERS
from scripts.ask import API_KEY_ENVIRONMENT_VARIABLE, read_key
from scripts.aws import explained
from scripts.rehearsal_runner import Context, StepFailed, format_seconds

#: The answer function and its log group, as the stack names them.
FUNCTION_NAME = "andur-answer"
FUNCTION_LOG_GROUP = f"/aws/lambda/{FUNCTION_NAME}"

#: Where AWS lists the address ranges of its regions.
IP_RANGES_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"

HTTP_TIMEOUT_SECONDS = 10


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
    address this computer has no route to (no IPv6 here, say) is named as not checked.

    A timeout counts as closed, but the summary names it apart: a network that blocks port 80
    on this computer's side looks the same from here as a server that never answers on it."""

    def probe(address: str) -> str:
        try:
            status, headers, _ = send("http", host, "GET", "/", address=address)
        except TimeoutError:
            return "timed out"  # nothing answered unencrypted, as far as this side can tell
        except (ConnectionRefusedError, ConnectionResetError):
            return "refused"  # nothing answers unencrypted
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
    timed_out = answers.count("timed out")
    closed = timed_out + answers.count("refused")
    summary = f"http: {closed} of {len(addresses)} address(es) without a connection"
    if timed_out:
        summary += f" ({timed_out} timed out, which a block on this side would look like too)"
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

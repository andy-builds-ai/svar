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
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from infra.answer_models import REGIONS, answer_model
from scripts import utf8_output
from scripts.aws import STACK_NAME, client
from scripts.layout import document_of
from scripts.rehearsal_checks import (
    check_address_region,
    check_deploy_time,
    check_function_log,
    check_https_only,
    check_identity,
    check_residency,
    check_stack,
    workspace_key_environment,
)
from scripts.rehearsal_runner import (
    Context,
    Result,
    Runner,
    Step,
    StepFailed,
    inside_repository,
    scrub,
    select,
    summary_lines,
)

#: The regions whose function is its own address. Behind CloudFront the address checks
#: cannot pass: its addresses are edge locations worldwide.
RUN_REGIONS = tuple(region for region, models in REGIONS.items() if not models.behind_cloudfront)
DEFAULT_REGION = "eu-central-1"


def deployed_model_name(region: str) -> str:
    """The name of the region's answer model in its table, as `evaluate` and the probes take it."""
    return answer_model(REGIONS[region].answer_model_id, region).name


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


def file_names(file: str) -> set[str]:
    """A hit's file name as retrieve printed it, and with its %-escapes decoded. Whether Bedrock
    gives a space in a document's S3 URI as it is or as %20 is not measured: both are read."""
    return {file, urllib.parse.unquote(file)}


#: The file name is the rest of the line: a document's name may hold spaces.
_HIT_FILE = re.compile(r"^\d+\. score \S+  workspace=(\S+)  layer=\S+  (.+)$", re.MULTILINE)


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
        if workspace == ctx.workspace
        or any(document_of(name) in ctx.documents for name in file_names(file))
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
    """The names, without their suffix, of the workspace's documents under `source`; composed,
    as `document_of` reads them, also where the file system keeps them decomposed."""
    folder = source / workspace
    if not folder.is_dir():
        return frozenset()
    sidecar = ".metadata.json"
    return frozenset(
        unicodedata.normalize("NFC", path.stem)
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

"""Measure the denied topics against a fixed question set, before a deploy or after it.

By default the topics of `DENIED_TOPICS` go into a guardrail of their own,
`PROBE_NAME`, built the way the stack builds the answer guardrail's topics:
same order, same tier, same EU guardrail profile. Every question goes through
`ApplyGuardrail` as INPUT, the way a visitor's question reaches the topic
filter, and the probe guardrail is deleted at the end, also when the run
fails. With ``--deployed`` the run measures the deployed answer guardrail
instead; it creates and deletes nothing.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from infra.stacks.andur_stack import (
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    DENIED_TOPICS,
    GUARDRAIL_PROFILE_ID,
    GUARDRAIL_PROFILE_REGIONS,
    PRICE_TOPIC_PER_1K_UNITS,
    TOPICS_TIER,
)
from scripts import utf8_output
from scripts.aws import REGION, AwsProblem, client, explained
from scripts.evaluate import deployed_environment

QUESTIONS_FILE = Path(__file__).with_name("topic-questions.json")

#: The only guardrail this script creates or deletes.
PROBE_NAME = "andur-topic-probe"

LANGUAGES = ("en", "de")

TOPIC_NAMES = tuple(name for name, _, _ in DENIED_TOPICS)

#: The same topics and question gave different verdicts in two runs (02.10.2026): a
#: question counts as right only if it is right in every one of these runs.
DEFAULT_REPEATS = 3

#: How long a new probe guardrail may take to become READY, and how often to look.
READY_TIMEOUT_SECONDS = 120
READY_POLL_SECONDS = 2


def load_probe_questions(path: Path) -> list[dict[str, Any]]:
    """The question set, checked: a question to block names the denied topic it must hit, a
    harmless one names its subject, which is no denied topic."""
    questions = json.loads(path.read_text(encoding="utf-8"))
    seen = set()
    for question in questions:
        name = question["id"]
        if name in seen:
            raise ValueError(f"{name}: the id is used twice")
        seen.add(name)
        if question["language"] not in LANGUAGES:
            raise ValueError(f"{name}: unknown language {question['language']!r}")
        if not isinstance(question["blocked"], bool):
            raise ValueError(f"{name}: blocked must be true or false")
        for field in ("topic", "question"):
            if not (isinstance(question[field], str) and question[field].strip()):
                raise ValueError(f"{name}: {field} must be a non-empty text")
        if question["blocked"] != (question["topic"] in TOPIC_NAMES):
            raise ValueError(
                f"{name}: a question to block names one of {list(TOPIC_NAMES)}, "
                "a harmless one names a subject that is none of them"
            )
    return questions


def topic_policy() -> dict[str, Any]:
    """`DENIED_TOPICS` as the CreateGuardrail API takes them, set the way the stack sets them."""
    return {
        "topicsConfig": [
            {
                "name": name,
                "definition": definition,
                "examples": list(examples),
                "type": "DENY",
                "inputEnabled": True,
                "inputAction": "BLOCK",
                "outputEnabled": True,
                "outputAction": "BLOCK",
            }
            for name, definition, examples in DENIED_TOPICS
        ],
        "tierConfig": {"tierName": TOPICS_TIER},
    }


def probe_request() -> dict[str, Any]:
    """The probe guardrail: the stack's topics, tier, profile and messages, nothing else."""
    return {
        "name": PROBE_NAME,
        "description": "Andur: temporary, measures DENIED_TOPICS; deleted after the run.",
        "topicPolicyConfig": topic_policy(),
        "crossRegionConfig": {"guardrailProfileIdentifier": GUARDRAIL_PROFILE_ID},
        "blockedInputMessaging": BLOCKED_INPUT_MESSAGE,
        "blockedOutputsMessaging": BLOCKED_OUTPUT_MESSAGE,
    }


def guardrails_named(bedrock: Any, name: str) -> list[str]:
    """The ids of every guardrail in the region with this name."""
    ids, token = [], None
    while True:
        with explained("Listing the guardrails"):
            page = bedrock.list_guardrails(**({"nextToken": token} if token else {}))
        ids += [g["id"] for g in page.get("guardrails", []) if g.get("name") == name]
        token = page.get("nextToken")
        if not token:
            return ids


def create_probe(bedrock: Any) -> str:
    """Create the probe guardrail and return its id; refuse if one is left from a run before."""
    left = guardrails_named(bedrock, PROBE_NAME)
    if left:
        raise AwsProblem(
            f"A guardrail named {PROBE_NAME} already exists ({', '.join(left)}), left by an "
            "earlier run. Delete it first: aws bedrock delete-guardrail "
            f"--guardrail-identifier {left[0]} --region <region>"
        )
    with explained(f"Creating the guardrail {PROBE_NAME}"):
        return bedrock.create_guardrail(**probe_request())["guardrailId"]


def wait_until_ready(
    bedrock: Any, guardrail_id: str, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Wait for the draft to be usable; a failed or slow one ends the run."""
    for _ in range(READY_TIMEOUT_SECONDS // READY_POLL_SECONDS):
        with explained(f"Reading the guardrail {PROBE_NAME}"):
            guardrail = bedrock.get_guardrail(guardrailIdentifier=guardrail_id)
        status = guardrail["status"]
        if status == "READY":
            return
        if status == "FAILED":
            raise AwsProblem(f"{PROBE_NAME} failed: {guardrail.get('statusReasons')}")
        sleep(READY_POLL_SECONDS)
    raise AwsProblem(f"{PROBE_NAME} was not ready after {READY_TIMEOUT_SECONDS} seconds")


def delete_probe(bedrock: Any, guardrail_id: str) -> None:
    """Delete the probe guardrail, after checking by its name that it is the probe."""
    with explained(f"Reading the guardrail {guardrail_id} before deleting it"):
        name = bedrock.get_guardrail(guardrailIdentifier=guardrail_id)["name"]
    if name != PROBE_NAME:
        raise AwsProblem(f"Refusing to delete {guardrail_id}: it is {name!r}, not {PROBE_NAME}")
    with explained(f"Deleting the guardrail {PROBE_NAME}"):
        bedrock.delete_guardrail(guardrailIdentifier=guardrail_id)


def delete_after_failure(bedrock: Any, guardrail_id: str, progress: Callable[[str], None]) -> None:
    """Delete the probe after a failed run without hiding why the run failed."""
    try:
        delete_probe(bedrock, guardrail_id)
    except AwsProblem as problem:
        progress(f"error: {problem}")
        return
    progress(f"deleted {PROBE_NAME} ({guardrail_id})")


def measure(
    runtime: Any,
    guardrail_id: str,
    version: str,
    questions: Sequence[Mapping[str, Any]],
    progress: Callable[[str], None],
    repeats: int = 1,
) -> list[dict[str, Any]]:
    """Every question through ApplyGuardrail as INPUT, ``repeats`` times: in how many runs it
    was blocked, and by which topics."""
    rows = [
        {
            "id": question["id"],
            "language": question["language"],
            "topic": question["topic"],
            "question": question["question"],
            "expected": question["blocked"],
            "runs": 0,
            "blocked_runs": 0,
            "topics": [],
            "usage": {},
        }
        for question in questions
    ]
    for run in range(1, repeats + 1):
        for row in rows:
            with explained(f"Applying the guardrail to {row['id']}"):
                response = runtime.apply_guardrail(
                    guardrailIdentifier=guardrail_id,
                    guardrailVersion=version,
                    source="INPUT",
                    content=[{"text": {"text": row["question"]}}],
                )
            topics = [
                topic["name"]
                for assessment in response.get("assessments", [])
                for topic in assessment.get("topicPolicy", {}).get("topics", [])
                if topic.get("action") == "BLOCKED"
            ]
            blocked = response["action"] == "GUARDRAIL_INTERVENED"
            row["runs"] += 1
            row["blocked_runs"] += blocked
            row["topics"] += [topic for topic in topics if topic not in row["topics"]]
            for kind, count in response.get("usage", {}).items():
                row["usage"][kind] = row["usage"].get(kind, 0) + count
            verdict = "right" if blocked == row["expected"] else "WRONG"
            outcome = "blocked " + ", ".join(topics) if blocked else "passed"
            progress(f"run {run} {row['id']} {verdict}: {outcome}")
    return rows


def always_blocked(row: Mapping[str, Any]) -> bool:
    return row["blocked_runs"] == row["runs"]


def ever_blocked(row: Mapping[str, Any]) -> bool:
    return row["blocked_runs"] > 0


def missed(row: Mapping[str, Any]) -> bool:
    """A question to block that passed in any run, or a harmless one blocked in any run."""
    return not always_blocked(row) if row["expected"] else ever_blocked(row)


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Slips per denied topic and language, false blocks per firing topic and language, cost.
    A question counts as right only if it was right in every run."""
    slips = {
        f"{topic} ({language})": {
            "blocked": sum(always_blocked(r) for r in group),
            "questions": len(group),
            "slipped": [r["id"] for r in group if not always_blocked(r)],
        }
        for topic in TOPIC_NAMES
        for language in LANGUAGES
        if (
            group := [
                r
                for r in rows
                if r["expected"] and r["topic"] == topic and r["language"] == language
            ]
        )
    }
    harmless = {
        language: {
            "blocked": sum(ever_blocked(r) for r in group),
            "questions": len(group),
        }
        for language in LANGUAGES
        if (group := [r for r in rows if not r["expected"] and r["language"] == language])
    }
    false_blocks: dict[str, list[str]] = {}
    for row in rows:
        if ever_blocked(row) and not row["expected"]:
            for topic in row["topics"] or ["(no topic)"]:
                false_blocks.setdefault(f"{topic} ({row['language']})", []).append(row["id"])
    units: dict[str, int] = {}
    for row in rows:
        for kind, count in row["usage"].items():
            units[kind] = units.get(kind, 0) + count
    topic_units = units.get("topicPolicyUnits", 0)
    return {
        "runs": max((r["runs"] for r in rows), default=0),
        "slips": slips,
        "harmless": harmless,
        "false_blocks": false_blocks,
        "false_blocked": sum(missed(r) for r in rows if not r["expected"]),
        "slipped": sum(missed(r) for r in rows if r["expected"]),
        "other_topic": [
            r["id"]
            for r in rows
            if r["expected"] and ever_blocked(r) and r["topic"] not in r["topics"]
        ],
        "units": {kind: count for kind, count in units.items() if count},
        "cost_usd": str(Decimal(topic_units) * Decimal(PRICE_TOPIC_PER_1K_UNITS) / 1000),
    }


def report(summary: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The run as Markdown: blocked per topic and language, false blocks, every miss, cost."""
    lines = [
        f"Runs per question: {summary['runs']}. Right means right in every run.",
        "",
        "| Denied topic (language) | Blocked | Slipped through |",
        "|---|---|---|",
        *(
            f"| {key} | {s['blocked']}/{s['questions']} | {', '.join(s['slipped']) or '-'} |"
            for key, s in summary["slips"].items()
        ),
        "",
        "| Harmless questions (language) | Blocked by mistake |",
        "|---|---|",
        *(
            f"| {language} | {h['blocked']}/{h['questions']} |"
            for language, h in summary["harmless"].items()
        ),
        *(f"- by {key}: {', '.join(ids)}" for key, ids in summary["false_blocks"].items()),
    ]
    misses = [r for r in rows if missed(r)]
    if misses:
        lines += ["", "| Id | Expected | Blocked in | By | Question |", "|---|---|---|---|---|"]
        lines += [
            f"| {r['id']} | {'blocked' if r['expected'] else 'answered'} "
            f"| {r['blocked_runs']}/{r['runs']} | {', '.join(r['topics']) or '-'} "
            f"| {r['question']} |"
            for r in misses
        ]
    if summary["other_topic"]:
        lines += ["", f"Blocked, but under another topic: {', '.join(summary['other_topic'])}"]
    lines += [
        "",
        f"Units: {summary['units']}. Cost: {summary['cost_usd']} USD "
        f"(topic units at {PRICE_TOPIC_PER_1K_UNITS} USD per 1,000).",
    ]
    return lines


def deployed_topics_match(bedrock: Any, guardrail_id: str, version: str) -> bool:
    """Whether the deployed version holds `DENIED_TOPICS` in the tier the stack sets."""
    with explained("Reading the deployed guardrail"):
        guardrail = bedrock.get_guardrail(
            guardrailIdentifier=guardrail_id, guardrailVersion=version
        )
    policy = guardrail.get("topicPolicy", {})
    deployed = [
        (t["name"], t["definition"], list(t.get("examples", []))) for t in policy.get("topics", [])
    ]
    wanted = [(name, definition, list(examples)) for name, definition, examples in DENIED_TOPICS]
    return deployed == wanted and policy.get("tier", {}).get("tierName") == TOPICS_TIER


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.topic_probe",
        description="Measure DENIED_TOPICS against the topic question set.",
    )
    parser.add_argument(
        "--deployed",
        action="store_true",
        help="measure the deployed answer guardrail instead of a temporary probe guardrail",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"ask every question this many times (default: {DEFAULT_REPEATS})",
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
    parser.add_argument("--out", type=Path, help="write every question's result as JSON here")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)
    if args.region not in GUARDRAIL_PROFILE_REGIONS:
        print(
            f"error: {args.region} is not a source region of {GUARDRAIL_PROFILE_ID}",
            file=sys.stderr,
        )
        return 2
    questions = load_probe_questions(args.questions)

    def progress(line: str) -> None:
        print(line, file=sys.stderr)

    try:
        bedrock = client("bedrock", args.region)
        runtime = client("bedrock-runtime", args.region)
        if args.deployed:
            environment = deployed_environment(args.region)
            guardrail_id = environment["GUARDRAIL_ID"]
            version = environment["GUARDRAIL_VERSION"]
            same = deployed_topics_match(bedrock, guardrail_id, version)
            progress(f"deployed version {version}; topics as in DENIED_TOPICS: {same}")
            rows = measure(runtime, guardrail_id, version, questions, progress, args.repeat)
        else:
            guardrail_id, version, same = create_probe(bedrock), "DRAFT", True
            progress(f"created {PROBE_NAME} ({guardrail_id})")
            try:
                wait_until_ready(bedrock, guardrail_id)
                rows = measure(runtime, guardrail_id, version, questions, progress, args.repeat)
            except BaseException:
                delete_after_failure(bedrock, guardrail_id, progress)
                raise
            delete_probe(bedrock, guardrail_id)
            progress(f"deleted {PROBE_NAME} ({guardrail_id})")
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    summary = summarise(rows)
    for line in report(summary, rows):
        print(line)
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "guardrail": "deployed" if args.deployed else PROBE_NAME,
                    "version": version,
                    "topics_as_in_the_repository": same,
                    "topic_policy": topic_policy(),
                    "summary": summary,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

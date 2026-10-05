"""Measure the denied topics against a fixed question set, before a deploy or after it.

By default the topics of `DENIED_TOPICS` go into a guardrail of their own,
`PROBE_NAME`, built the way the stack builds the topic guardrail: same order,
same tier, same EU guardrail profile. Every question goes through
`ApplyGuardrail` as INPUT, the way a visitor's question reaches the topic
filter of the answer guardrail, and every answer as OUTPUT without its
citations, the way the function hands the model's answer to the topic
guardrail: each on its own, without the other and without the sources. The
probe guardrail is deleted at the end, also when the run fails. With
``--deployed`` the run measures the deployed guardrails instead, the questions
on the answer guardrail and the answers on the topic guardrail; it creates and
deletes nothing.
"""

import argparse
import json
import random
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
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
from scripts.aws import REGION, AwsProblem, client, error_code, explained
from scripts.evaluate import deployed_environment, import_handler

QUESTIONS_FILE = Path(__file__).with_name("topic-questions.json")
ANSWERS_FILE = Path(__file__).with_name("topic-answers.json")

#: What the topic filter reads on each side, and the field of a set that holds it.
TEXT_FIELDS = {"INPUT": "question", "OUTPUT": "answer"}

#: The deployed guardrail that reads each side, by the function's environment variables.
DEPLOYED_GUARDRAILS = {
    "INPUT": ("GUARDRAIL_ID", "GUARDRAIL_VERSION"),
    "OUTPUT": ("TOPIC_GUARDRAIL_ID", "TOPIC_GUARDRAIL_VERSION"),
}

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

#: How many ApplyGuardrail calls run at once. One call took 0.43 s one after the other (20 calls,
#: 05.10.2026), so 8 at once make about 19 calls a second: under the 25 a second AWS allows, with
#: room, and below the 10 connections botocore keeps open by default.
DEFAULT_WORKERS = 8

#: AWS answers ThrottlingException when more than 25 ApplyGuardrail calls a second arrive in
#: eu-west-1 (the default of "Each of the other supported Regions" in the Amazon Bedrock quotas,
#: docs.aws.amazon.com/general/latest/gr/bedrock.html; the account's own value is in Service
#: Quotas). Such a call is repeated after a wait that doubles, with jitter, up to a ceiling.
THROTTLE_ATTEMPTS = 8
THROTTLE_WAIT_SECONDS = 1.0
THROTTLE_MAX_WAIT_SECONDS = 20.0


def load_probe_questions(path: Path) -> list[dict[str, Any]]:
    """The question set, checked: a question to block names the denied topic it must hit, a
    harmless one names its subject, which is no denied topic."""
    return load_probe_set(path, TEXT_FIELDS["INPUT"])


def load_probe_answers(path: Path) -> list[dict[str, Any]]:
    """The answer set, checked the same way: an answer to withhold names the denied topic it
    strays into, a harmless one the subject it answers. An answer of citations alone is
    refused: the function never checks one, so it measures nothing."""
    answers = load_probe_set(path, TEXT_FIELDS["OUTPUT"])
    for answer in answers:
        if not judged_text("OUTPUT", answer["answer"]):
            raise ValueError(f"{answer['id']}: the answer is citations alone")
    return answers


def judged_text(source: str, text: str) -> str:
    """The text the topics judge: a question as it is, an answer without its citations, cut
    out by the function's own `without_citations`."""
    return import_handler().without_citations(text) if source == "OUTPUT" else text


def load_probe_set(path: Path, field: str) -> list[dict[str, Any]]:
    """A set of texts under ``field``, each with an id, a language, a topic and a verdict."""
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
        for key in ("topic", field):
            if not (isinstance(question.get(key), str) and question[key].strip()):
                raise ValueError(f"{name}: {key} must be a non-empty text")
        if question["blocked"] != (question["topic"] in TOPIC_NAMES):
            raise ValueError(
                f"{name}: a text to block names one of {list(TOPIC_NAMES)}, "
                "a harmless one names a subject that is none of them"
            )
    return questions


def topic_policy() -> dict[str, Any]:
    """`DENIED_TOPICS` as the CreateGuardrail API takes them, set the way the stack sets them
    on the topic guardrail: both sides."""
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


def delete_reporting(bedrock: Any, guardrail_id: str, progress: Callable[[str], None]) -> bool:
    """Delete the probe and say whether it is gone. A failed delete is reported, not raised:
    after a failed run it must not hide why the run failed, after a finished run it must not
    cost the report of calls already paid for."""
    try:
        delete_probe(bedrock, guardrail_id)
    except AwsProblem as problem:
        progress(f"error: {problem}")
        return False
    progress(f"deleted {PROBE_NAME} ({guardrail_id})")
    return True


def apply_guardrail(
    runtime: Any,
    guardrail_id: str,
    version: str,
    source: str,
    row: Mapping[str, Any],
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], int]:
    """One text through ApplyGuardrail: the response and how often AWS throttled the call first.
    A throttled call is asked again after a wait; any other error ends the run."""
    with explained(f"Applying the guardrail to {row['id']}"):
        for attempt in range(1, THROTTLE_ATTEMPTS + 1):
            try:
                response = runtime.apply_guardrail(
                    guardrailIdentifier=guardrail_id,
                    guardrailVersion=version,
                    source=source,
                    content=[{"text": {"text": row["text"]}}],
                )
            except Exception as exc:
                if error_code(exc) != "ThrottlingException" or attempt == THROTTLE_ATTEMPTS:
                    raise
                ceiling = min(THROTTLE_MAX_WAIT_SECONDS, THROTTLE_WAIT_SECONDS * 2 ** (attempt - 1))
                sleep(random.uniform(ceiling / 2, ceiling))
            else:
                return response, attempt - 1
    raise AssertionError("unreachable: the last attempt returns or raises")


def measure(
    runtime: Any,
    guardrail_id: str,
    version: str,
    questions: Sequence[Mapping[str, Any]],
    progress: Callable[[str], None],
    repeats: int = 1,
    source: str = "INPUT",
    workers: int = DEFAULT_WORKERS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Every text through ApplyGuardrail as ``source``, ``repeats`` times: in how many runs it
    was blocked, and by which topics. INPUT reads a set's questions, OUTPUT its answers without
    their citations, as the function checks them (`judged_text`). Meant
    for any list of texts against any guardrail, a handful as much as a whole set, so a probe
    needs no loop of its own.

    Up to ``workers`` calls run at once (``DEFAULT_WORKERS``, as on the command line), but the
    responses are counted and reported one by one in the order the calls are listed (run by
    run, text by text), so rows, counts and progress lines are the same for any number of
    workers. ``workers=1`` runs the calls one after the other."""
    field = TEXT_FIELDS[source]
    rows = [
        {
            "id": question["id"],
            "language": question["language"],
            "topic": question["topic"],
            "source": source,
            "text": judged_text(source, question[field]),
            "expected": question["blocked"],
            "runs": 0,
            "blocked_runs": 0,
            "topics": [],
            "usage": {},
        }
        for question in questions
    ]
    calls = [(run, row) for run in range(1, repeats + 1) for row in rows]
    stop = threading.Event()

    def ask(row: Mapping[str, Any]) -> tuple[dict[str, Any], int] | None:
        if stop.is_set():  # an earlier call failed: do not start another
            return None
        return apply_guardrail(runtime, guardrail_id, version, source, row, sleep)

    throttled = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        if workers == 1:
            results = (ask(row) for _, row in calls)
        else:
            futures = [pool.submit(ask, row) for _, row in calls]
            results = (future.result() for future in futures)
        try:
            for (run, row), result in zip(calls, results, strict=True):
                assert result is not None
                response, retries = result
                throttled += retries
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
        except BaseException:
            stop.set()
            raise
    if throttled:
        progress(f"{source}: AWS throttled {throttled} calls; each was repeated after a wait")
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
        f"Runs per text: {summary['runs']}. Right means right in every run.",
        "",
        "| Denied topic (language) | Blocked | Slipped through |",
        "|---|---|---|",
        *(
            f"| {key} | {s['blocked']}/{s['questions']} | {', '.join(s['slipped']) or '-'} |"
            for key, s in summary["slips"].items()
        ),
        "",
        "| Harmless (language) | Blocked by mistake |",
        "|---|---|",
        *(
            f"| {language} | {h['blocked']}/{h['questions']} |"
            for language, h in summary["harmless"].items()
        ),
        *(f"- by {key}: {', '.join(ids)}" for key, ids in summary["false_blocks"].items()),
    ]
    misses = [r for r in rows if missed(r)]
    if misses:
        lines += ["", "| Id | Expected | Blocked in | By | Text |", "|---|---|---|---|---|"]
        lines += [
            f"| {r['id']} | {'blocked' if r['expected'] else 'answered'} "
            f"| {r['blocked_runs']}/{r['runs']} | {', '.join(r['topics']) or '-'} "
            f"| {r['text']} |"
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


def deployed_topics_match(bedrock: Any, guardrail_id: str, version: str, on_answer: bool) -> bool:
    """Whether the deployed version holds `DENIED_TOPICS` in the tier the stack sets, read in
    the answer as well only if ``on_answer``: the topic guardrail reads both sides, the answer
    guardrail the question alone."""
    with explained(f"Reading the deployed guardrail {guardrail_id}"):
        guardrail = bedrock.get_guardrail(
            guardrailIdentifier=guardrail_id, guardrailVersion=version
        )
    policy = guardrail.get("topicPolicy", {})
    deployed = [
        (t["name"], t["definition"], list(t.get("examples", [])), t.get("outputEnabled"))
        for t in policy.get("topics", [])
    ]
    wanted = [
        (name, definition, list(examples), on_answer)
        for name, definition, examples in DENIED_TOPICS
    ]
    return deployed == wanted and policy.get("tier", {}).get("tierName") == TOPICS_TIER


def deployed_guardrails(environment: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """The deployed guardrail and version that read each side, from the function's
    environment; a function from before the topic guardrail ends the run."""
    missing = [
        name
        for names in DEPLOYED_GUARDRAILS.values()
        for name in names
        if not environment.get(name)
    ]
    if missing:
        raise AwsProblem(
            f"The deployed function has no {', '.join(missing)}: it predates the topic "
            "guardrail. Deploy first, or measure without --deployed."
        )
    return {
        source: (environment[id_name], environment[version_name])
        for source, (id_name, version_name) in DEPLOYED_GUARDRAILS.items()
    }


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.topic_probe",
        description="Measure DENIED_TOPICS against the topic question set.",
    )
    parser.add_argument(
        "--deployed",
        action="store_true",
        help=(
            "measure the deployed guardrails instead of a temporary probe guardrail: the "
            "questions on the answer guardrail, the answers on the topic guardrail"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"ask every question this many times (default: {DEFAULT_REPEATS})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            f"ApplyGuardrail calls at once (default: {DEFAULT_WORKERS}); 1 asks one after the "
            "other. The result and its order do not depend on it. AWS allows 25 calls a second "
            "in eu-west-1 by default and a throttled call is repeated after a wait."
        ),
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
    parser.add_argument("--answers", type=Path, default=ANSWERS_FILE)
    parser.add_argument("--out", type=Path, help="write every question's result as JSON here")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
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
    sets = {
        "INPUT": load_probe_questions(args.questions),
        "OUTPUT": load_probe_answers(args.answers),
    }

    def progress(line: str) -> None:
        print(line, file=sys.stderr)

    def measure_both(runtime: Any, guardrails: Mapping[str, tuple[str, str]]) -> dict[str, Any]:
        started = time.monotonic()
        rows = {
            source: measure(
                runtime, *guardrails[source], texts, progress, args.repeat, source, args.workers
            )
            for source, texts in sets.items()
        }
        calls = args.repeat * sum(len(texts) for texts in sets.values())
        seconds = time.monotonic() - started
        progress(
            f"{calls} calls in {seconds:.1f} s with {args.workers} workers "
            f"({seconds / calls:.2f} s per call on average)"
        )
        return rows

    deleted = True  # no probe to delete when the deployed guardrails are measured
    try:
        bedrock = client("bedrock", args.region)
        runtime = client("bedrock-runtime", args.region)
        if args.deployed:
            guardrails = deployed_guardrails(deployed_environment(args.region))
            same = all(
                deployed_topics_match(bedrock, *guardrails[source], on_answer=source == "OUTPUT")
                for source in guardrails
            )
            version = {source: guardrails[source][1] for source in guardrails}
            progress(
                f"deployed versions: questions {version['INPUT']}, answers {version['OUTPUT']}; "
                f"topics as in DENIED_TOPICS: {same}"
            )
            rows = measure_both(runtime, guardrails)
        else:
            guardrail_id, same = create_probe(bedrock), True
            version = {"INPUT": "DRAFT", "OUTPUT": "DRAFT"}
            progress(f"created {PROBE_NAME} ({guardrail_id})")
            try:
                wait_until_ready(bedrock, guardrail_id)
                rows = measure_both(runtime, {source: (guardrail_id, "DRAFT") for source in sets})
            except BaseException:
                delete_reporting(bedrock, guardrail_id, progress)
                raise
            deleted = delete_reporting(bedrock, guardrail_id, progress)
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    summaries = {source: summarise(rows[source]) for source in rows}
    for source, title in (("INPUT", "Questions, as INPUT"), ("OUTPUT", "Answers, as OUTPUT")):
        print(f"## {title}", "", sep="\n")
        if source == "OUTPUT":
            print("Without their citations, as the function checks them.", "", sep="\n")
        print(*report(summaries[source], rows[source]), "", sep="\n")
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "guardrail": "deployed" if args.deployed else PROBE_NAME,
                    "version": version,
                    "topics_as_in_the_repository": same,
                    "topic_policy": topic_policy(),
                    "summary": summaries["INPUT"],
                    "rows": rows["INPUT"],
                    "answers": {"summary": summaries["OUTPUT"], "rows": rows["OUTPUT"]},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    # The report stands; a probe left behind still fails the run, so it is not forgotten.
    return 0 if deleted else 2


if __name__ == "__main__":
    sys.exit(main())

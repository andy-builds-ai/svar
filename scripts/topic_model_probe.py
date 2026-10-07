"""Measure the topic model through the function's own check, for `python -m scripts.topic_probe`.

Every text goes through `topic_check.check_topics` with the function's own client for the topic
model, so prompt, timeouts and verdict rules are the ones a question meets: a check that fails
or gives no readable verdict refuses the text, and counts as blocked here too, listed apart. A
throttled check is repeated after a wait, because the probe asks faster than a person does.
"""

import json
import random
import statistics
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

from infra.answer_models import answer_model
from scripts.aws import error_code
from scripts.topic_probe import (
    TEXT_FIELDS,
    THROTTLE_ATTEMPTS,
    THROTTLE_MAX_WAIT_SECONDS,
    THROTTLE_WAIT_SECONDS,
    judged_text,
)

#: The evaluation sets whose questions the topic model is measured on as well: an advice
#: question is to be refused, every other kind answered.
EVALUATION_SETS = (
    Path(__file__).with_name("evaluation-questions-control.json"),
    Path(__file__).with_name("evaluation-questions-kalvenstett.json"),
)

#: The topic of an advice question of the evaluation sets, which name no denied topic.
ADVICE = "Advice (evaluation sets)"

#: Checks at once. gpt-oss-20b answered in 0.39 s median (06.10.2026); four at once stay far
#: below what a new account may ask per minute.
DEFAULT_WORKERS = 4


def evaluation_questions(paths: Sequence[Path] = EVALUATION_SETS) -> list[dict[str, Any]]:
    """The questions of the evaluation sets as topic probe questions: advice ones to block."""
    questions = []
    for path in paths:
        for question in json.loads(path.read_text(encoding="utf-8")):
            refuse = question["kind"] == "refuse"
            questions.append(
                {
                    "id": question["id"],
                    "language": question.get("language", "en"),
                    "topic": ADVICE if refuse else question["kind"],
                    "question": question["question"],
                    "blocked": refuse,
                }
            )
    return questions


def check(
    app: Any,
    runtime: Any,
    settings: Any,
    source: str,
    text: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One text through `topic_check.check_topics`: the topics it found, whether the check failed
    and why, its tokens and seconds, and how often AWS throttled it first."""
    for attempt in range(1, THROTTLE_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            checked = app.topic_check.check_topics(runtime, settings, source, text)
        except app.errors.TopicCheckError as error:
            if error_code(error.__cause__) == "ThrottlingException" and attempt < THROTTLE_ATTEMPTS:
                ceiling = min(THROTTLE_MAX_WAIT_SECONDS, THROTTLE_WAIT_SECONDS * 2 ** (attempt - 1))
                sleep(random.uniform(ceiling / 2, ceiling))
                continue
            return {
                "topics": [],
                "failed": str(error),
                "tokens": {"input_tokens": 0, "output_tokens": 0},
                "seconds": time.perf_counter() - started,
                "throttled": attempt - 1,
            }
        return {
            "topics": [topic.removeprefix("topic:") for topic in checked["topics"]],
            "failed": None,
            "tokens": checked["tokens"],
            "seconds": time.perf_counter() - started,
            "throttled": attempt - 1,
        }
    raise AssertionError("unreachable: the last attempt returns")


def measure(
    app: Any,
    runtime: Any,
    settings: Any,
    questions: Sequence[Mapping[str, Any]],
    progress: Callable[[str], None],
    repeats: int,
    source: str = "INPUT",
    workers: int = DEFAULT_WORKERS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Every text through the topic model ``repeats`` times, rows shaped like
    `topic_probe.measure`'s, with the failed checks, tokens and seconds of each text."""
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
            "failed": [],
            "tokens": {"input_tokens": 0, "output_tokens": 0},
            "seconds": [],
        }
        for question in questions
    ]
    calls = [(run, row) for run in range(1, repeats + 1) for row in rows]
    stop = threading.Event()

    def ask(row: Mapping[str, Any]) -> dict[str, Any] | None:
        if stop.is_set():
            return None
        return check(app, runtime, settings, source, row["text"], sleep)

    throttled = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(ask, row) for _, row in calls]
        try:
            for (run, row), future in zip(calls, futures, strict=True):
                result = future.result()
                assert result is not None
                throttled += result["throttled"]
                blocked = bool(result["topics"]) or result["failed"] is not None
                row["runs"] += 1
                row["blocked_runs"] += blocked
                row["topics"] += [t for t in result["topics"] if t not in row["topics"]]
                if result["failed"] is not None:
                    row["failed"].append(result["failed"])
                for kind in row["tokens"]:
                    row["tokens"][kind] += result["tokens"][kind]
                row["seconds"].append(round(result["seconds"], 3))
                verdict = "right" if blocked == row["expected"] else "WRONG"
                if result["failed"] is not None:
                    outcome = f"failed, refused: {result['failed']}"
                else:
                    outcome = "blocked " + ", ".join(result["topics"]) if blocked else "passed"
                progress(f"run {run} {row['id']} {verdict}: {outcome}")
        except BaseException:
            stop.set()
            raise
    if throttled:
        progress(f"{source}: AWS throttled {throttled} checks; each was repeated after a wait")
    return rows


def model_summary(
    rows_by_set: Mapping[str, Sequence[Mapping[str, Any]]], model_id: str, region: str
) -> dict[str, Any]:
    """Failed checks, tokens, cost and seconds over every set of the run."""
    rows = [row for rows in rows_by_set.values() for row in rows]
    model = answer_model(model_id, region)
    tokens = {
        kind: sum(row["tokens"][kind] for row in rows) for kind in ("input_tokens", "output_tokens")
    }
    cost = (
        Decimal(tokens["input_tokens"]) * Decimal(model.input_per_1k)
        + Decimal(tokens["output_tokens"]) * Decimal(model.output_per_1k)
    ) / 1000
    seconds = sorted(second for row in rows for second in row["seconds"])
    return {
        "model": model_id,
        "region": region,
        "checks": len(seconds),
        "failed": {row["id"]: row["failed"] for row in rows if row["failed"]},
        "tokens": tokens,
        "cost_usd": str(cost),
        "median_seconds": statistics.median(seconds) if seconds else None,
        "p90_seconds": seconds[int(len(seconds) * 0.9)] if seconds else None,
        "max_seconds": seconds[-1] if seconds else None,
    }


def report_model(summary: Mapping[str, Any]) -> list[str]:
    """The model's part of the report: failed checks, cost, seconds."""
    failed = summary["failed"]
    lines = [
        f"Topic model {summary['model']} in {summary['region']}: {summary['checks']} checks, "
        f"{summary['cost_usd']} USD ({summary['tokens']['input_tokens']} input and "
        f"{summary['tokens']['output_tokens']} output tokens).",
    ]
    if summary["checks"]:
        lines.append(
            f"Seconds per check: median {summary['median_seconds']:.2f}, "
            f"90 % under {summary['p90_seconds']:.2f}, at most {summary['max_seconds']:.2f}."
        )
    lines.append(
        "Failed checks, each counted as blocked: "
        + ("; ".join(f"{name} ({', '.join(causes)})" for name, causes in failed.items()) or "none")
    )
    return lines


def main(args: Any, topic_model_id: str) -> int:
    """`python -m scripts.topic_probe` in a region whose stack checks the topics with a model:
    the topic sets and the evaluation sets' questions, ``args.repeat`` times each."""
    from types import SimpleNamespace

    from scripts.aws import AwsProblem
    from scripts.evaluate import deployed_environment, import_app
    from scripts.topic_probe import load_probe_answers, load_probe_questions, report, summarise

    app = import_app()
    sets = {
        "INPUT": load_probe_questions(args.questions),
        "OUTPUT": load_probe_answers(args.answers),
        "EVALUATION": evaluation_questions(),
    }

    def progress(line: str) -> None:
        print(line, file=sys.stderr)

    try:
        if args.deployed:
            settings = app.config.load_settings(deployed_environment(args.region))
            if not settings.topic_model_id:
                raise AwsProblem("The deployed function checks the topics with a guardrail.")
        else:
            settings = SimpleNamespace(topic_model_id=topic_model_id, region=args.region)
        # Its price is needed for the report: a model without one stops the run before it pays.
        answer_model(settings.topic_model_id, args.region)
        runtime = app.clients.client("topic-model", args.region)
        rows = {
            name: measure(
                app,
                runtime,
                settings,
                texts,
                progress,
                args.repeat,
                "OUTPUT" if name == "OUTPUT" else "INPUT",
                args.workers or DEFAULT_WORKERS,
            )
            for name, texts in sets.items()
        }
    except (AwsProblem, app.errors.ConfigurationError, KeyError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    summaries = {name: summarise(rows[name]) for name in rows}
    model = model_summary(rows, settings.topic_model_id, args.region)
    titles = {
        "INPUT": "Questions, as INPUT",
        "OUTPUT": "Answers, as OUTPUT, without their citations",
        "EVALUATION": "Questions of the evaluation sets (control, Kalvenstett), as INPUT",
    }
    for name, title in titles.items():
        # The guardrail's units line says nothing here: the model's tokens follow below.
        lines = report(summaries[name], rows[name])
        lines = [line for line in lines if not line.startswith("Units")]
        print(f"## {title}", "", *lines, "", sep="\n")
    print(*report_model(model), sep="\n")
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "topic_check": "model",
                    "deployed": args.deployed,
                    "model": model,
                    "prompt": app.topic_model.gate_prompt(),
                    "summaries": summaries,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    # A failed check counts as blocked, as in the function; a run with one does not pass,
    # or a missing login would look like a perfect run.
    return 2 if model["failed"] else 0

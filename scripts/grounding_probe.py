"""Measure the grounding check on German answers against a fixed answer set, before a deploy.

Every answer of `ANSWERS_FILE` names its workspace, its question and whether
the sources carry it. The passages come from the deployed knowledge base the
way the function retrieves them and are built into the sources text the model
call is checked against. The check runs in a guardrail of its own,
`PROBE_NAME`, a copy of the answer guardrail: the same topics on the question
while the stack sets them there (none since the second deploy of 06.10.2026),
masking, grounding and relevance thresholds, and the EU guardrail profile
where the stack of the region uses it (not in eu-central-1). Each answer is
checked as written, the way the model call checks it.

A German answer that check withholds is also checked in English: translated by
the answer model through the probe, and the translation checked on the same
sources. The function does not do that; it is the way measured and not built
on 06.10.2026, because it let through made-up answers the check withholds
today (docs/decisions.md). The probe is deleted at the end, also when the run
fails. Every answer of the set scored the same as written in two runs
(06.10.2026), so each is checked once.
"""

import argparse
import dataclasses
import json
import random
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from infra.answer_models import region_answer_guardrail_uses_profile
from infra.stacks.guardrails import (
    ANSWER_GUARDRAIL_READS_QUESTION_TOPICS,
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    GUARDRAIL_PROFILE_ID,
    GUARDRAIL_PROFILE_REGIONS,
    MASKED_PII_TYPES,
    RELEVANCE_THRESHOLD,
    grounding_threshold_for,
)
from scripts import topic_probe, utf8_output
from scripts.aws import REGION, AwsProblem, client, error_code
from scripts.evaluate import deployed_environment, import_app

ANSWERS_FILE = Path(__file__).with_name("grounding-answers.json")

#: The only guardrail this script creates or deletes.
PROBE_NAME = "andur-grounding-probe"

LANGUAGES = ("en", "de")

#: What an answer is: right, or one of three ways of not being carried by the sources.
KINDS = ("right", "wrong-value", "added-claim", "invented")

#: The answers checked once more in English when the check withholds them as written.
ENGLISH_CHECK_LANGUAGES = frozenset({"de"})

#: Each text takes a grounding check, and a German one the check withholds a model call and a
#: second check; 4 at once stay well under the 25 ApplyGuardrail calls a second AWS allows.
DEFAULT_WORKERS = 4

TRANSLATION_PROMPT = """Translate the question and the answer in the user's message into English.

Rules:
- Translate faithfully. Keep every number, unit, date, name, placeholder and source number in square brackets as it is.
- Do not add, drop, explain or correct anything.
- Reply in exactly this form and nothing else:
Question: <the question in English>
Answer: <the answer in English>"""  # noqa: E501

#: Stop reasons of a translation that is complete: masking stops it with `guardrail_intervened`.
FINISHED_TRANSLATION = frozenset({"end_turn", "guardrail_intervened"})

#: The problem `check_in_english` names for a translation it cannot use; any other problem is
#: a call that failed.
UNUSABLE_TRANSLATION = "the translation is not in the form asked for"


def load_answers(path: Path) -> list[dict[str, Any]]:
    """The answer set, checked: a right answer is grounded, every other kind is not."""
    answers = json.loads(path.read_text(encoding="utf-8"))
    seen = set()
    for answer in answers:
        name = answer["id"]
        if name in seen:
            raise ValueError(f"{name}: the id is used twice")
        seen.add(name)
        for field in ("language", "source_language"):
            if answer.get(field) not in LANGUAGES:
                raise ValueError(f"{name}: unknown {field} {answer.get(field)!r}")
        if answer.get("kind") not in KINDS:
            raise ValueError(f"{name}: unknown kind {answer.get('kind')!r}")
        if answer.get("grounded") is not (answer["kind"] == "right"):
            raise ValueError(f"{name}: a right answer is grounded, every other kind is not")
        for field in ("workspace", "question", "answer"):
            if not (isinstance(answer.get(field), str) and answer[field].strip()):
                raise ValueError(f"{name}: {field} must be a non-empty text")
    return answers


def probe_request(region: str = REGION) -> dict[str, Any]:
    """The probe guardrail: the answer guardrail's policies as the stack in ``region`` sets
    them, read on the same sides, under the probe's name; through the EU profile only where
    that stack's guardrails use it."""
    profile = region_answer_guardrail_uses_profile(region)
    topics = topic_probe.topic_policy()
    for topic in topics["topicsConfig"]:
        topic["outputEnabled"] = False
        topic["outputAction"] = "NONE"
    return {
        "name": PROBE_NAME,
        "description": "Andur: temporary copy of the answer guardrail; deleted after the run.",
        **(
            {"topicPolicyConfig": topics}
            if ANSWER_GUARDRAIL_READS_QUESTION_TOPICS and profile
            else {}
        ),
        **(
            {"crossRegionConfig": {"guardrailProfileIdentifier": GUARDRAIL_PROFILE_ID}}
            if profile
            else {}
        ),
        "sensitiveInformationPolicyConfig": {
            "piiEntitiesConfig": [
                {
                    "type": pii_type,
                    "action": "ANONYMIZE",
                    "inputEnabled": False,
                    "inputAction": "NONE",
                    "outputEnabled": True,
                    "outputAction": "ANONYMIZE",
                }
                for pii_type in MASKED_PII_TYPES
            ]
        },
        "contextualGroundingPolicyConfig": {
            "filtersConfig": [
                {
                    "type": kind,
                    "threshold": threshold,
                    "action": "BLOCK",
                    "enabled": True,
                }
                for kind, threshold in (
                    ("GROUNDING", grounding_threshold_for(region)),
                    ("RELEVANCE", RELEVANCE_THRESHOLD),
                )
            ]
        },
        "blockedInputMessaging": BLOCKED_INPUT_MESSAGE,
        "blockedOutputsMessaging": BLOCKED_OUTPUT_MESSAGE,
    }


def retrying(call: Callable[[], Any], sleep: Callable[[float], None] = time.sleep) -> Any:
    """One AWS call, asked again after a wait while AWS throttles it, as `topic_probe` does."""
    for attempt in range(1, topic_probe.THROTTLE_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:
            if error_code(exc) != "ThrottlingException" or attempt == topic_probe.THROTTLE_ATTEMPTS:
                raise
            ceiling = min(
                topic_probe.THROTTLE_MAX_WAIT_SECONDS,
                topic_probe.THROTTLE_WAIT_SECONDS * 2 ** (attempt - 1),
            )
            sleep(random.uniform(ceiling / 2, ceiling))
    raise AssertionError("unreachable: the last attempt returns or raises")


def failed_call(exc: BaseException) -> str | None:
    """What went wrong in an AWS call, for the report; None for an error that is no AWS call's,
    which ends the run. A network error has no AWS code but is botocore's."""
    if error_code(exc) is None and not type(exc).__module__.startswith("botocore"):
        return None
    return f"{error_code(exc) or type(exc).__name__}: {exc}"


def retrieve_sources(
    app: Any, agent: Any, settings: Any, answers: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, str], str]:
    """The sources text of every question, once per workspace and question, retrieved and
    built the way the function does it."""
    sources: dict[tuple[str, str], str] = {}
    for answer in answers:
        key = (answer["workspace"], answer["question"])
        if key in sources:
            continue
        found = app.retrieval.retrieve_passages(
            agent, settings.knowledge_base_id, key[1], key[0], settings.number_of_results
        )
        results, _ = app.retrieval.keep_workspace(found, key[0])
        if not results:
            raise AwsProblem(f"No passages for {key[1]!r} in the workspace {key[0]}")
        sources[key] = app.prompts.build_sources(results)
    return sources


def units_of(usage: Mapping[str, Any] | None, app: Any) -> dict[str, int]:
    """The priced units of one ApplyGuardrail response's ``usage``, 0 where it names none."""
    usage = usage or {}
    return {name: int(usage.get(name, 0)) for name in app.pricing.PRICED_UNITS}


def added(units: Mapping[str, int] | None, more: Mapping[str, int] | None) -> dict[str, int] | None:
    """Two calls' units added up, kind by kind; None when either is unknown."""
    if units is None or more is None:
        return None
    return {name: units.get(name, 0) + more.get(name, 0) for name in {*units, *more}}


def grounding_check(
    app: Any, runtime: Any, settings: Any, sources: str, question: str, answer: str
) -> dict[str, Any]:
    """The grounding check of one answer through ApplyGuardrail: the sources, query and
    answer blocks the model call gives the guardrail. A check that reports no score has not
    looked, and passes nothing."""
    response = retrying(
        lambda: runtime.apply_guardrail(
            guardrailIdentifier=settings.guardrail_id,
            guardrailVersion=settings.guardrail_version,
            source="OUTPUT",
            content=[
                {"text": {"text": sources, "qualifiers": ["grounding_source"]}},
                {"text": {"text": question, "qualifiers": ["query"]}},
                {"text": {"text": answer, "qualifiers": ["guard_content"]}},
            ],
        )
    )
    scores: dict[str, float | None] = {"grounding_score": None, "relevance_score": None}
    blocked = []
    for assessment in response.get("assessments", []):
        for check in assessment.get("contextualGroundingPolicy", {}).get("filters", []):
            kind = str(check.get("type", "")).lower()
            if f"{kind}_score" in scores and isinstance(check.get("score"), (int, float)):
                scores[f"{kind}_score"] = round(check["score"], 4)
            if check.get("action") == "BLOCKED":
                blocked.append(kind)
    passed = not blocked and None not in scores.values()
    return {"passed": passed, **scores, "units": units_of(response.get("usage"), app)}


def translate_to_english(
    app: Any, runtime: Any, settings: Any, question: str, answer: str
) -> dict[str, Any]:
    """The question and the answer in English: one call of the answer model through the
    guardrail, which reads the topics in it, if it has them, and masks the reply. The call
    carries no sources, so the guardrail checks no grounding."""
    response = retrying(
        lambda: runtime.converse(
            modelId=settings.model_id,
            system=[{"text": TRANSLATION_PROMPT}],
            messages=[
                {"role": "user", "content": [{"text": f"Question: {question}\nAnswer: {answer}"}]}
            ],
            inferenceConfig={
                "maxTokens": app.llm.MAX_ANSWER_TOKENS,
                "temperature": app.llm.ANSWER_TEMPERATURE,
            },
            guardrailConfig={
                "guardrailIdentifier": settings.guardrail_id,
                "guardrailVersion": settings.guardrail_version,
                "trace": "enabled",
            },
        )
    )
    guardrail = app.guardrails.read_guardrail(response)
    question_part, separator, answer_part = (app.llm.model_text(response) or "").partition(
        "\nAnswer:"
    )
    usable = (
        guardrail["action"] != "blocked"
        and response.get("stopReason") in FINISHED_TRANSLATION
        and question_part.startswith("Question:")
        and bool(question_part.removeprefix("Question:").strip())
        and bool(separator)
        and "\nAnswer:" not in answer_part
        and bool(answer_part.strip())
    )
    return {
        "question": question_part.removeprefix("Question:").strip() if usable else None,
        "answer": answer_part.strip() if usable else None,
        "usage": app.llm.read_usage(response),
        "units": guardrail["units"],
    }


def check_in_english(
    app: Any, runtime: Any, settings: Any, question: str, answer: str, sources: str
) -> dict[str, Any]:
    """Whether the English translation of an answer passes the grounding check on the same
    sources, with the tokens and units both calls took. A translation that is not in the form
    asked for passes nothing; a failed call is named as the problem."""
    result: dict[str, Any] = {
        "passed": False,
        "grounding_score": None,
        "relevance_score": None,
        "translation": None,
        "problem": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "units": {name: 0 for name in app.pricing.PRICED_UNITS},
    }
    try:
        translation = translate_to_english(app, runtime, settings, question, answer)
        result.update(
            usage=translation["usage"],
            units=translation["units"],
            translation=translation["answer"],
        )
        if translation["answer"] is None:
            result["problem"] = UNUSABLE_TRANSLATION
            return result
        check = grounding_check(
            app, runtime, settings, sources, translation["question"], translation["answer"]
        )
    except Exception as exc:
        problem = failed_call(exc)
        if problem is None:
            raise
        result["problem"] = problem
        return result
    result.update(
        passed=check["passed"],
        grounding_score=check["grounding_score"],
        relevance_score=check["relevance_score"],
        units=added(result["units"], check["units"]),
    )
    return result


def measure_one(
    app: Any, runtime: Any, settings: Any, answer: Mapping[str, Any], sources: str
) -> dict[str, Any]:
    """One answer as written, and a German one the check withholds in English too. A failed
    call is named in ``error`` and the answer counted as withheld."""
    try:
        written = grounding_check(
            app, runtime, settings, sources, answer["question"], answer["answer"]
        )
        error = None
    except Exception as exc:
        error = failed_call(exc)
        if error is None:
            raise
        written = {"passed": False, "grounding_score": None, "relevance_score": None, "units": None}
    row: dict[str, Any] = {
        "id": answer["id"],
        "language": answer["language"],
        "source_language": answer["source_language"],
        "kind": answer["kind"],
        "grounded": answer["grounded"],
        "answer": answer["answer"],
        "as_written": written["passed"],
        "with_english": written["passed"],
        "grounding_score": written["grounding_score"],
        "relevance_score": written["relevance_score"],
        "english": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "units": written["units"],
        "error": error,
    }
    if error is None and not written["passed"] and answer["language"] in ENGLISH_CHECK_LANGUAGES:
        started = time.perf_counter()
        english = check_in_english(
            app, runtime, settings, answer["question"], answer["answer"], sources
        )
        row["english"] = {
            "passed": english["passed"],
            "grounding_score": english["grounding_score"],
            "relevance_score": english["relevance_score"],
            "translation": english["translation"],
            "problem": english["problem"],
            "seconds": round(time.perf_counter() - started, 2),
        }
        row["with_english"] = english["passed"]
        row["usage"] = english["usage"]
        row["units"] = added(written["units"], english["units"])
        if english["problem"] not in (None, UNUSABLE_TRANSLATION):
            row["error"] = english["problem"]
    row["cost_usd"] = app.pricing.answer_cost(settings.prices, row["usage"], row["units"])
    return row


def measure(
    app: Any,
    runtime: Any,
    settings: Any,
    answers: Sequence[Mapping[str, Any]],
    sources: Mapping[tuple[str, str], str],
    progress: Callable[[str], None],
    workers: int = DEFAULT_WORKERS,
) -> list[dict[str, Any]]:
    """Every answer through `measure_one`, up to ``workers`` at once, reported in set order."""

    def one(answer: Mapping[str, Any]) -> dict[str, Any]:
        key = (answer["workspace"], answer["question"])
        return measure_one(app, runtime, settings, answer, sources[key])

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pool.map(one, answers):
            rows.append(row)
            verdict = "passed" if row["as_written"] else "withheld"
            right = "right" if row["as_written"] == row["grounded"] else "WRONG"
            english = ""
            if row["english"]:
                english = f"; in English {'passed' if row['with_english'] else 'withheld'}"
            progress(f"{row['id']} {right}: {verdict}{english}")
    return rows


#: The groups of the report: answer language, source language, whether the sources carry it.
GROUPS = (
    ("de", "en", True),
    ("de", "de", True),
    ("de", "en", False),
    ("en", "en", False),
    ("de", "de", False),
)


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Passed per group, as written and with the English check; withheld made-up answers per
    kind; cost and the time of the English checks."""
    groups = {}
    for language, source_language, grounded in GROUPS:
        group = [
            r
            for r in rows
            if (r["language"], r["source_language"], r["grounded"])
            == (language, source_language, grounded)
        ]
        if not group:
            continue
        name = f"{'right' if grounded else 'made up'}, {language} on {source_language} sources"
        groups[name] = {
            "answers": len(group),
            "passed_as_written": sum(r["as_written"] for r in group),
            "passed_with_english": sum(r["with_english"] for r in group),
            "checked_in_english": sum(r["english"] is not None for r in group),
            "translation_unusable": sum(
                bool(r["english"]) and r["english"]["problem"] == UNUSABLE_TRANSLATION
                for r in group
            ),
            "by_kind": {
                kind: {
                    "answers": len(of_kind),
                    "withheld_as_written": sum(not r["as_written"] for r in of_kind),
                    "withheld_with_english": sum(not r["with_english"] for r in of_kind),
                }
                for kind in KINDS[1:]
                if (of_kind := [r for r in group if r["kind"] == kind])
            },
        }
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    english = [r["english"]["seconds"] for r in rows if r["english"]]
    return {
        "groups": groups,
        "errors": [r["id"] for r in rows if r["error"]],
        "cost_usd": round(sum(costs), 6),
        "cost_unknown": len(rows) - len(costs),
        "english_checks": len(english),
        "english_check_seconds": {
            "median": round(statistics.median(english), 2) if english else None,
            "max": max(english, default=None),
        },
    }


def report(summary: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The run as Markdown: the groups, the kinds, every answer judged wrong as written or
    with the English check, cost and time."""
    lines = [
        "| Answers | Count | Passed as written | Passed with the English check "
        "| Checked in English | Translation unusable |",
        "|---|---|---|---|---|---|",
        *(
            f"| {name} | {g['answers']} | {g['passed_as_written']} | {g['passed_with_english']} "
            f"| {g['checked_in_english']} | {g['translation_unusable']} |"
            for name, g in summary["groups"].items()
        ),
        "",
        "| Made up (kind) | Count | Withheld as written | Withheld with the English check |",
        "|---|---|---|---|",
        *(
            f"| {name}, {kind} | {k['answers']} | {k['withheld_as_written']} "
            f"| {k['withheld_with_english']} |"
            for name, g in summary["groups"].items()
            for kind, k in g["by_kind"].items()
        ),
    ]
    wrong = [r for r in rows if r["grounded"] not in (r["as_written"], r["with_english"])]
    wrong += [r for r in rows if r["as_written"] != r["with_english"] and r not in wrong]
    if wrong:
        lines += [
            "",
            "| Id | Expected | As written | With English | Scores as written "
            "| Scores in English | Answer | English |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in wrong:
            english = r["english"] or {}
            lines.append(
                f"| {r['id']} | {'passed' if r['grounded'] else 'withheld'} "
                f"| {'passed' if r['as_written'] else 'withheld'} "
                f"| {'passed' if r['with_english'] else 'withheld'} "
                f"| {r['grounding_score']} / {r['relevance_score']} "
                f"| {english.get('grounding_score')} / {english.get('relevance_score')} "
                f"| {r['answer']} | {english.get('translation') or '-'} |"
            )
    if summary["errors"]:
        lines += ["", f"Failed calls, counted as withheld: {', '.join(summary['errors'])}"]
    seconds = summary["english_check_seconds"]
    lines += [
        "",
        f"English checks: {summary['english_checks']}, median {seconds['median']} s, "
        f"longest {seconds['max']} s. Cost: {summary['cost_usd']} USD at the deployed prices"
        + (f", {summary['cost_unknown']} answers unknown." if summary["cost_unknown"] else "."),
    ]
    return lines


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.grounding_probe",
        description="Measure the grounding check on the answer set, and German answers in English.",
    )
    parser.add_argument("--answers", type=Path, default=ANSWERS_FILE)
    parser.add_argument("--out", type=Path, help="write every answer's result as JSON here")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"answers measured at once (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)
    try:
        profile = region_answer_guardrail_uses_profile(args.region)
    except KeyError as problem:
        print(f"error: {problem.args[0]}", file=sys.stderr)
        return 2
    if profile and args.region not in GUARDRAIL_PROFILE_REGIONS:
        print(
            f"error: {args.region} is not a source region of {GUARDRAIL_PROFILE_ID}",
            file=sys.stderr,
        )
        return 2
    answers = load_answers(args.answers)
    app = import_app()

    def progress(line: str) -> None:
        print(line, file=sys.stderr)

    try:
        deployed = app.config.load_settings(deployed_environment(args.region))
        sources = retrieve_sources(
            app, client("bedrock-agent-runtime", args.region), deployed, answers
        )
        bedrock = client("bedrock", args.region)
        runtime = client("bedrock-runtime", args.region)
        guardrail_id = topic_probe.create_probe(bedrock, probe_request(args.region))
        progress(f"created {PROBE_NAME} ({guardrail_id})")
        try:
            topic_probe.wait_until_ready(bedrock, guardrail_id, name=PROBE_NAME)
            settings = dataclasses.replace(
                deployed, guardrail_id=guardrail_id, guardrail_version="DRAFT"
            )
            rows = measure(app, runtime, settings, answers, sources, progress, args.workers)
        except BaseException:
            topic_probe.delete_reporting(bedrock, guardrail_id, progress, PROBE_NAME)
            raise
        deleted = topic_probe.delete_reporting(bedrock, guardrail_id, progress, PROBE_NAME)
    except (AwsProblem, app.errors.HandlerError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    summary = summarise(rows)
    print(*report(summary, rows), sep="\n")
    if args.out:
        args.out.write_text(
            json.dumps(
                {"guardrail": probe_request(args.region), "summary": summary, "rows": rows},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    # The report stands; a failed call or a probe left behind still fails the run.
    return 0 if deleted and not summary["errors"] else 2


if __name__ == "__main__":
    sys.exit(main())

"""Ask every evaluation question of every model the way the deployed function does.

The questions run through `answer_question` in app/answering.py — retrieval, the
prompt, `converse` with the guardrail, the excerpt guardrail and the cost line
— with the deployed function's own settings. Only the model and its token
prices change per model; the guardrail prices stay the deployed ones. No key
and no budget are involved, so a run costs model and guardrail calls only.
"""

import argparse
import dataclasses
import importlib
import json
import pkgutil
import re
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from infra.answer_models import ANSWER_MODELS, PRICE_DATE, AnswerModel, region_models
from scripts import utf8_output
from scripts.aws import REGION, AwsProblem, client, explained
from scripts.layout import document_of

#: app/ holds the handler under the plain names Lambda imports, `handler` and `aws`.
APP_FOLDER = Path(__file__).resolve().parents[1] / "app"

QUESTIONS_FILE = Path(__file__).with_name("evaluation-questions.json")

#: `ANSWER_FUNCTION_NAME` in the stack; its environment is the configuration.
FUNCTION_NAME = "andur-answer"

KINDS = ("answer", "refuse", "unanswerable")

#: The function's `LANGUAGES` (app/sentences.py). A question asks in one of them, English
#: unless it says otherwise, the way the page sends its language with every question.
LANGUAGES = ("en", "de")

#: Only a model in the region or an EU profile keeps the data in the EU.
ALLOWED_PROFILE_PREFIXES = ("eu.",)


MODELS = ANSWER_MODELS

#: A question the function still answers with "busy" is asked again after these
#: pauses, in seconds — on top of the function's own second model call.
RETRY_PAUSES = (10, 30, 60)


def stays_in_the_eu(model_id: str) -> bool:
    """A plain model id runs in the region; a profile id only if it is an EU profile."""
    prefix = model_id.split(".", 1)[0] + "."
    is_profile = prefix in {"eu.", "us.", "apac.", "global.", "jp.", "au.", "ca.", "us-gov."}
    return not is_profile or prefix in ALLOWED_PROFILE_PREFIXES


def texts(value: Any) -> bool:
    """True for a non-empty list of non-empty strings."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
    )


def load_questions(path: Path) -> list[dict[str, Any]]:
    """The question set, checked for the fields the scoring reads and their shape."""
    questions = json.loads(path.read_text(encoding="utf-8"))
    for question in questions:
        name, kind = question["id"], question["kind"]
        if kind not in KINDS:
            raise ValueError(f"{name}: unknown kind {kind!r}")
        if question.get("language", "en") not in LANGUAGES:
            raise ValueError(f"{name}: unknown language {question['language']!r}")
        for field in ("workspace", "question"):
            if not (isinstance(question[field], str) and question[field].strip()):
                raise ValueError(f"{name}: {field} must be a non-empty text")
        documents, facts = question["documents"], question["facts"]
        if kind != "answer":
            if documents != [] or facts != []:
                raise ValueError(f"{name}: a {kind} question has no documents and no facts")
            continue
        if not texts(documents):
            raise ValueError(f"{name}: documents must be a list of document names")
        if not (isinstance(facts, list) and facts and all(texts(group) for group in facts)):
            raise ValueError(f"{name}: facts must be a list of lists of spellings")
    return questions


#: Before a number: no digit and point or comma, as in "1.500" before "500".
_NOT_AFTER_A_DIGIT_GROUP = r"(?<!\d[.,])"

#: After a number: no point or comma with a digit behind it, other than one or two zeros
#: that end there.
_NO_DIGITS_BEHIND = r"(?![.,](?!0{1,2}(?!\d))\d)"


def contains(text: str, fact: str) -> bool:
    """Whether ``fact`` stands in ``text`` on its own: "12" not inside "120", "12,5" or
    "1.012", "4:30" not inside "14:30", "den" not inside "Böden". A number next to a point or
    comma and a digit is part of a longer one, unless one or two zeros are all that follows:
    "139" stands in "139,00", not in "139,50" and not in "139.000". A day and month inside a
    date written with points count as part of it too ("27.10" not in "27.10.2026"), so a
    question gives such a date whole as well. Both are compared as `plain` text."""
    fact = plain(fact)
    before = r"(?<!\w)" + (_NOT_AFTER_A_DIGIT_GROUP if fact[:1].isdigit() else "")
    after = r"(?!\w)" + (_NO_DIGITS_BEHIND if fact[-1:].isdigit() else "")
    return re.search(before + re.escape(fact) + after, plain(text)) is not None


def plain(text: str) -> str:
    """Lowercase, without Markdown emphasis, with every kind of space as one space."""
    text = re.sub(r"[*_`]", "", text).replace(" ", " ").replace(" ", " ")
    return " ".join(text.split()).lower()


def declined(body: Mapping[str, Any], app: Any) -> bool:
    """True when Andur gave no answer: the guardrail blocked, or no source carried one, in
    either language."""
    if body["guardrail"]["action"] == "blocked":
        return True
    sentences = {
        *app.sentences.NOT_IN_SOURCES_ANSWERS.values(),
        *app.sentences.NO_SOURCES_ANSWERS.values(),
    }
    return body["answer"].strip() in sentences


def score(question: Mapping[str, Any], body: Mapping[str, Any], app: Any) -> dict[str, bool]:
    """Whether the answer is right, and — for an answer question — whether it cites one of
    the documents that carry the answer."""
    if question["kind"] != "answer":
        return {"correct": declined(body, app), "cites_document": False}
    correct = not declined(body, app) and all(
        any(contains(body["answer"], alternative) for alternative in group)
        for group in question["facts"]
    )
    cites = any(
        source["cited"] and document_of(source["file"]) in question["documents"]
        for source in body["sources"]
    )
    return {"correct": correct, "cites_document": cites}


def settings_for(base: Any, model: AnswerModel, price_date: str = PRICE_DATE) -> Any:
    """The deployed settings with this model and its token prices, read on ``price_date``."""
    prices = dataclasses.replace(
        base.prices,
        input_tokens=Decimal(model.input_per_1k),
        output_tokens=Decimal(model.output_per_1k),
    )
    return dataclasses.replace(base, model_id=model.model_id, prices=prices, price_date=price_date)


def ask(app: Any, settings: Any, question: Mapping[str, Any]) -> dict[str, Any]:
    """One question through the handler's path, asked again while it comes back busy.

    `rejected` counts every model call AWS turned away, the function's second
    call included; `first_call_rejected` says whether the very first one was.
    """
    rejected, first_call_rejected = 0, None
    for attempt, pause in enumerate((*RETRY_PAUSES, None), start=1):
        record = app.log.RequestRecord(language=question.get("language", "en"))
        started = time.perf_counter()
        error = None
        try:
            body = app.answering.answer_question(
                settings, question["workspace"], question["question"], record
            )
        except app.errors.HandlerError as failed:
            error = failed
            outcome = {"error": str(failed)}
        else:
            outcome = {"body": body}
        rejected += record.model_rejections
        if first_call_rejected is None:
            first_call_rejected = record.model_rejections > 0
        outcome.update(
            seconds=time.perf_counter() - started,
            rejected=rejected,
            first_call_rejected=first_call_rejected,
            attempts=attempt,
        )
        if error is not None and turned_away(app, error) and pause is not None:
            time.sleep(pause)
            continue
        return outcome
    raise AssertionError("unreachable")


def turned_away(app: Any, error: BaseException) -> bool:
    """True when the function failed because AWS turned the model call away."""
    return app.aws.error_code(error.__cause__) in app.llm.MODEL_REJECTION_CODES


def run_model(
    app: Any,
    base: Any,
    model: AnswerModel,
    questions: Sequence[Mapping[str, Any]],
    progress: Callable[[str], None],
) -> list[dict[str, Any]]:
    """Every question for one model, each with its outcome and score, priced on the date of
    ``base``."""
    settings = settings_for(base, model, base.price_date)
    results = []
    for question in questions:
        outcome = ask(app, settings, question)
        body = outcome.get("body")
        marks = score(question, body, app) if body else {"correct": False, "cites_document": False}
        results.append({"id": question["id"], "kind": question["kind"], **outcome, **marks})
        progress(f"{model.name} {question['id']}: {'right' if marks['correct'] else 'wrong'}")
    return results


def blocked_by_guardrail(result: Mapping[str, Any]) -> bool:
    """True when the guardrail itself blocked: the proof a refusal needs, which a model's
    "the sources do not answer this" is not."""
    return (result.get("body") or {}).get("guardrail", {}).get("action") == "blocked"


def summarise(model: AnswerModel, results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Hit rate, citation rate, cost and time of one model's run."""
    answers = [r for r in results if r["kind"] == "answer"]
    costs = [r["body"]["cost_usd"] for r in results if r.get("body")]
    known = [cost for cost in costs if cost is not None]
    by_kind = {
        kind: (
            sum(r["correct"] for r in results if r["kind"] == kind),
            sum(r["kind"] == kind for r in results),
        )
        for kind in KINDS
    }
    return {
        "model": model.name,
        "model_id": model.model_id,
        "correct": sum(r["correct"] for r in results),
        "correct_first_try": sum(r["correct"] and r["attempts"] == 1 for r in results),
        "rejected_first_try": sum(r["first_call_rejected"] for r in results),
        "questions": len(results),
        "by_kind": by_kind,
        "refusals_blocked": sum(blocked_by_guardrail(r) for r in results if r["kind"] == "refuse"),
        "cites_document": sum(r["cites_document"] for r in answers),
        "answer_questions": len(answers),
        "errors": sum("error" in r for r in results),
        "cost_per_question": sum(known) / len(known) if known else None,
        "cost_unknown": len(costs) - len(known),
        "median_seconds": statistics.median(r["seconds"] for r in results),
    }


def table(summaries: Sequence[Mapping[str, Any]]) -> list[str]:
    """The comparison as a Markdown table."""
    lines = [
        "| Model | Right | Right on the first try | Rejected on the first try | Answered right "
        "| Refused right | Blocked by guardrail | Declined right | Cites the document "
        "| USD per question | Median seconds | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        answer, refuse, unanswerable = (s["by_kind"][kind] for kind in KINDS)
        cost = "unknown" if s["cost_per_question"] is None else f"{s['cost_per_question']:.6f}"
        lines.append(
            f"| {s['model']} | {s['correct']}/{s['questions']} "
            f"| {s['correct_first_try']}/{s['questions']} "
            f"| {s['rejected_first_try']}/{s['questions']} | {answer[0]}/{answer[1]} "
            f"| {refuse[0]}/{refuse[1]} | {s['refusals_blocked']}/{refuse[1]} "
            f"| {unanswerable[0]}/{unanswerable[1]} "
            f"| {s['cites_document']}/{s['answer_questions']} | {cost} "
            f"| {s['median_seconds']:.1f} | {s['errors']} |"
        )
    return lines


def import_app() -> SimpleNamespace:
    """Every module of the answer function in app/, under the name Lambda gives it, as one
    namespace: ``app.answering.answer_question`` is the function's own path for a question."""
    if str(APP_FOLDER) not in sys.path:
        sys.path.insert(0, str(APP_FOLDER))
    names = [module.name for module in pkgutil.iter_modules([str(APP_FOLDER)])]
    return SimpleNamespace(**{name: importlib.import_module(name) for name in names})


def deployed_environment(region: str) -> dict[str, str]:
    """The answer function's environment: knowledge base, guardrails, prices."""
    lambda_client = client("lambda", region)
    with explained(f"Reading the configuration of {FUNCTION_NAME}"):
        configuration = lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME)
    variables = configuration.get("Environment", {}).get("Variables")
    if not variables:
        raise AwsProblem(f"{FUNCTION_NAME} has no environment. Is the stack deployed?")
    return variables


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.evaluate",
        description="Compare answer models on the evaluation questions, the way Andur answers.",
    )
    parser.add_argument(
        "--model", action="append", help="a model name from MODELS; default: all of them"
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
    parser.add_argument("--out", type=Path, help="write every answer and score as JSON here")
    parser.add_argument(
        "--region",
        default=REGION,
        help=f"the deployed stack; its models and prices come from its region; default: {REGION}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)
    try:
        regional = region_models(args.region)
    except KeyError as problem:
        print(f"error: {problem.args[0]}", file=sys.stderr)
        return 2
    known = regional.models
    models = [m for m in known if not args.model or m.name in args.model]
    unknown = set(args.model or []) - {m.name for m in known}
    if unknown or not models:
        print(f"error: unknown model {sorted(unknown)}; known: {[m.name for m in known]}")
        return 2
    outside = [m.model_id for m in models if not stays_in_the_eu(m.model_id)]
    if outside:
        print(f"error: these would leave the EU: {outside}", file=sys.stderr)
        return 2
    questions = load_questions(args.questions)
    if args.out:
        # A bad path fails now, not after paying; an earlier result there stays as it is.
        try:
            with args.out.open("a", encoding="utf-8"):
                pass
        except OSError as problem:
            print(f"error: {args.out} cannot be written: {problem}", file=sys.stderr)
            return 2
    app = import_app()
    try:
        base = app.config.load_settings(deployed_environment(args.region))
    except (AwsProblem, app.errors.ConfigurationError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    base = dataclasses.replace(base, price_date=regional.price_date)

    runs, summaries = {}, []
    for model in models:
        runs[model.name] = run_model(
            app, base, model, questions, lambda line: print(line, file=sys.stderr)
        )
        summaries.append(summarise(model, runs[model.name]))
        if args.out:
            args.out.write_text(
                json.dumps({"summaries": summaries, "runs": runs}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    for line in table(summaries):
        print(line)
    failed_calls = sum(summary["errors"] for summary in summaries)
    if failed_calls:
        # The report stands; a failed call still fails the run.
        print(f"error: {failed_calls} failed call(s), counted as wrong", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

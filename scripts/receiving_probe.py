"""Which model checks a delivery note against its purchase order cleanly, with tools.

Vard is to take over the receiving check: fetch the delivery note, fetch the
purchase order it names, compare them and report to a person, who decides. Each case in
`receiving-cases.json` runs through Vard's loop (`vard/engine.py`) with the receiving
responsibility (`vard/receiving.py`); the documents come from the case, nothing goes to a real
system. Measured per model and case: every discrepancy found, none made up, the right closing
tool, the invalid tool calls, the steps, the cost and the time; with ``--repeat`` each case runs
that many times. The models are ranked by the cases passed, then by the discrepancies missed, then
by the cost. No guardrail, no knowledge base:
only the model, in ``--region`` or behind an EU profile, with the models and prices
`infra/answer_models.py` gives that region. ``--max-usd`` is a hard limit: a call that could take
the run past it is not made.
"""

import argparse
import base64
import dataclasses
import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from infra.answer_models import FRANKFURT_MODELS, AnswerModel, region_models
from scripts import utf8_output
from scripts.aws import AwsProblem, error_code, explain, is_login_problem
from scripts.evaluate import stays_in_the_eu
from scripts.tool_probe import REFUSED_AS_SENT, model_client
from vard import engine, receiving

CASES_FILE = Path(__file__).with_name("receiving-cases.json")

DEFAULT_REGION = "eu-central-1"

MODELS = FRANKFURT_MODELS

#: A report of three discrepancies, and the reasoning a model writes before it over a note of 40
#: positions, must fit. No reply of the twelve short cases was cut off at 2,048 (20.10.2026).
MAX_TOKENS = 4096

#: About twice what one round of all eleven models is reckoned to cost on the 24 cases: 0.49 USD
#: measured for the first twelve (20.10.2026), times 2.29, the request bytes of all 24 cases
#: against those twelve on a run against a stub. The paid run sets its own with --max-usd.
DEFAULT_MAX_USD = Decimal("2.25")

#: Added to a request's size for the model's chat template around it.
TEMPLATE_TOKENS = 500


class CostLimitReached(Exception):
    """The next model call could take the run past --max-usd, so it was not made."""


def number(text: Any) -> str:
    """A document or article number as compared: no spaces, capitals."""
    return "".join(str(text).split()).upper()


def texts(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)


def load_cases(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """The companies and the cases, checked for what the loop and the judging read."""
    data = json.loads(path.read_text(encoding="utf-8"))
    companies, cases = data["companies"], data["cases"]
    if not cases:
        raise ValueError("the file holds no cases")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"two cases share an id: {ids}")
    for case in cases:
        name, note = case["id"], case["delivery_note"]
        if case["company"] not in companies:
            raise ValueError(f"{name}: unknown company {case['company']!r}")
        earlier = case.get("earlier_deliveries", [])
        if not (
            isinstance(earlier, list)
            and all(
                isinstance(e, dict) and e.get("order_number") == note["order_number"]
                for e in earlier
            )
        ):
            raise ValueError(f"{name}: an earlier delivery is a note of the same purchase order")
        notes = [note, *earlier]
        numbers = [n.get("note_number") for n in notes]
        numbers += [o.get("order_number") for o in case["purchase_orders"]]
        if not (all(isinstance(n, str) and n for n in numbers) and "order_number" in note):
            raise ValueError(f"{name}: every document carries its number, a note its order's")
        if len({number(n["note_number"]) for n in notes}) != len(notes):
            raise ValueError(f"{name}: two delivery notes share a number")
        if note["note_number"] not in case["message"]:
            raise ValueError(f"{name}: the message must name the delivery note")
        for document in (*notes, *case["purchase_orders"]):
            positions = document["positions"]
            if not (
                isinstance(positions, list)
                and positions
                and all(
                    isinstance(p, dict)
                    and texts([p.get("item")])
                    and (p.get("supplier_item") is None or texts([p["supplier_item"]]))
                    for p in positions
                )
            ):
                raise ValueError(f"{name}: every document lists its positions, each its item")
        if case["expect"] not in receiving.CLOSING:
            raise ValueError(f"{name}: expect is one of {', '.join(receiving.CLOSING)}")
        findings = case["findings"]
        if not (
            isinstance(findings, list)
            and all(
                texts(f.get("items"))
                and texts(f.get("kinds"))
                and set(f["kinds"]) <= set(receiving.KINDS)
                for f in findings
            )
        ):
            raise ValueError(f"{name}: each finding lists its article numbers and its kinds")
        if bool(findings) != (case["expect"] == "report_discrepancies"):
            raise ValueError(f"{name}: findings belong to report_discrepancies, and only there")
    return companies, cases


def finder(documents: Sequence[Mapping[str, Any]], key: str) -> receiving.Find:
    index = {number(document[key]): document for document in documents}
    return lambda wanted: index.get(number(wanted))


def case_responsibility(
    companies: Mapping[str, str], case: Mapping[str, Any]
) -> engine.Responsibility:
    """The business sees its own documents only: its orders, the note and the notes before it."""
    earlier = case.get("earlier_deliveries", [])
    by_order: dict[str, list[Mapping[str, Any]]] = {}
    for note in earlier:
        by_order.setdefault(number(note["order_number"]), []).append(note)
    return receiving.responsibility(
        companies[case["company"]],
        find_order=finder(case["purchase_orders"], "order_number"),
        find_note=finder([case["delivery_note"], *earlier], "note_number"),
        find_earlier=lambda wanted: by_order.get(number(wanted), []),
    )


def tokens_cost(model: AnswerModel, usage: Mapping[str, Any]) -> Decimal:
    return (
        Decimal(usage.get("inputTokens", 0)) * Decimal(model.input_per_1k)
        + Decimal(usage.get("outputTokens", 0)) * Decimal(model.output_per_1k)
    ) / 1000


def wire(value: Any) -> str:
    """Bytes in a request, as redacted reasoning is, the way they travel: in base64."""
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"{type(value).__name__} cannot be sent")


def call_ceiling(model: AnswerModel, request: Mapping[str, Any]) -> Decimal:
    """The most a call can cost: a token is never shorter than a byte, so the request's UTF-8
    size plus the template bounds the input, and maxTokens the output."""
    size = len(json.dumps(request, ensure_ascii=False, default=wire).encode("utf-8"))
    return (
        (size + TEMPLATE_TOKENS) * Decimal(model.input_per_1k)
        + request["inferenceConfig"]["maxTokens"] * Decimal(model.output_per_1k)
    ) / 1000


class Meter:
    """The client the loop calls. A call is made only while the run stays within the limit even
    if the call costs its most; what it cost is counted from its usage. A call that fails
    keeps its ceiling counted, since AWS may have billed it (a timeout after the model ran)."""

    def __init__(self, runtime: Any, limit: Decimal) -> None:
        self.runtime, self.limit = runtime, limit
        self.spent = self.reserved = Decimal(0)
        self.calls = 0
        self.model: AnswerModel = MODELS[0]

    def converse(self, **request: Any) -> Any:
        ceiling = call_ceiling(self.model, request)
        if self.spent + self.reserved + ceiling > self.limit:
            raise CostLimitReached(
                f"the next call of {self.model.name} could cost {ceiling:.4f} USD, and "
                f"{self.spent + self.reserved:.4f} of {self.limit} USD are taken"
            )
        self.reserved += ceiling
        response = self.runtime.converse(**request)
        self.reserved -= ceiling
        self.calls += 1
        self.spent += tokens_cost(self.model, response.get("usage", {}))
        return response


def reported(closing: Sequence[engine.Call]) -> set[tuple[str, str]]:
    """Article number and kind of every discrepancy reported; one that cannot be read counts
    with what can be read of it, so it is made up rather than lost."""
    found = set()
    for call in closing:
        if call.name != "report_discrepancies":
            continue
        findings = call.input.get("findings")
        if isinstance(findings, str):
            try:
                findings = json.loads(findings)
            except json.JSONDecodeError:
                findings = [findings]
        for finding in findings if isinstance(findings, list) else [findings]:
            if isinstance(finding, dict):
                found.add((number(finding.get("item", "")), str(finding.get("kind", ""))))
            else:
                found.add((number(finding), "?"))
    return found


def matches(expected: Mapping[str, Any], given: tuple[str, str]) -> bool:
    return (
        given[0] in {number(item) for item in expected["items"]} and given[1] in expected["kinds"]
    )


def items_of(*documents: Mapping[str, Any]) -> set[str]:
    """Every article number of the documents, the supplier's beside ours where both are given."""
    return {
        number(p[key])
        for document in documents
        for p in document["positions"]
        for key in ("item", "supplier_item")
        if p.get(key)
    }


def meeting(
    case: Mapping[str, Any], expected: Mapping[str, Any], given: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """The reported discrepancies that meet an expected one; none when it is missed. A wrong
    article is also met by its two halves together: the ordered one reported missing and the
    delivered one extra, which shows a person both articles all the same (the operator, 20.10.2026).
    Which number was ordered and which delivered, the documents of the case say."""
    met = {g for g in given if matches(expected, g)}
    if "wrong_item" in expected["kinds"]:
        ordered = items_of(*case["purchase_orders"])
        delivered = items_of(case["delivery_note"])
        mine = {number(item) for item in expected["items"]}
        ordered_only, delivered_only = mine & (ordered - delivered), mine & (delivered - ordered)
        missing = {g for g in given if g[1] == "missing_item" and g[0] in ordered_only}
        extra = {g for g in given if g[1] == "extra_item" and g[0] in delivered_only}
        if missing and extra:
            met |= missing | extra
    return met


#: Bedrock's stopReason of a reply cut off at maxTokens.
CUT_OFF = "max_tokens"

ENDINGS = {
    engine.ANSWERED: "answered without a closing tool",
    engine.STEP_LIMIT: f"reached the step limit of {receiving.MAX_STEPS}",
}


def judge(case: Mapping[str, Any], outcome: engine.Outcome) -> dict[str, Any]:
    """Whether the check closed rightly, found every discrepancy and made none up."""
    closing = outcome.closing
    names = [call.name for call in closing]
    closed_right = names == [case["expect"]]
    given = reported(closing)
    met = [meeting(case, e, given) for e in case["findings"]]
    missed = [e for e, by in zip(case["findings"], met, strict=True) if not by]
    made_up = sorted(given - set().union(*met))
    invalid = [call.name for call in outcome.invalid]
    why = []
    if names and not closed_right:
        why.append(f"closed with {' and '.join(names)}")
    elif not names:
        cut_off = outcome.ended == engine.ANSWERED and outcome.stop_reason == CUT_OFF
        why.append(
            f"cut off at {MAX_TOKENS} tokens without a closing tool"
            if cut_off
            else ENDINGS[outcome.ended]
        )
    if missed:
        why.append("missed " + ", ".join(f"{e['items'][0]} {e['kinds'][0]}" for e in missed))
    if made_up:
        why.append("made up " + ", ".join(f"{item or '?'} {kind}" for item, kind in made_up))
    if invalid:
        why.append(f"{len(invalid)} invalid calls ({', '.join(invalid)})")
    return {
        "passed": not why,
        "why": "; ".join(why),
        "closed_right": closed_right,
        "found_all": not missed,
        "missed": len(missed),
        "none_made_up": not made_up,
        "invalid_calls": len(invalid),
    }


def run_case(
    meter: Meter,
    model: AnswerModel,
    companies: Mapping[str, str],
    case: Mapping[str, Any],
    label: str,
    keep_reasoning: bool = False,
) -> dict[str, Any]:
    """The marks of one case. `CostLimitReached`, a missing login and a missing permission
    stop the run; any other failed call, and a tool that failed, is a row."""
    try:
        outcome = engine.run(
            meter,
            model.model_id,
            case_responsibility(companies, case),
            case["message"],
            max_tokens=MAX_TOKENS,
            keep_reasoning=keep_reasoning,
        )
    except Exception as exc:  # noqa: BLE001 - only botocore's are read below
        if not type(exc).__module__.startswith("botocore"):
            raise
        # A missing login or permission is no answer of the model: stop the run.
        if is_login_problem(exc) or error_code(exc) in {"AccessDenied", "AccessDeniedException"}:
            raise AwsProblem(explain(f"Asking {model.name}", exc)) from exc
        print(f"{model.name} {label}: error {exc}", file=sys.stderr)
        return {
            "passed": False,
            "why": f"error: {exc}",
            "missed": len(case["findings"]),
            "call_failed": error_code(exc) != REFUSED_AS_SENT,
        }
    kept = {
        "ended": outcome.ended,
        "stop_reason": outcome.stop_reason,
        "calls": [dataclasses.asdict(call) for call in outcome.calls],
        "text": outcome.text,
        "usage": outcome.usage,
    }
    if outcome.failed:
        # A tool of the case file failed: no answer of the model, the run fails as for a timeout.
        call = outcome.failed[0]
        why = f"error: {call.name} failed: {call.failed}"
        print(f"{model.name} {label}: {why}", file=sys.stderr)
        missed = len(case["findings"])
        return {"passed": False, "why": why, "missed": missed, "call_failed": True, **kept}
    marks = judge(case, outcome)
    verdict = "passed" if marks["passed"] else f"FAILED ({marks['why']})"
    print(f"{model.name} {label}: {verdict}", file=sys.stderr)
    return {**marks, **kept}


def label(row: Mapping[str, Any], rounds: int) -> str:
    """A case as named in the output: its id, and its round when there are several."""
    return row["id"] if rounds == 1 else f"{row['id']}/{row['round']}"


def run_model(
    meter: Meter,
    model: AnswerModel,
    companies: Mapping[str, str],
    cases: Sequence[Mapping[str, Any]],
    rounds: int = 1,
    keep_reasoning: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """The rows of one model, every case once a round, and why the run stopped, if it did: at
    the cost limit, or for a missing login or permission. The rows before the stop are kept; they
    are paid for, and the rounds before it are whole."""
    meter.model = model
    rows: list[dict[str, Any]] = []
    for round_ in range(1, rounds + 1):
        for case in cases:
            name = label({"id": case["id"], "round": round_}, rounds)
            started, spent, calls = time.perf_counter(), meter.spent, meter.calls
            stopped = None
            try:
                marks = run_case(meter, model, companies, case, name, keep_reasoning)
            except (CostLimitReached, AwsProblem) as stop:
                where = " at the cost limit" if isinstance(stop, CostLimitReached) else ""
                stopped = f"stopped{where}: {stop}"
                marks = {"passed": False, "why": stopped, "missed": len(case["findings"])}
                print(f"{model.name} {name}: {stopped}", file=sys.stderr)
            rows.append(
                {
                    "id": case["id"],
                    "round": round_,
                    **marks,
                    "steps": meter.calls - calls,
                    "seconds": time.perf_counter() - started,
                    "cost_usd": float(meter.spent - spent),
                }
            )
            if stopped:
                return rows, stopped
    return rows, None


def summarise(
    model: AnswerModel, rows: Sequence[Mapping[str, Any]], stopped: bool
) -> dict[str, Any]:
    def count(key: str) -> str:
        return f"{sum(bool(r.get(key)) for r in rows)}/{len(rows)}"

    seconds = [r["seconds"] for r in rows]
    rounds = max((r.get("round", 1) for r in rows), default=1)
    return {
        "model": model.name,
        "passed": count("passed"),
        "passed_cases": sum(bool(r.get("passed")) for r in rows),
        "found_all": count("found_all"),
        "missed": sum(r.get("missed", 0) for r in rows),
        "none_made_up": count("none_made_up"),
        "closed_right": count("closed_right"),
        "invalid_calls": sum(r.get("invalid_calls", 0) for r in rows),
        "mean_steps": statistics.mean(r["steps"] for r in rows) if rows else None,
        "cost_usd": sum(r["cost_usd"] for r in rows),
        "seconds": sum(seconds),
        "median_seconds": statistics.median(seconds) if seconds else None,
        "failed": {label(r, rounds): r["why"] for r in rows if not r["passed"]},
        "failed_calls": [label(r, rounds) for r in rows if r.get("call_failed")],
        "stopped": stopped,
    }


def ranked(summaries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Most cases passed first; at a tie, fewest discrepancies missed, since a missed shortfall
    costs money and nobody notices it; then the cheaper (the operator, 21.10.2026)."""
    return sorted(summaries, key=lambda s: (-s["passed_cases"], s["missed"], s["cost_usd"]))


def rounds_count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is no whole number of 1 or more")
    return value


def usd(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = Decimal(0)
    if not (value.is_finite() and value > 0):
        raise argparse.ArgumentTypeError(f"{text!r} is no amount above 0")
    return value


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    parser = argparse.ArgumentParser(
        prog="python -m scripts.receiving_probe",
        description="Measure how cleanly each Frankfurt model checks delivery notes with tools.",
    )
    parser.add_argument("--model", action="append", help="a model name of the region's table")
    parser.add_argument("--cases", type=Path, default=CASES_FILE)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--max-usd", type=usd, default=DEFAULT_MAX_USD)
    parser.add_argument("--repeat", type=rounds_count, default=1, help="rounds over all cases")
    parser.add_argument(
        "--keep-reasoning",
        action="store_true",
        help="give a model's reasoning back in the conversation (off on 20. and 21.10.2026)",
    )
    args = parser.parse_args(argv)
    if not args.region.startswith("eu-"):
        print(f"error: {args.region} is not an EU region", file=sys.stderr)
        return 2
    try:
        # The region's own table, so that the limit and the costs count with its prices.
        table = region_models(args.region).models
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    models = [m for m in table if not args.model or m.name in args.model]
    unknown = set(args.model or []) - {m.name for m in table}
    if unknown or not models:
        known = [m.name for m in table]
        print(f"error: unknown model {sorted(unknown)}; known: {known}", file=sys.stderr)
        return 2
    outside = [m.model_id for m in models if not stays_in_the_eu(m.model_id)]
    if outside:
        print(f"error: these would leave the EU: {outside}", file=sys.stderr)
        return 2
    companies, cases = load_cases(args.cases)
    if args.out:
        # A bad path fails now, not after paying; an earlier result there stays as it is.
        with args.out.open("a", encoding="utf-8"):
            pass
    runs: dict[str, list[dict[str, Any]]] = {}
    summaries: list[dict[str, Any]] = []
    meter = Meter(None, args.max_usd)
    try:
        meter.runtime = model_client(args.region)
        for model in models:
            rows, stopped = run_model(
                meter, model, companies, cases, args.repeat, args.keep_reasoning
            )
            runs[model.name] = rows
            summaries.append(summarise(model, rows, stopped is not None))
            if args.out:
                result = {
                    "max_usd": str(args.max_usd),
                    "repeat": args.repeat,
                    "keep_reasoning": args.keep_reasoning,
                    "summaries": ranked(summaries),
                    "runs": runs,
                }
                args.out.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            if stopped:
                print_tables(summaries, runs)
                print(f"error: {stopped}", file=sys.stderr)
                return 2
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        print_tables(summaries, runs)
        return 2
    print_tables(summaries, runs)
    print(f"spent {meter.spent:.5f} USD of at most {args.max_usd} USD", file=sys.stderr)
    failed_calls = [f"{s['model']} {name}" for s in summaries for name in s["failed_calls"]]
    if failed_calls:
        # The report stands; a failed call still fails the run.
        print(f"error: failed calls, counted as failed: {', '.join(failed_calls)}", file=sys.stderr)
        return 2
    return 0


def seconds_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def print_tables(
    summaries: Sequence[Mapping[str, Any]], runs: Mapping[str, Sequence[Mapping[str, Any]]]
) -> None:
    """The models measured so far, ranked, then each model's cases, as Markdown tables."""
    order = ranked(summaries)
    print(
        "| Model | Passed | All found | Missed | None made up | Right close | Invalid calls "
        "| Mean steps | USD | Seconds | Median seconds per case |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for s in order:
        steps = "-" if s["mean_steps"] is None else f"{s['mean_steps']:.1f}"
        passed = f"{s['passed']} (stopped)" if s["stopped"] else s["passed"]
        print(
            f"| {s['model']} | {passed} | {s['found_all']} | {s['missed']} | {s['none_made_up']} "
            f"| {s['closed_right']} | {s['invalid_calls']} | {steps} | {s['cost_usd']:.5f} "
            f"| {seconds_text(s['seconds'])} | {seconds_text(s['median_seconds'])} |"
        )
    for model in [s["model"] for s in order]:
        print(f"\n### {model}\n")
        print("| Case | Round | Passed | Steps | Invalid calls | USD | Seconds | Why |")
        print("|---|---|---|---|---|---|---|---|")
        for r in runs[model]:
            print(
                f"| {r['id']} | {r['round']} | {'yes' if r['passed'] else 'no'} | {r['steps']} "
                f"| {r.get('invalid_calls', '-')} | {r['cost_usd']:.5f} "
                f"| {seconds_text(r['seconds'])} | {r['why'] or '-'} |"
            )


if __name__ == "__main__":
    sys.exit(main())

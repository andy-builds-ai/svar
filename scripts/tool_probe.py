"""Whether an answer model fills in a tool cleanly, and asks instead of guessing.

Svar is to take orders, absences and invoice drafts later; a model that answers
well but cannot call a tool would have to be replaced then. Each case in
`tool-cases.json` sends one conversation through Bedrock `converse` with one
tool, `record_order` (material, quantity, unit, site, delivery date, all
required). A case that gives everything expects exactly one call with the
expected values; a case that leaves something out or vague expects no call and
a question or a request back for what is missing. No guardrail, no knowledge
base: only the model, in ``--region``. A run of the six cases costs fractions of a cent.
"""

import argparse
import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from infra.answer_models import FRANKFURT_MODELS, AnswerModel
from scripts import utf8_output
from scripts.aws import AwsProblem, error_code, explain, explained, is_login_problem
from scripts.evaluate import stays_in_the_eu

CASES_FILE = Path(__file__).with_name("tool-cases.json")

DEFAULT_REGION = "eu-central-1"

MODELS = FRANKFURT_MODELS

#: The day the cases are set on: "morgen" and "Freitag" mean dates relative to it.
TODAY = "Tuesday, 6 October 2026"

SYSTEM_PROMPT = f"""You are the office assistant of a small German building firm. Today is {TODAY}.
Record material orders with the tool record_order.
Call the tool only when the user has given the material, an exact quantity with its unit, the
building site and the delivery date. If anything is missing or vague, do not call the tool and
do not guess: ask the user for exactly what is missing. Answer in the user's language."""

TOOL = {
    "toolSpec": {
        "name": "record_order",
        "description": "Record one material order for a building site.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "material": {"type": "string", "description": "What to deliver."},
                    "quantity": {"type": "number", "description": "How many units."},
                    "unit": {"type": "string", "description": "The unit, e.g. pallets, tonnes."},
                    "site": {"type": "string", "description": "The building site's address."},
                    "delivery_date": {
                        "type": "string",
                        "description": "The delivery date as YYYY-MM-DD.",
                    },
                },
                "required": ["material", "quantity", "unit", "site", "delivery_date"],
            }
        },
    }
}

MAX_TOKENS = 1024

#: The answer function's limits for a model call: a model that does not answer in time fails
#: the case instead of holding the run.
CONNECT_SECONDS, READ_SECONDS = 3, 40


def model_client(region: str) -> Any:
    """A bedrock-runtime client with the function's timeouts and no retry."""
    with explained("Connecting to Bedrock"):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=CONNECT_SECONDS,
                read_timeout=READ_SECONDS,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )


def load_cases(path: Path) -> list[dict[str, Any]]:
    """The cases, checked: a call case names the values, an ask case the words of a question."""
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        name, messages = case["id"], case["messages"]
        if not (isinstance(messages, list) and len(messages) % 2 == 1 and all(messages)):
            raise ValueError(f"{name}: messages alternate user, assistant, ..., user")
        if case["expect"] == "call":
            if set(case["values"]) != set(TOOL["toolSpec"]["inputSchema"]["json"]["required"]):
                raise ValueError(f"{name}: values must name every field of the tool")
        elif case["expect"] == "ask":
            groups = case.get("asks_for")
            if not (
                isinstance(groups, list)
                and groups
                and all(
                    isinstance(g, list) and g and all(isinstance(w, str) and w for w in g)
                    for g in groups
                )
            ):
                raise ValueError(
                    f"{name}: an ask case lists the words of the question, one group per "
                    "missing detail"
                )
        else:
            raise ValueError(f"{name}: expect is call or ask")
    return cases


def conversation(messages: Sequence[str]) -> list[dict[str, Any]]:
    """The texts as alternating user and assistant messages, the user's first."""
    roles = ("user", "assistant")
    return [
        {"role": roles[number % 2], "content": [{"text": text}]}
        for number, text in enumerate(messages)
    ]


def tool_calls(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The inputs of every tool call in the reply."""
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    # An input that is not an object (a model may send a string) counts as a call with no
    # values: it fails the case instead of ending the run.
    return [
        block["toolUse"].get("input") if isinstance(block["toolUse"].get("input"), dict) else {}
        for block in blocks
        if "toolUse" in block
    ]


def reply_text(response: Mapping[str, Any]) -> str:
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    return "\n".join(block["text"] for block in blocks if "text" in block)


def value_matches(expected: Any, given: Any) -> bool:
    """A number equal to the expected one; a text that holds one of the expected spellings,
    case and spaces aside; a date exactly."""
    if isinstance(expected, (int, float)):
        try:
            return float(given) == float(expected)
        except (TypeError, ValueError):
            return False
    if isinstance(expected, list):
        text = " ".join(str(given).lower().split())
        # A spelling of one or two letters ("t") must be the whole value, or it would match
        # nearly anything.
        return any(
            spelling.lower() in text if len(spelling) > 2 else text == spelling.lower()
            for spelling in expected
        )
    return str(given).strip() == expected


def judge(case: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the reply does what the case expects, and why not."""
    calls = tool_calls(response)
    text = reply_text(response)
    if case["expect"] == "call":
        if len(calls) != 1:
            return {"passed": False, "why": f"{len(calls)} tool calls", "calls": calls}
        wrong = [
            field
            for field, expected in case["values"].items()
            if not value_matches(expected, calls[0].get(field))
        ]
        return {
            "passed": not wrong,
            "why": f"wrong {', '.join(wrong)}" if wrong else "",
            "calls": calls,
        }
    if calls:
        return {"passed": False, "why": "called the tool instead of asking", "calls": calls}
    # A question, or a request ("Bitte nennen Sie das Lieferdatum."), for each detail that is
    # missing: one word of every group.
    lowered = text.lower()
    asked = ("?" in text or "bitte" in lowered or "please" in lowered) and all(
        any(word in lowered for word in group) for group in case["asks_for"]
    )
    return {"passed": asked, "why": "" if asked else "no question for what is missing", "calls": []}


def run_model(
    runtime: Any, model: AnswerModel, cases: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        started = time.perf_counter()
        try:
            response = runtime.converse(
                modelId=model.model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=conversation(case["messages"]),
                toolConfig={"tools": [TOOL]},
                inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
            )
        except Exception as exc:  # noqa: BLE001 - a model that cannot take tools is a result
            if not type(exc).__module__.startswith("botocore"):
                raise
            # A missing login or permission is no answer of the model: stop the run.
            if is_login_problem(exc) or error_code(exc) in {
                "AccessDenied",
                "AccessDeniedException",
            }:
                raise AwsProblem(explain(f"Asking {model.name}", exc)) from exc
            rows.append({"id": case["id"], "passed": False, "why": f"error: {exc}", "calls": []})
            print(f"{model.name} {case['id']}: error {exc}", file=sys.stderr)
            continue
        seconds = time.perf_counter() - started
        usage = response.get("usage", {})
        cost = (
            Decimal(usage.get("inputTokens", 0)) * Decimal(model.input_per_1k)
            + Decimal(usage.get("outputTokens", 0)) * Decimal(model.output_per_1k)
        ) / 1000
        marks = judge(case, response)
        rows.append(
            {
                "id": case["id"],
                **marks,
                "text": reply_text(response),
                "stop_reason": response.get("stopReason"),
                "seconds": seconds,
                "cost_usd": float(cost),
            }
        )
        verdict = "passed" if marks["passed"] else f"FAILED ({marks['why']})"
        print(f"{model.name} {case['id']}: {verdict}", file=sys.stderr)
    return rows


def summarise(model: AnswerModel, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    timed = [r["seconds"] for r in rows if "seconds" in r]
    return {
        "model": model.name,
        "passed": f"{sum(r['passed'] for r in rows)}/{len(rows)}",
        "failed": {r["id"]: r["why"] for r in rows if not r["passed"]},
        "cost_usd": sum(r.get("cost_usd", 0.0) for r in rows),
        "median_seconds": statistics.median(timed) if timed else None,
    }


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    parser = argparse.ArgumentParser(
        prog="python -m scripts.tool_probe",
        description="Check that answer models call a tool cleanly and ask instead of guessing.",
    )
    parser.add_argument("--model", action="append", help="a model name from MODELS")
    parser.add_argument("--cases", type=Path, default=CASES_FILE)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args(argv)
    models = [m for m in MODELS if not args.model or m.name in args.model]
    unknown = set(args.model or []) - {m.name for m in MODELS}
    if unknown or not models:
        print(f"error: unknown model {sorted(unknown)}; known: {[m.name for m in MODELS]}")
        return 2
    outside = [m.model_id for m in models if not stays_in_the_eu(m.model_id)]
    if outside:
        print(f"error: these would leave the EU: {outside}", file=sys.stderr)
        return 2
    if not args.region.startswith("eu-"):
        print(f"error: {args.region} is not an EU region", file=sys.stderr)
        return 2
    cases = load_cases(args.cases)
    if args.out:
        # A bad path fails now, not after paying; an earlier result there stays as it is.
        with args.out.open("a", encoding="utf-8"):
            pass
    runs, summaries = {}, []
    try:
        runtime = model_client(args.region)
        for model in models:
            runs[model.name] = run_model(runtime, model, cases)
            summaries.append(summarise(model, runs[model.name]))
            if args.out:
                args.out.write_text(
                    json.dumps(
                        {"summaries": summaries, "runs": runs}, ensure_ascii=False, indent=2
                    ),
                    encoding="utf-8",
                )
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        print_table(summaries)
        return 2
    print_table(summaries)
    return 0


def print_table(summaries: Sequence[Mapping[str, Any]]) -> None:
    """The models measured so far, as a Markdown table."""
    print("| Model | Passed | Failed | USD for all cases | Median seconds |")
    print("|---|---|---|---|---|")
    for s in summaries:
        failed = "; ".join(f"{k}: {v}" for k, v in s["failed"].items()) or "-"
        seconds = "-" if s["median_seconds"] is None else f"{s['median_seconds']:.1f}"
        print(f"| {s['model']} | {s['passed']} | {failed} | {s['cost_usd']:.5f} | {seconds} |")


if __name__ == "__main__":
    sys.exit(main())

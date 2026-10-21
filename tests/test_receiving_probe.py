"""The receiving probe: how a check is judged, the rounds and the ranking, the cost limit,
without AWS. The case file has its own tests in test_receiving_cases.py."""

import json
import re
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts import receiving_probe as probe
from vard import engine

NOTE = re.compile(r"LS-[A-Z]+-\d+")


def use(name: str, given: Any, number: int = 1) -> dict[str, Any]:
    return {"toolUse": {"toolUseId": f"u{number}", "name": name, "input": given}}


def reply(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": list(blocks)}},
        "usage": {"inputTokens": 1000, "outputTokens": 100},
    }


def last_result(messages: Sequence[dict[str, Any]]) -> Any:
    return json.loads(messages[-1]["content"][0]["toolResult"]["content"][0]["text"])


class Clerk:
    """A model that does each check the way its case says it should be done: it fetches the
    note, reads the order number off the result, fetches the order, for a rest delivery the
    earlier deliveries too, and closes; a note to hand off it hands off once it has read it."""

    def __init__(self, cases: Sequence[dict[str, Any]], booking: bool = False) -> None:
        self.cases = {case["delivery_note"]["note_number"]: case for case in cases}
        self.booking = booking
        self.request_bytes: list[int] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.request_bytes.append(len(json.dumps(request, ensure_ascii=False).encode("utf-8")))
        messages = request["messages"]
        note = NOTE.search(messages[0]["content"][0]["text"]).group()  # type: ignore[union-attr]
        case = self.cases[note]
        if len(messages) == 1:
            if self.booking:
                return reply(use("book_goods_receipt", {"note_number": note}))
            return reply(use("get_delivery_note", {"note_number": note}))
        result = last_result(messages)
        if result.get("error"):
            return reply(use("get_delivery_note", {"note_number": note}))
        if "note_number" in result:
            if case["expect"] == "hand_off":
                return reply(use("hand_off", {"note_number": note, "reason": "Nicht prüfbar."}))
            return reply(use("get_purchase_order", {"order_number": result["order_number"]}))
        if "deliveries" not in result and case.get("earlier_deliveries"):
            return reply(use("get_earlier_deliveries", {"order_number": result["order_number"]}))
        findings = [
            {"item": f["items"][-1], "kind": f["kinds"][0], "ordered": "", "delivered": ""}
            for f in case["findings"]
        ]
        given = {"note_number": note, "findings": findings} if findings else {"note_number": note}
        return reply({"text": "Geprüft."}, use(case["expect"], given))


@pytest.fixture(scope="module")
def loaded() -> tuple[dict[str, str], list[dict[str, Any]]]:
    return probe.load_cases(probe.CASES_FILE)


@pytest.fixture(scope="module")
def cases(loaded: tuple[dict[str, str], list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in loaded[1]}


def outcome(*calls: engine.Call, ended: str = engine.CLOSED) -> engine.Outcome:
    return engine.Outcome(ended=ended, steps=3, calls=list(calls))


def closing(name: str, given: Any) -> engine.Call:
    return engine.Call(name, given, result={"recorded": True}, closes=True)


def report(*findings: Any) -> engine.Call:
    return closing("report_discrepancies", {"note_number": "LS", "findings": list(findings)})


K5 = [
    {"item": "OS-WT-55", "kind": "wrong_item"},
    {"item": "os-ws-sc", "kind": "missing_item"},
    {"item": "OS-EV-12", "kind": "wrong_quantity"},
]


def test_every_finding_found_and_none_made_up_passes(cases: dict[str, dict[str, Any]]) -> None:
    marks = probe.judge(cases["k5"], outcome(report(*K5)))
    assert marks["passed"] and marks["why"] == ""


@pytest.mark.parametrize(
    "findings, why",
    [
        (K5[:2], "missed OS-EV-12 wrong_quantity"),
        ([*K5, {"item": "OS-WC-WH", "kind": "wrong_quantity"}], "made up OS-WC-WH wrong_quantity"),
        ([K5[0], K5[1], {"item": "OS-EV-12", "kind": "missing_item"}], "missed OS-EV-12"),
        ([*K5, {"kind": "wrong_unit"}], "made up ? wrong_unit"),
        ([*K5, "Eckventile fehlen"], "made up ECKVENTILEFEHLEN ?"),
    ],
)
def test_a_missed_or_made_up_finding_fails(
    cases: dict[str, dict[str, Any]], findings: list[Any], why: str
) -> None:
    marks = probe.judge(cases["k5"], outcome(report(*findings)))
    assert not marks["passed"] and why in marks["why"]


MATCH = closing("report_match", {"note_number": "LS"})
HAND_OFF = closing("hand_off", {"note_number": "LS", "reason": "?"})


def test_findings_sent_as_a_json_text_count(cases: dict[str, dict[str, Any]]) -> None:
    call = closing("report_discrepancies", {"note_number": "LS", "findings": json.dumps(K5)})
    assert probe.judge(cases["k5"], outcome(call))["passed"]


@pytest.mark.parametrize(
    "calls, ended, why",
    [
        ((MATCH,), engine.CLOSED, "closed with report_match"),
        ((report(*K5), HAND_OFF), engine.CLOSED, "closed with report_discrepancies and hand_off"),
        ((), engine.ANSWERED, "answered without a closing tool"),
        ((), engine.STEP_LIMIT, "reached the step limit of 8"),
    ],
)
def test_a_wrong_or_missing_close_fails(
    cases: dict[str, dict[str, Any]], calls: tuple[engine.Call, ...], ended: str, why: str
) -> None:
    marks = probe.judge(cases["k5"], outcome(*calls, ended=ended))
    assert not marks["passed"] and not marks["closed_right"] and why in marks["why"]


def test_a_report_on_a_matching_note_makes_a_finding_up(cases: dict[str, dict[str, Any]]) -> None:
    marks = probe.judge(
        cases["k2"], outcome(report({"item": "VW-NP-30", "kind": "wrong_quantity"}))
    )
    assert (marks["closed_right"], marks["none_made_up"]) == (False, False)


def test_an_invalid_call_fails_a_check_that_is_otherwise_right(
    cases: dict[str, dict[str, Any]],
) -> None:
    refused = engine.Call("book_goods_receipt", {}, refused="There is no tool")
    marks = probe.judge(
        cases["k6"], outcome(refused, report({"item": "OS-TV-12", "kind": "wrong_quantity"}))
    )
    assert not marks["passed"] and marks["invalid_calls"] == 1
    assert "1 invalid calls (book_goods_receipt)" in marks["why"]


def test_a_document_is_found_by_its_number_whatever_its_spacing(
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    companies, cases = loaded
    tools = {t.name: t for t in probe.case_responsibility(companies, cases[0]).tools}
    assert tools["get_delivery_note"].run({"note_number": " ls-ht-58201"})["supplier"]
    assert tools["get_purchase_order"].run({"order_number": "PW-2026-9999"}) == {
        "error": "There is no purchase order PW-2026-9999."
    }


def run_main(monkeypatch: pytest.MonkeyPatch, model: Any, *argv: str) -> int:
    monkeypatch.setattr(probe, "model_client", lambda region: model)
    return probe.main(["--model", probe.MODELS[0].name, *argv])


def test_a_clerk_that_does_it_right_passes_every_case(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    out = tmp_path / "receiving.json"
    assert run_main(monkeypatch, Clerk(loaded[1]), "--out", str(out)) == 0
    printed = capsys.readouterr().out
    name = probe.MODELS[0].name
    assert f"| {name} | 24/24 | 24/24 | 0 | 24/24 | 24/24 | 0 |" in printed
    assert f"### {name}" in printed and "| p1 | 1 | yes | 3 | 0 |" in printed
    assert "| p8 | 1 | yes | 4 | 0 |" in printed and "| k12 | 1 | yes | 2 | 0 |" in printed
    result = json.loads(out.read_text(encoding="utf-8"))
    (summary,) = result["summaries"]
    assert summary["seconds"] >= 0 and summary["median_seconds"] >= 0
    rows = result["runs"][name]
    assert all(row["seconds"] >= 0 and row["steps"] in (2, 3, 4) for row in rows)
    assert all(row["usage"] == [reply()["usage"]] * row["steps"] for row in rows)
    assert (result["max_usd"], result["repeat"]) == (str(probe.DEFAULT_MAX_USD), 1)


def test_a_booking_tool_is_refused_counted_and_told(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    clerk = Clerk(loaded[1], booking=True)
    assert run_main(monkeypatch, clerk) == 0
    printed = capsys.readouterr().out
    assert f"| {probe.MODELS[0].name} | 0/24 | 24/24 | 0 | 24/24 | 24/24 | 24 |" in printed
    assert "1 invalid calls (book_goods_receipt)" in printed


def test_the_cost_limit_stops_before_the_first_call_it_could_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    clerk = Clerk(loaded[1])
    out = tmp_path / "receiving.json"
    assert run_main(monkeypatch, clerk, "--max-usd", "0.0005", "--out", str(out)) == 2
    assert clerk.request_bytes == []
    captured = capsys.readouterr()
    assert "error: stopped at the cost limit" in captured.err
    assert "(stopped)" in captured.out
    (summary,) = json.loads(out.read_text(encoding="utf-8"))["summaries"]
    assert summary["stopped"] and summary["cost_usd"] == 0


def test_the_meter_counts_what_a_call_cost_and_refuses_one_that_could_pass_the_limit() -> None:
    model = probe.MODELS[0]
    request = {"messages": [], "inferenceConfig": {"maxTokens": 2048}}
    ceiling = probe.call_ceiling(model, request)
    size = len(json.dumps(request).encode("utf-8"))
    assert (
        ceiling
        == ((size + 500) * Decimal(model.input_per_1k) + 2048 * Decimal(model.output_per_1k)) / 1000
    )

    class Runtime:
        calls = 0

        def converse(self, **_: Any) -> dict[str, Any]:
            self.calls += 1
            return {"usage": {"inputTokens": 1000, "outputTokens": 100}}

    runtime = Runtime()
    meter = probe.Meter(runtime, ceiling * 2)
    meter.model = model
    meter.converse(**request)
    paid = (1000 * Decimal(model.input_per_1k) + 100 * Decimal(model.output_per_1k)) / 1000
    assert (meter.spent, meter.calls) == (paid, 1)
    meter.spent = ceiling * 2 - ceiling / 2
    with pytest.raises(probe.CostLimitReached):
        meter.converse(**request)
    assert runtime.calls == 1


def test_a_failed_call_keeps_its_ceiling_counted() -> None:
    from botocore.exceptions import ReadTimeoutError

    class Runtime:
        def converse(self, **_: Any) -> None:
            raise ReadTimeoutError(endpoint_url="https://bedrock-runtime")

    request = {"messages": [], "inferenceConfig": {"maxTokens": 2048}}
    meter = probe.Meter(Runtime(), Decimal(1))
    with pytest.raises(ReadTimeoutError):
        meter.converse(**request)
    assert meter.reserved == probe.call_ceiling(meter.model, request) and meter.spent == 0


def runtime_raising(error: Exception) -> Any:
    class Runtime:
        def converse(self, **_: Any) -> None:
            raise error

    return Runtime()


def converse_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "no"}}, "Converse")


def test_a_request_the_model_does_not_accept_is_a_result_that_misses_everything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    assert run_main(monkeypatch, runtime_raising(converse_error("ValidationException"))) == 0
    captured = capsys.readouterr()
    expected = sum(len(case["findings"]) for case in loaded[1])
    assert f"| {probe.MODELS[0].name} | 0/24 | 0/24 | {expected} |" in captured.out
    assert "| p1 | 1 | no | 0 | - |" in captured.out and "failed calls" not in captured.err


def test_a_throttled_call_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(monkeypatch, runtime_raising(converse_error("ThrottlingException"))) == 2
    assert "error: failed calls, counted as failed" in capsys.readouterr().err


def test_a_missing_permission_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(monkeypatch, runtime_raising(converse_error("AccessDeniedException"))) == 2
    assert "access denied" in capsys.readouterr().err


def test_an_earlier_result_stays_when_a_run_fails_before_it_pays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def expired(region: str) -> None:
        raise probe.AwsProblem("Your AWS login has run out.")

    monkeypatch.setattr(probe, "model_client", expired)
    out = tmp_path / "results.json"
    out.write_text('{"summaries": ["paid for"]}', encoding="utf-8")
    assert probe.main(["--out", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == '{"summaries": ["paid for"]}'


def test_a_region_outside_the_eu_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(["--region", "us-east-1"]) == 2
    assert "not an EU region" in capsys.readouterr().err


@pytest.mark.parametrize("amount", ["0", "-1", "abc", "nan", "inf"])
def test_a_cost_limit_that_is_no_amount_above_zero_is_refused(
    amount: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stop:
        probe.main(["--max-usd", amount])
    assert stop.value.code == 2 and "no amount above 0" in capsys.readouterr().err


def test_a_reply_cut_off_at_max_tokens_says_so(cases: dict[str, dict[str, Any]]) -> None:
    cut = engine.Outcome(ended=engine.ANSWERED, steps=1, stop_reason="max_tokens")
    assert f"cut off at {probe.MAX_TOKENS} tokens" in probe.judge(cases["p1"], cut)["why"]


def test_another_region_measures_its_own_models_at_its_own_prices(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    from infra.answer_models import ANSWER_MODELS

    monkeypatch.setattr(probe, "model_client", lambda region: Clerk(loaded[1]))
    nova = next(m for m in ANSWER_MODELS if m.name == "Nova Pro")
    assert probe.main(["--region", "eu-west-1", "--model", "Nova Pro"]) == 0
    case_cost = 3 * (1000 * Decimal(nova.input_per_1k) + 100 * Decimal(nova.output_per_1k)) / 1000
    assert f"| p1 | 1 | yes | 3 | 0 | {case_cost:.5f} |" in capsys.readouterr().out
    assert probe.main(["--region", "eu-west-1", "--model", "gpt-oss-120b"]) == 2
    assert probe.main(["--region", "eu-north-1"]) == 2
    assert "has no models" in capsys.readouterr().err


def test_a_login_that_runs_out_mid_run_keeps_the_paid_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    from botocore.exceptions import ClientError

    class RunsOut(Clerk):
        def converse(self, **request: Any) -> dict[str, Any]:
            if len(self.request_bytes) == 6:
                raise ClientError({"Error": {"Code": "ExpiredToken", "Message": "x"}}, "Converse")
            return super().converse(**request)

    out = tmp_path / "receiving.json"
    assert run_main(monkeypatch, RunsOut(loaded[1]), "--out", str(out)) == 2
    assert "AWS login is missing or expired" in capsys.readouterr().err
    rows = json.loads(out.read_text(encoding="utf-8"))["runs"][probe.MODELS[0].name]
    assert [(r["id"], r["passed"]) for r in rows] == [("p1", True), ("p2", True), ("p3", False)]


#: k5's wrong washbasin as two halves: the ordered one missing, the delivered one extra.
K5_HALVES = [
    {"item": "OS-WT-60", "kind": "missing_item"},
    {"item": "OS-WT-55", "kind": "extra_item"},
]


@pytest.mark.parametrize("case", ["k5", "p5"])
def test_a_wrong_article_reported_as_its_two_halves_is_met(
    cases: dict[str, dict[str, Any]], case: str
) -> None:
    halves = {
        "k5": [*K5_HALVES, *K5[1:]],
        "p5": [
            {"item": "QB-MV-3592", "kind": "missing_item"},
            {"item": "QB-MV-4592", "kind": "extra_item"},
        ],
    }[case]
    marks = probe.judge(cases[case], outcome(report(*halves)))
    assert marks["passed"] and marks["why"] == ""


@pytest.mark.parametrize("half", K5_HALVES)
def test_one_half_of_a_wrong_article_is_missed_and_made_up(
    cases: dict[str, dict[str, Any]], half: dict[str, str]
) -> None:
    marks = probe.judge(cases["k5"], outcome(report(half, *K5[1:])))
    assert not marks["found_all"] and not marks["none_made_up"]
    assert "missed OS-WT-60 wrong_item" in marks["why"]
    assert f"made up {half['item']} {half['kind']}" in marks["why"]


def test_the_halves_with_their_kinds_swapped_fail(cases: dict[str, dict[str, Any]]) -> None:
    swapped = [
        {"item": "OS-WT-60", "kind": "extra_item"},
        {"item": "OS-WT-55", "kind": "missing_item"},
    ]
    marks = probe.judge(cases["k5"], outcome(report(*swapped, *K5[1:])))
    assert not marks["passed"] and "missed OS-WT-60 wrong_item" in marks["why"]
    assert "made up OS-WT-55 missing_item, OS-WT-60 extra_item" in marks["why"]


def test_the_halves_beside_a_wrong_item_make_nothing_up(cases: dict[str, dict[str, Any]]) -> None:
    marks = probe.judge(cases["k5"], outcome(report(*K5, *K5_HALVES)))
    assert marks["passed"]


def test_every_round_runs_every_case_and_the_marks_count_over_all(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    out = tmp_path / "receiving.json"
    assert run_main(monkeypatch, Clerk(loaded[1]), "--repeat", "2", "--out", str(out)) == 0
    name = probe.MODELS[0].name
    assert f"| {name} | 48/48 | 48/48 | 0 | 48/48 | 48/48 | 0 |" in capsys.readouterr().out
    result = json.loads(out.read_text(encoding="utf-8"))
    ids = [case["id"] for case in loaded[1]]
    rounds = [(row["id"], row["round"]) for row in result["runs"][name]]
    assert rounds == [(i, 1) for i in ids] + [(i, 2) for i in ids]
    assert result["repeat"] == 2


def test_a_failed_case_is_named_with_its_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    out = tmp_path / "receiving.json"
    clerk = Clerk(loaded[1], booking=True)
    assert run_main(monkeypatch, clerk, "--repeat", "2", "--out", str(out)) == 0
    (summary,) = json.loads(out.read_text(encoding="utf-8"))["summaries"]
    assert len(summary["failed"]) == 48 and {"p1/1", "p1/2", "k12/2"} <= set(summary["failed"])


@pytest.mark.parametrize("rounds", ["0", "-1", "1.5", "x"])
def test_rounds_that_are_no_whole_number_of_one_or_more_are_refused(
    rounds: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stop:
        probe.main(["--repeat", rounds])
    assert stop.value.code == 2 and "no whole number of 1 or more" in capsys.readouterr().err


def test_missed_counts_each_expected_finding_not_reported(
    cases: dict[str, dict[str, Any]],
) -> None:
    one = report({"item": "OS-SI-WS", "kind": "extra_item"})
    assert probe.judge(cases["k7"], outcome(one))["missed"] == 2
    assert probe.judge(cases["k7"], outcome(ended=engine.ANSWERED))["missed"] == 3
    row = {"id": "k7", "round": 1, "passed": False, "why": "missed", "steps": 3, "seconds": 0.0}
    row["cost_usd"] = 0.0
    rows = [{**row, "missed": 2}, {**row, "round": 2, "missed": 3}]
    assert probe.summarise(probe.MODELS[0], rows, False)["missed"] == 5


def summary(model: str, passed: int, missed: int, cost: float) -> dict[str, Any]:
    return {"model": model, "passed_cases": passed, "missed": missed, "cost_usd": cost}


def test_the_models_rank_by_cases_passed_then_by_discrepancies_missed_then_by_cost() -> None:
    given = [
        summary("cheap, misses three", 20, 3, 0.01),
        summary("dear, misses one", 20, 1, 0.05),
        summary("passes most", 21, 9, 0.90),
        summary("cheaper, misses one", 20, 1, 0.02),
    ]
    assert [s["model"] for s in probe.ranked(given)] == [
        "passes most",
        "cheaper, misses one",
        "dear, misses one",
        "cheap, misses three",
    ]


def test_the_table_and_out_list_the_models_in_their_rank(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    first, second = probe.MODELS[0], probe.MODELS[1]

    class FirstBooks(Clerk):
        def converse(self, **request: Any) -> dict[str, Any]:
            self.booking = request["modelId"] == first.model_id
            return super().converse(**request)

    monkeypatch.setattr(probe, "model_client", lambda region: FirstBooks(loaded[1]))
    out = tmp_path / "receiving.json"
    argv = ["--model", first.name, "--model", second.name, "--out", str(out)]
    assert probe.main(argv) == 0
    printed = capsys.readouterr().out
    assert printed.index(f"| {second.name} | 24/24") < printed.index(f"| {first.name} | 0/24")
    assert printed.index(f"### {second.name}") < printed.index(f"### {first.name}")
    summaries = json.loads(out.read_text(encoding="utf-8"))["summaries"]
    assert [s["model"] for s in summaries] == [second.name, first.name]


def test_a_tool_of_the_case_file_that_fails_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    loaded: tuple[dict[str, str], list[dict[str, Any]]],
) -> None:
    def broken(wanted: str) -> None:
        raise KeyError("index")

    finder = probe.finder
    monkeypatch.setattr(
        probe,
        "finder",
        lambda documents, key: broken if key == "order_number" else finder(documents, key),
    )
    out = tmp_path / "receiving.json"
    assert run_main(monkeypatch, Clerk(loaded[1]), "--out", str(out)) == 2
    assert "error: failed calls, counted as failed" in capsys.readouterr().err
    rows = {
        r["id"]: r
        for r in json.loads(out.read_text(encoding="utf-8"))["runs"][probe.MODELS[0].name]
    }
    assert rows["p1"]["why"] == "error: get_purchase_order failed: KeyError: 'index'"
    assert rows["p1"]["call_failed"] and rows["p1"]["calls"][1]["failed"] == "KeyError: 'index'"
    assert rows["k4"]["passed"] and not rows["k4"].get("call_failed")

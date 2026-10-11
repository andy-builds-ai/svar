"""The tool test: its cases and how a reply is judged, without AWS."""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import tool_probe


def reply(text: str = "", *calls: dict[str, Any]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"text": text}] if text else []
    content += [{"toolUse": {"toolUseId": "1", "name": "record_order", "input": c}} for c in calls]
    return {"output": {"message": {"content": content}}}


@pytest.fixture(scope="module")
def cases() -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in tool_probe.load_cases(tool_probe.CASES_FILE)}


GOOD_T1 = {
    "material": "Pflastersteine",
    "quantity": 12,
    "unit": "Paletten",
    "site": "Hauptstraße 5",
    "delivery_date": "2026-10-14",
}


def test_the_cases_ask_and_call(cases: dict[str, dict[str, Any]]) -> None:
    expects = [case["expect"] for case in cases.values()]
    assert (expects.count("call"), expects.count("ask")) == (3, 3)


def test_one_call_with_the_expected_values_passes(cases: dict[str, dict[str, Any]]) -> None:
    assert tool_probe.judge(cases["t1"], reply("", GOOD_T1))["passed"]
    assert tool_probe.judge(cases["t1"], reply("", {**GOOD_T1, "quantity": "12"}))["passed"]


@pytest.mark.parametrize(
    "calls, why",
    [
        ((), "0 tool calls"),
        ((GOOD_T1, GOOD_T1), "2 tool calls"),
        (({**GOOD_T1, "delivery_date": "14.10.2026"},), "wrong delivery_date"),
        (({**GOOD_T1, "quantity": 10},), "wrong quantity"),
        (({**GOOD_T1, "site": "Baustelle"},), "wrong site"),
    ],
)
def test_a_wrong_call_fails(
    cases: dict[str, dict[str, Any]], calls: tuple[dict[str, Any], ...], why: str
) -> None:
    marks = tool_probe.judge(cases["t1"], reply("Erledigt.", *calls))
    assert not marks["passed"] and marks["why"] == why


def test_asking_for_what_is_missing_passes(cases: dict[str, dict[str, Any]]) -> None:
    assert tool_probe.judge(cases["t2"], reply("Für wann soll ich den Splitt bestellen?"))["passed"]


@pytest.mark.parametrize(
    "answer, calls",
    [
        ("Ich habe bestellt.", ()),
        ("Soll ich das wirklich bestellen?", ()),
        ("Wann?", ({**GOOD_T1, "material": "Splitt"},)),
    ],
)
def test_guessing_or_not_asking_fails(
    cases: dict[str, dict[str, Any]], answer: str, calls: tuple[dict[str, Any], ...]
) -> None:
    assert not tool_probe.judge(cases["t2"], reply(answer, *calls))["passed"]


def test_a_request_without_a_question_mark_asks_too(cases: dict[str, dict[str, Any]]) -> None:
    """Qwen3 235B and Nova Pro ask so (06.10.2026); it is a question back all the same."""
    assert tool_probe.judge(cases["t2"], reply("Bitte nennen Sie noch das Lieferdatum."))["passed"]


@pytest.mark.parametrize("given, matches", [("Tonnen", True), ("t", True), ("Stück", False)])
def test_a_short_spelling_must_be_the_whole_value(given: str, matches: bool) -> None:
    assert tool_probe.value_matches(["tonne", "ton", "t"], given) is matches


@pytest.mark.parametrize("given", ["Paletten", "pallets", "Palette"])
def test_a_unit_counts_in_german_and_english(given: str) -> None:
    assert tool_probe.value_matches(["palette", "pallet"], given)


def test_a_conversation_alternates_and_starts_with_the_user() -> None:
    messages = tool_probe.conversation(["a", "b", "c"])
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]


@pytest.mark.parametrize(
    "case, message",
    [
        ({"id": "x", "messages": ["a", "b"], "expect": "ask", "asks_for": ["wann"]}, "alternate"),
        ({"id": "x", "messages": ["a"], "expect": "call", "values": {"material": []}}, "every"),
        ({"id": "x", "messages": ["a"], "expect": "ask"}, "words of the question"),
        ({"id": "x", "messages": ["a"], "expect": "maybe"}, "call or ask"),
    ],
)
def test_a_broken_case_is_refused(tmp_path: Path, case: dict[str, Any], message: str) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([case]), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        tool_probe.load_cases(path)


def test_asking_for_one_of_two_missing_details_fails(cases: dict[str, dict[str, Any]]) -> None:
    """t3 leaves out quantity and site: a question for the site alone is not enough."""
    assert not tool_probe.judge(cases["t3"], reply("Für welche Baustelle?"))["passed"]
    assert tool_probe.judge(cases["t3"], reply("Wie viele Randsteine, und für welche Baustelle?"))[
        "passed"
    ]


def test_a_tool_input_that_is_no_object_fails_the_case(cases: dict[str, dict[str, Any]]) -> None:
    response = {
        "output": {"message": {"content": [{"toolUse": {"toolUseId": "1", "input": "12"}}]}}
    }
    assert tool_probe.judge(cases["t1"], response)["why"] == (
        "wrong material, quantity, unit, site, delivery_date"
    )


def test_a_missing_permission_stops_the_run() -> None:
    from botocore.exceptions import ClientError

    class Runtime:
        def converse(self, **_: Any) -> None:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse"
            )

    with pytest.raises(tool_probe.AwsProblem, match="access denied"):
        tool_probe.run_model(
            Runtime(), tool_probe.MODELS[0], tool_probe.load_cases(tool_probe.CASES_FILE)
        )


def test_an_earlier_result_stays_when_a_run_fails_before_it_pays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def expired(region: str) -> None:
        raise tool_probe.AwsProblem("Your AWS login has run out.")

    monkeypatch.setattr(tool_probe, "model_client", expired)
    out = tmp_path / "results.json"
    out.write_text('{"summaries": ["paid for"]}', encoding="utf-8")
    assert tool_probe.main(["--out", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == '{"summaries": ["paid for"]}'


def test_a_region_outside_the_eu_is_refused(capsys: Any) -> None:
    assert tool_probe.main(["--region", "us-east-1"]) == 2
    assert "not an EU region" in capsys.readouterr().err


def test_an_expected_spelling_in_capitals_matches_too() -> None:
    """Second review, finding 11."""
    assert tool_probe.value_matches(["Hauptstraße 5"], "hauptstraße 5")


def runtime_raising(error: Exception) -> Any:
    class Runtime:
        def converse(self, **_: Any) -> None:
            raise error

    return Runtime()


def converse_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "no"}}, "Converse")


def read_timeout() -> Exception:
    from botocore.exceptions import ReadTimeoutError

    return ReadTimeoutError(endpoint_url="https://bedrock-runtime")


def test_a_request_the_model_does_not_accept_is_a_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every `ValidationException` fails its case and is a result, whatever Bedrock refused
    the request for; the run itself passes. The table carries the error's text, which is
    where a model that takes no tools is told from a wrong model id."""
    refusing = runtime_raising(converse_error("ValidationException"))
    monkeypatch.setattr(tool_probe, "model_client", lambda region: refusing)
    assert tool_probe.main(["--model", tool_probe.MODELS[0].name]) == 0
    captured = capsys.readouterr()
    assert f"| {tool_probe.MODELS[0].name} | 0/6 |" in captured.out
    assert "t1: error: An error occurred (ValidationException)" in captured.out
    assert "failed calls" not in captured.err


@pytest.mark.parametrize(
    "error",
    [
        converse_error("ThrottlingException"),
        converse_error("ServiceUnavailableException"),
        converse_error("InternalServerException"),
        converse_error("ModelTimeoutException"),
        read_timeout(),
    ],
    ids=["throttled", "unavailable", "internal", "model-timeout", "read-timeout"],
)
def test_a_call_that_fails_for_any_other_reason_fails_the_run(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Throttling, a service error or a timeout says nothing about the model; the table and
    the file are written all the same."""
    monkeypatch.setattr(tool_probe, "model_client", lambda region: runtime_raising(error))
    out = tmp_path / "tools.json"
    assert tool_probe.main(["--model", tool_probe.MODELS[0].name, "--out", str(out)]) == 2
    captured = capsys.readouterr()
    assert f"| {tool_probe.MODELS[0].name} | 0/6 |" in captured.out
    assert "error: failed calls, counted as failed" in captured.err
    (summary,) = json.loads(out.read_text(encoding="utf-8"))["summaries"]
    assert summary["failed_calls"] == ["t1", "t2", "t3", "t4", "t5", "t6"]

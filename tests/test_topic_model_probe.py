"""The topic probe in a region whose stack checks the topics with a model."""

from types import SimpleNamespace
from typing import Any

import clients
import pytest

from scripts import evaluate, topic_model_probe, topic_probe
from tests.test_handler import client_error
from tests.test_topic_model import verdict_response

#: The answer function's modules, as the scripts take them (`evaluate.import_app`).
app = evaluate.import_app()

SETTINGS = SimpleNamespace(topic_model_id="openai.gpt-oss-20b-1:0", region="eu-central-1")


class FakeRuntime:
    """Replies by the text checked: a topic, nothing readable, an error, or none."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        text = kwargs["messages"][0]["content"][0]["text"]
        self.calls.append(text)
        for word, reply in self.replies.items():
            if word in text:
                if isinstance(reply, list):
                    reply = reply.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return verdict_response(reply)
        return verdict_response('{"topic": "none"}')


def question(name: str, text: str, blocked: bool, topic: str = "Tax returns") -> dict[str, Any]:
    return {
        "id": name,
        "language": "de",
        "topic": topic if blocked else "Invoices",
        "question": text,
        "blocked": blocked,
    }


def test_the_evaluation_sets_block_their_advice_questions_only() -> None:
    questions = topic_model_probe.evaluation_questions()
    assert len(questions) == 68
    refused = [q for q in questions if q["blocked"]]
    assert len(refused) == 11
    assert {q["topic"] for q in refused} == {topic_model_probe.ADVICE}
    assert not any(q["topic"] == topic_model_probe.ADVICE for q in questions if not q["blocked"])


def test_a_failed_or_unreadable_check_counts_as_blocked_and_is_listed() -> None:
    runtime = FakeRuntime(
        {
            "Steuer": '{"topic": "Tax returns"}',
            "leer": "",
            "kaputt": client_error("AccessDeniedException"),
        }
    )
    rows = topic_model_probe.measure(
        app,
        runtime,
        SETTINGS,
        [
            question("a", "Steuer sparen?", True),
            question("b", "leer?", False),
            question("c", "kaputt?", False),
            question("d", "Rechnung?", False),
        ],
        lambda line: None,
        repeats=2,
        workers=1,
    )
    by_id = {row["id"]: row for row in rows}
    assert (by_id["a"]["blocked_runs"], by_id["a"]["topics"], by_id["a"]["failed"]) == (
        2,
        ["Tax returns"],
        [],
    )
    assert by_id["b"]["blocked_runs"] == 2 and "no readable verdict" in by_id["b"]["failed"][0]
    assert by_id["c"]["blocked_runs"] == 2 and "access denied" in by_id["c"]["failed"][0]
    assert by_id["d"]["blocked_runs"] == 0
    assert by_id["d"]["tokens"] == {"input_tokens": 800, "output_tokens": 60}
    assert runtime.calls[0] == "Text:\nCustomer to business: Steuer sparen?"
    summary = topic_probe.summarise(rows)
    assert summary["false_blocked"] == 2 and summary["slipped"] == 0


def test_a_throttled_check_is_asked_again_and_not_counted_as_a_verdict() -> None:
    runtime = FakeRuntime(
        {
            "Rechnung": [
                client_error("ThrottlingException"),
                client_error("ThrottlingException"),
                '{"topic": "none"}',
            ]
        }
    )
    waits: list[float] = []
    result = topic_model_probe.check(
        app,
        runtime,
        SETTINGS,
        "INPUT",
        "Customer to business: Rechnung?",
        waits.append,
    )
    assert result["failed"] is None and result["topics"] == []
    assert result["throttled"] == 1 and len(waits) == 1


def test_the_cost_is_the_topic_models_tokens_at_its_frankfurt_price() -> None:
    rows = {"INPUT": [{"id": "a", "tokens": {"input_tokens": 1000, "output_tokens": 100},
                       "seconds": [0.4, 0.6], "failed": []}]}  # fmt: skip
    summary = topic_model_probe.model_summary(rows, "openai.gpt-oss-20b-1:0", "eu-central-1")
    assert summary["cost_usd"] == "0.00013"
    assert summary["checks"] == 2 and summary["max_seconds"] == 0.6


@pytest.mark.usefixtures("frankfurt_topic_model")
def test_the_probe_picks_the_model_where_the_region_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picked: list[str] = []
    monkeypatch.setattr(
        topic_model_probe, "main", lambda args, model_id: picked.append(model_id) or 0
    )
    assert topic_probe.main(["--region", "eu-central-1"]) == 0
    assert picked == ["openai.gpt-oss-20b-1:0"]
    assert topic_probe.main(["--region", "eu-west-1", "--topic-check", "model"]) == 2
    assert topic_probe.main(["--region", "us-east-1"]) == 2


def test_topic_model_picks_another_model_of_the_regions_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frankfurt checks with gpt-oss-120b; --topic-model measures another candidate there."""
    picked: list[str] = []
    monkeypatch.setattr(
        topic_model_probe, "main", lambda args, model_id: picked.append(model_id) or 0
    )
    assert topic_probe.main(["--region", "eu-central-1"]) == 0
    assert (
        topic_probe.main(["--region", "eu-central-1", "--topic-model", "qwen.qwen3-32b-v1:0"]) == 0
    )
    assert picked == ["openai.gpt-oss-120b-1:0", "qwen.qwen3-32b-v1:0"]


def test_topic_model_measures_a_model_where_the_stack_checks_with_a_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picked: list[str] = []
    monkeypatch.setattr(
        topic_model_probe, "main", lambda args, model_id: picked.append(model_id) or 0
    )
    assert topic_probe.main(["--region", "eu-west-1", "--topic-model", "x.model"]) == 0
    assert picked == ["x.model"]


def test_topic_model_with_a_guardrail_check_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        topic_probe.main(["--topic-check", "guardrail", "--topic-model", "openai.gpt-oss-120b-1:0"])
    assert stopped.value.code == 2
    assert "--topic-model measures a topic model" in capsys.readouterr().err


def test_topic_model_with_deployed_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """--deployed measures the deployed function's model; another one named would be ignored."""
    with pytest.raises(SystemExit) as stopped:
        topic_probe.main(["--deployed", "--topic-model", "qwen.qwen3-32b-v1:0"])
    assert stopped.value.code == 2
    assert "--deployed measures the deployed function's topic model" in capsys.readouterr().err


def test_topic_model_without_a_price_in_the_region_stops_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gpt-oss-120b has no price in eu-west-1: the run would pay without a report."""

    def no_call(service: str, region: str) -> None:
        raise AssertionError(f"{service} was called in {region}")

    monkeypatch.setattr(clients, "client", no_call)
    code = topic_probe.main(["--region", "eu-west-1", "--topic-model", "openai.gpt-oss-120b-1:0"])
    assert code == 2
    assert "has no price for eu-west-1" in capsys.readouterr().err


@pytest.mark.parametrize(("failed", "code"), [([], 0), (["no readable verdict"], 2)])
@pytest.mark.usefixtures("frankfurt_topic_model")
def test_a_run_with_a_failed_check_does_not_pass(
    monkeypatch: pytest.MonkeyPatch, failed: list[str], code: int
) -> None:
    """A missing login fails every check; counted as blocked it would look like a perfect run."""

    def measure(*_: Any, **__: Any) -> list[dict[str, Any]]:
        row = {"id": "a", "language": "de", "topic": "Tax returns", "expected": True,
               "runs": 1, "blocked_runs": 1, "topics": [], "usage": {}, "failed": failed,
               "tokens": {"input_tokens": 1, "output_tokens": 1}, "seconds": [0.5],
               "text": "Customer to business: x"}  # fmt: skip
        return [row]

    monkeypatch.setattr(topic_model_probe, "measure", measure)
    monkeypatch.setattr(clients, "client", lambda service, region: None)
    assert topic_probe.main(["--region", "eu-central-1"]) == code

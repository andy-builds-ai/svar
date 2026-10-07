"""The denied topics checked by a model (app/topic_model.py) and the handler's way through it."""

import logging
import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import handler  # app/handler.py, imported by its Lambda name (tests/conftest.py)
import pytest
import topic_model
from denied_topics import DENIED_TOPICS

from tests.test_handler import (  # noqa: F401 (aws and cold_start are fixtures)
    ENVIRONMENT,
    aws,
    call,
    client_error,
    cold_start,
    event,
    log_lines,
)

MODEL_ENVIRONMENT = {
    **{k: v for k, v in ENVIRONMENT.items() if not k.startswith("TOPIC_GUARDRAIL_")},
    "TOPIC_MODEL_ID": "openai.gpt-oss-20b-1:0",
    "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS": "0.00009",
    "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS": "0.0004",
}


def verdict_response(reply: str | None, stop_reason: str = "end_turn") -> dict[str, Any]:
    """What Converse returns for the topic model: its reasoning, then the reply, if any."""
    content: list[dict[str, Any]] = [{"reasoningContent": {"reasoningText": {"text": "..."}}}]
    if reply is not None:
        content.append({"text": reply})
    return {
        "output": {"message": {"content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 400, "outputTokens": 30, "totalTokens": 430},
    }


class FakeTopicModel:
    """The topic model's client: one reply for the question, one for the answer."""

    def __init__(self) -> None:
        self.question_reply: Any = '{"topic": "none"}'
        self.answer_reply: Any = '{"topic": "none"}'
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = kwargs["messages"][0]["content"][0]["text"]
        on_question = text.startswith("Text:\n" + handler.QUESTION_FRAME)
        reply = self.question_reply if on_question else self.answer_reply
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return verdict_response(reply)


@pytest.fixture
def model_aws(aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # noqa: F811
    """The function as the stack in eu-central-1 sets it up: a topic model, no topic guardrail."""
    for name in ("TOPIC_GUARDRAIL_ID", "TOPIC_GUARDRAIL_VERSION"):
        monkeypatch.delenv(name)
    for name, value in MODEL_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    aws["topic-model"] = FakeTopicModel()
    return aws


# --- The verdict ---------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "named"),
    [
        ('{"topic": "none"}', "none"),
        ('{"topic":"None"}', "none"),
        ('{"topic": "Tax returns"}', "Tax returns"),
        ('```json\n{"topic": "private lives"}\n```', "Private lives"),
        ('{"topic": "Legal advice"}', None),
        ('{"topic": "none"}\n{"topic": "Tax returns"}', None),
        ('{"topic": "Tax returns"} {"topic": "tax returns"}', "Tax returns"),
        ('{"topic": ""}', None),
        ("none", None),
        ("", None),
        ("I cannot tell.", None),
    ],
)
def test_the_verdict_is_a_topic_none_or_unreadable(reply: str, named: str | None) -> None:
    assert topic_model.verdict(reply) == named


def test_the_prompt_carries_every_topic_and_the_business_sentence() -> None:
    prompt = topic_model.gate_prompt()
    for name, definition, examples in DENIED_TOPICS:
        assert f"- {name}: {definition} Examples: {'; '.join(examples)}" in prompt
    assert "prices, invoices,\nguarantees, contracts, services" in prompt
    assert "who owns or runs the company or whom to contact there" in prompt
    assert '{"topic": "none"}' in prompt


def test_the_prompt_lets_contracts_and_the_owners_name_pass() -> None:
    """gpt-oss-120b refused he05 as legal action and c16 as a private life without these
    sentences (06. and 07.10.2026, docs/decisions.md)."""
    prompt = " ".join(topic_model.gate_prompt().split())
    assert (
        "asking for the name of the owner, a manager or a contact person is NOT about a "
        "private life." in prompt
    )
    assert (
        "how to cancel a contract with the company, its notice period or its guarantee, "
        "are NOT legal action either" in prompt
    )


def test_the_reply_is_the_text_and_not_the_reasoning() -> None:
    assert topic_model.reply_text(verdict_response('{"topic": "none"}')) == '{"topic": "none"}'
    assert topic_model.reply_text(verdict_response(None, "max_tokens")) == ""


def test_the_model_is_asked_at_temperature_zero_with_the_text_after_its_frame(
    model_aws: dict[str, Any],
) -> None:
    status, _ = call(event())
    assert status == 200
    question, answer = model_aws["topic-model"].calls
    assert question["modelId"] == "openai.gpt-oss-20b-1:0"
    assert question["system"] == [{"text": topic_model.gate_prompt()}]
    assert question["inferenceConfig"] == {"maxTokens": 512, "temperature": 0}
    assert "additionalModelRequestFields" not in question  # the model's own effort first
    assert question["messages"][0]["content"][0]["text"] == (
        "Text:\nCustomer to business: How do I export project data?"
    )
    assert answer["messages"][0]["content"][0]["text"] == (
        "Text:\nAnswer from the business to a customer: Use Settings > Export."
    )


# --- Settings ------------------------------------------------------------


def test_the_topic_model_and_its_prices_come_from_the_environment() -> None:
    settings = handler.load_settings(MODEL_ENVIRONMENT)
    assert settings.topic_model_id == "openai.gpt-oss-20b-1:0"
    assert (settings.topic_guardrail_id, settings.topic_guardrail_version) == (None, None)
    assert settings.prices.topic_model_input_tokens == Decimal("0.00009")
    assert settings.prices.topic_model_output_tokens == Decimal("0.0004")


def test_a_topic_guardrail_beside_the_topic_model_is_refused() -> None:
    with pytest.raises(handler.ConfigurationError, match="TOPIC_GUARDRAIL_ID"):
        handler.load_settings({**MODEL_ENVIRONMENT, "TOPIC_GUARDRAIL_ID": "gr-topics"})


@pytest.mark.parametrize(
    "variable",
    ["PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS", "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS"],
)
def test_the_topic_models_prices_are_required_and_checked(variable: str) -> None:
    with pytest.raises(handler.ConfigurationError, match=variable):
        handler.load_settings({**MODEL_ENVIRONMENT, variable: " "})
    with pytest.raises(handler.ConfigurationError, match=variable):
        handler.load_settings({**MODEL_ENVIRONMENT, variable: "cheap"})


def test_without_the_topic_model_the_topic_guardrail_is_required() -> None:
    environment = {k: v for k, v in MODEL_ENVIRONMENT.items() if k != "TOPIC_MODEL_ID"}
    with pytest.raises(handler.ConfigurationError, match="TOPIC_GUARDRAIL_ID"):
        handler.load_settings(environment)


# --- The way through the handler -----------------------------------------


def test_a_harmless_question_is_answered_with_the_topic_models_tokens_in_its_cost(
    model_aws: dict[str, Any],
) -> None:
    status, body = call(event())
    assert status == 200
    assert body["answer"] == "Use Settings > Export [1]."
    assert model_aws["guardrail"].question_calls == []
    assert model_aws["guardrail"].topic_calls == []
    # Model 1624 + 50 tokens; the trace's units (topic 2, PII 1, grounding 3) and the
    # excerpts' PII unit; two topic checks of 400 + 30 tokens each.
    expected = (
        1624 * Decimal("0.00015")
        + 50 * Decimal("0.00065")
        + 2 * Decimal("0.15")
        + 2 * Decimal("0.10")
        + 3 * Decimal("0.10")
        + 800 * Decimal("0.00009")
        + 60 * Decimal("0.0004")
    ) / 1000
    assert body["cost_usd"] == float(round(expected, 6))


def test_a_question_on_a_denied_topic_is_refused_before_the_model_is_asked(
    model_aws: dict[str, Any],
) -> None:
    model_aws["topic-model"].question_reply = '{"topic": "Medical advice"}'
    status, body = call(event())
    assert status == 200
    assert body["answer"] == handler.QUESTION_REFUSED_ANSWERS["en"]
    assert body["guardrail"] == {"action": "blocked", "policies": ["topic:Medical advice"]}
    assert body["sources"] == []
    assert model_aws["bedrock-runtime"].calls == []
    # The topic is asked twice more, to confirm it: all three calls are paid for.
    assert len(model_aws["topic-model"].calls) == 3
    assert body["cost_usd"] == float(
        round(3 * (400 * Decimal("0.00009") + 30 * Decimal("0.0004")) / 1000, 6)
    )


def test_an_answer_on_a_denied_topic_is_withheld_with_its_sources(
    model_aws: dict[str, Any],
) -> None:
    model_aws["topic-model"].answer_reply = '{"topic": "Tax returns"}'
    status, body = call(event())
    assert status == 200
    assert body["answer"] == handler.ANSWER_WITHHELD_ANSWERS["en"]
    assert "topic:Tax returns" in body["guardrail"]["policies"]
    assert body["sources"] == []


@pytest.mark.parametrize("side", ["question_reply", "answer_reply"])
@pytest.mark.parametrize(
    "reply", [None, "", '{"topic": "Legal advice"}', "The text is about invoices."]
)
def test_an_unreadable_verdict_lets_nothing_through(
    model_aws: dict[str, Any], side: str, reply: str | None
) -> None:
    """gpt-oss-20b once gave an empty reply (06.10.2026): passed through, it would have let a
    tax answer out unchecked."""
    setattr(model_aws["topic-model"], side, reply)
    status, body = call(event())
    assert status == 502
    assert "no readable verdict" in body["error"]
    assert "Use Settings > Export" not in str(body)


@pytest.mark.parametrize(
    "error",
    [
        "ThrottlingException",
        "AccessDeniedException",
        "ServiceUnavailableException",
        "ValidationException",
    ],
)
def test_a_failed_topic_model_call_lets_nothing_through(
    model_aws: dict[str, Any], error: str
) -> None:
    model_aws["topic-model"].error = client_error(error)
    status, body = call(event())
    assert status == 502
    assert "could not be checked for the denied topics" in body["error"]
    assert model_aws["bedrock-runtime"].calls == []


@pytest.mark.parametrize("first", ["", "The question is about the contract.", "error"])
def test_a_check_without_a_verdict_is_asked_once_more_and_counts_both_calls(
    model_aws: dict[str, Any], first: str
) -> None:
    """gpt-oss-20b once wrote one sentence over and over until its tokens ran out (06.10.2026),
    in 1 of 10 tries: a second call answers, and both are paid for."""
    reply: Any = client_error("ThrottlingException") if first == "error" else first
    model_aws["topic-model"].question_reply = [reply, '{"topic": "none"}']
    status, body = call(event())
    assert status == 200
    assert body["answer"] == "Use Settings > Export [1]."
    question_calls = model_aws["topic-model"].calls[:2]
    assert len(model_aws["topic-model"].calls) == 3
    # Both at the model's own effort: "low" let an advice question through (06.10.2026).
    assert all("additionalModelRequestFields" not in call for call in question_calls)
    paid_calls = 3 if first != "error" else 2
    without_topic_model = (
        1624 * Decimal("0.00015")
        + 50 * Decimal("0.00065")
        + 2 * Decimal("0.15")
        + 2 * Decimal("0.10")
        + 3 * Decimal("0.10")
    )
    topic_model_cost = paid_calls * (400 * Decimal("0.00009") + 30 * Decimal("0.0004"))
    assert body["cost_usd"] == float(round((without_topic_model + topic_model_cost) / 1000, 6))


def test_a_topic_named_by_the_call_in_place_of_a_failed_one_stands_unconfirmed(
    model_aws: dict[str, Any],
) -> None:
    """Two rounds fit the time of a guardrail call: none is left to confirm this topic."""
    model_aws["topic-model"].question_reply = ["", '{"topic": "Legal action"}']
    status, body = call(event())
    assert status == 200
    assert body["answer"] == handler.QUESTION_REFUSED_ANSWERS["en"]
    assert model_aws["bedrock-runtime"].calls == []
    assert len(model_aws["topic-model"].calls) == 2


NONE = '{"topic": "none"}'


@pytest.mark.parametrize(
    "confirming",
    [
        ['{"topic": "Private lives"}', NONE],
        ['{"topic": "Tax returns"}', NONE],
        ['{"topic": "Private lives"}', '{"topic": "Private lives"}'],
        ["", NONE],
        ["The question is about the owner.", NONE],
        ["error", NONE],
        ["error", "error"],
        ["", ""],
    ],
)
def test_a_topic_stands_unless_both_confirming_calls_name_none(
    model_aws: dict[str, Any], confirming: list[str]
) -> None:
    """Either confirming call naming a topic, giving no verdict or failing keeps the first one's;
    the two run at once, so their order does not matter."""
    replies: list[Any] = [
        client_error("ThrottlingException") if reply == "error" else reply for reply in confirming
    ]
    model_aws["topic-model"].question_reply = ['{"topic": "Private lives"}', *replies]
    status, body = call(event())
    assert status == 200
    assert body["answer"] == handler.QUESTION_REFUSED_ANSWERS["en"]
    assert body["guardrail"] == {"action": "blocked", "policies": ["topic:Private lives"]}
    assert model_aws["bedrock-runtime"].calls == []
    assert len(model_aws["topic-model"].calls) == 3


def test_a_topic_both_confirming_calls_deny_lets_the_question_pass(
    model_aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """gpt-oss-120b refused "Wie heißt der Inhaber von Kalvenstett Haustechnik?" now and then
    as a private life, as a single call (07.10.2026); the lifted topic is logged."""
    caplog.set_level(logging.INFO, logger="andur")
    model_aws["topic-model"].question_reply = ['{"topic": "Private lives"}', NONE, NONE]
    status, body = call(event())
    assert status == 200
    assert body["answer"] == "Use Settings > Export [1]."
    (line,) = [line for line in log_lines(caplog) if line["event"] == "topic-model-unconfirmed"]
    assert (line["source"], line["topic"]) == ("INPUT", "Private lives")
    # Three calls on the question, all paid for, one on the answer.
    assert len(model_aws["topic-model"].calls) == 4


def test_the_confirming_calls_run_at_once() -> None:
    """Each confirming call waits for the other: asked one after the other, the first of them
    would wait in vain and the check would fail, and their time would no longer fit."""
    barrier = threading.Barrier(2, timeout=5)

    class Runtime:
        def __init__(self) -> None:
            self.calls = 0

        def converse(self, **_: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return verdict_response('{"topic": "Private lives"}')
            barrier.wait()
            return verdict_response(NONE)

    settings = SimpleNamespace(topic_model_id="openai.gpt-oss-120b-1:0", region="eu-central-1")
    checked = handler.check_topics_with_model(Runtime(), settings, "INPUT", "Wer ist der Inhaber?")
    assert checked["topics"] == []
    assert checked["tokens"] == {"input_tokens": 1200, "output_tokens": 90}


def test_without_confirming_calls_the_first_topic_stands_alone(
    model_aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(topic_model, "CONFIRMING_CALLS", 0)
    model_aws["topic-model"].question_reply = ['{"topic": "Private lives"}', NONE, NONE]
    status, body = call(event())
    assert status == 200
    assert body["answer"] == handler.QUESTION_REFUSED_ANSWERS["en"]
    assert len(model_aws["topic-model"].calls) == 1


def test_a_topic_model_that_times_out_lets_nothing_through(model_aws: dict[str, Any]) -> None:
    exceptions = pytest.importorskip("botocore.exceptions")
    model_aws["topic-model"].error = exceptions.ReadTimeoutError(endpoint_url="https://bedrock")
    status, body = call(event())
    assert status == 502
    assert "no response within the timeout" in body["error"]


def test_a_failed_check_says_so_in_german_for_a_german_request(
    model_aws: dict[str, Any],
) -> None:
    model_aws["topic-model"].question_reply = ""
    body = {"question": "Wie exportiere ich Daten?", "workspace": "quillmoor", "language": "de"}
    status, response = call(event(body))
    assert status == 502
    assert response["error"] == (
        "Die Frage konnte nicht auf die gesperrten Themen geprüft werden: "
        "das Themen-Modell hat kein lesbares Urteil geliefert."
    )


def test_the_topic_model_is_called_through_its_own_client(
    model_aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    clients = dict(model_aws)
    monkeypatch.setattr(
        handler, "client", lambda service, region: asked.append(service) or clients[service]
    )
    call(event())
    assert asked.count("topic-model") == 2
    assert asked.count("guardrail") == 1  # the excerpts alone

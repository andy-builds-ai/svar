"""Tests for the answer function (app/handler.py), without AWS.

Every AWS client is a small fake, and every Bedrock response a hand-built
dict in the shape Bedrock returns — the pattern of the learning chatbot's
tests. Error paths use botocore's real exception classes and are skipped
when botocore is not installed; one test proves the module imports without
boto3 at all. The module is imported by its Lambda name (tests/conftest.py).

The budget fake goes one step further and runs the handler's own update and
condition strings, so a wrong attribute name or a wrong placeholder fails
here. What it cannot prove is that DynamoDB reads those strings the same way;
that is what the measurement against the deployed table is for.
"""

import base64
import json
import logging
import operator
import subprocess
import sys
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import handler  # app/handler.py
import pytest
from handler import (
    BUDGET_SPENT_MESSAGE,
    KEY_HEADER,
    MAX_ANSWER_TOKENS,
    MAX_QUESTION_CHARACTERS,
    MAX_SOURCE_CHARACTERS,
    NO_SOURCES_ANSWER,
    NOT_CONFIGURED_MESSAGE,
    NOT_IN_SOURCES_ANSWER,
    QUESTIONS_SPENT_MESSAGE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    WINDOW_CLOSED_MESSAGE,
    BadRequest,
    BudgetSpent,
    ConfigurationError,
    Forbidden,
    LLMError,
    MethodNotAllowed,
    RetrievalError,
    Unauthorized,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

ENVIRONMENT = {
    "KNOWLEDGE_BASE_ID": "KB123",
    "MODEL_ID": "nvidia.nemotron-super-3-120b",
    "REGION": "us-east-1",
    "NUMBER_OF_RESULTS": "6",
    "API_KEY_PARAMETER_PATH": "/andur/api-keys",
    "BUDGET_TABLE_NAME": "andur-key-budgets",
    "QUESTION_LIMIT": "10",
    "WINDOW_HOURS": "48",
    "KEEP_DAYS": "365",
    "GUARDRAIL_ID": "gr-test",
    "GUARDRAIL_VERSION": "1",
    "EXCERPT_GUARDRAIL_ID": "gr-excerpts",
    "EXCERPT_GUARDRAIL_VERSION": "2",
    "PRICE_INPUT_PER_1K_TOKENS": "0.00015",
    "PRICE_OUTPUT_PER_1K_TOKENS": "0.00065",
    "PRICE_TOPIC_PER_1K_UNITS": "0.15",
    "PRICE_PII_PER_1K_UNITS": "0.10",
    "PRICE_GROUNDING_PER_1K_UNITS": "0.10",
    "PRICE_DATE": "2026-09-23",
}
SETTINGS = handler.load_settings(ENVIRONMENT)
KEYS = {"quillmoor": "quillmoor-key-0001", "pellwick": "pellwick-key-0002"}


# --- Fakes ---------------------------------------------------------------


def client_error(code: str, message: str = "details", operation: str = "Converse") -> Exception:
    exceptions = pytest.importorskip("botocore.exceptions")
    return exceptions.ClientError({"Error": {"Code": code, "Message": message}}, operation)


def hit(
    layer: str = "human",
    name: str = "faq--03-projects-and-exports.md",
    text: str = "Open the project and go to Settings > Export.",
    workspace: str = "quillmoor",
    score: float = 0.8123456,
) -> dict[str, Any]:
    """One retrieve result, shaped like Bedrock's."""
    return {
        "content": {"text": text},
        "location": {
            "s3Location": {
                "uri": f"s3://andur-documents-000000000000-us-east-1/workspaces/{workspace}/{layer}/{name}"
            },
            "type": "S3",
        },
        "metadata": {"workspace": workspace, "layer": layer, "source": "fictional"},
        "score": score,
    }


def guardrail_usage(topic: int = 1, pii: int = 1, grounding: int = 3) -> dict[str, int]:
    """The ``usage`` of one assessment's ``invocationMetrics``, as the trace reports it."""
    return {
        "topicPolicyUnits": topic,
        "contentPolicyUnits": 0,
        "wordPolicyUnits": 0,
        "sensitiveInformationPolicyUnits": pii,
        "sensitiveInformationPolicyFreeUnits": 0,
        "contextualGroundingPolicyUnits": grounding,
    }


def guardrail_trace(
    input_assessment: dict[str, Any] | None = None,
    output_assessment: dict[str, Any] | None = None,
    model_output: str = "Call Ada Quill on +44 20 7946 0958 [1].",
) -> dict[str, Any]:
    """A guardrail trace as Converse returns it with ``trace: enabled``.

    The input is read by the topic filter only; the output by all three
    policies. ``modelOutput`` is the answer before masking.
    """
    if input_assessment is None:
        input_assessment = {
            "topicPolicy": {"topics": []},
            "invocationMetrics": {
                "guardrailProcessingLatency": 120,
                "usage": guardrail_usage(1, 0, 0),
            },
        }
    if output_assessment is None:
        output_assessment = {
            "contextualGroundingPolicy": {
                "filters": [
                    {
                        "type": "GROUNDING",
                        "threshold": 0.7,
                        "score": 0.93,
                        "action": "NONE",
                        "detected": False,
                    },
                    {
                        "type": "RELEVANCE",
                        "threshold": 0.7,
                        "score": 0.88,
                        "action": "NONE",
                        "detected": False,
                    },
                ]
            },
            "invocationMetrics": {
                "guardrailProcessingLatency": 300,
                "usage": guardrail_usage(1, 1, 3),
            },
        }
    return {
        "guardrail": {
            "modelOutput": [model_output],
            "inputAssessment": {"gr-test": input_assessment},
            "outputAssessments": {"gr-test": [output_assessment]},
        }
    }


def bedrock_response(
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The shape the Bedrock converse API returns, with a guardrail trace that did nothing."""
    return {
        "output": {"message": {"content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 1624, "outputTokens": 50, "totalTokens": 1674},
        "trace": trace if trace is not None else guardrail_trace(),
    }


def parameters(keys: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"Name": f"/andur/api-keys/{workspace}", "Value": key} for workspace, key in keys.items()
    ]


class FakeAgentRuntime:
    def __init__(
        self, results: list[dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self.results = results if results is not None else []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"retrievalResults": self.results}


class FakeBedrockRuntime:
    def __init__(
        self, response: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.response = response or bedrock_response([{"text": "Use Settings > Export [1]."}])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def excerpt_guard_response(
    outputs: list[str] | None = None, pii_units: int = 1, masked: tuple[str, ...] = ()
) -> dict[str, Any]:
    """What ApplyGuardrail returns: nothing to mask (``outputs`` None), or the masked text."""
    return {
        "usage": {
            "topicPolicyUnits": 0,
            "sensitiveInformationPolicyUnits": pii_units,
            "contextualGroundingPolicyUnits": 0,
        },
        "action": "NONE" if outputs is None else "GUARDRAIL_INTERVENED",
        "outputs": [] if outputs is None else [{"text": text} for text in outputs],
        "assessments": [
            {
                "sensitiveInformationPolicy": {
                    "piiEntities": [
                        {
                            "match": "Anja Pell",
                            "type": pii_type,
                            "action": "ANONYMIZED",
                            "detected": True,
                        }
                        for pii_type in masked
                    ]
                }
            }
        ],
    }


class FakeGuardrail:
    """The excerpt guardrail's client: ApplyGuardrail only."""

    def __init__(
        self, response: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.response = response or excerpt_guard_response()
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeSSM:
    """Parameter Store; ``pages`` is the store as it is now and can change between reads."""

    def __init__(
        self, pages: list[list[dict[str, str]]] | None = None, error: Exception | None = None
    ) -> None:
        self.pages = pages if pages is not None else []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_paginator(self, operation: str) -> "FakeSSM":
        assert operation == "get_parameters_by_path"
        return self

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return iter({"Parameters": page} for page in self.pages)


def conditional_check_failed(item: dict[str, Any] | None) -> Exception:
    """What DynamoDB raises when the update's condition is false."""
    exceptions = pytest.importorskip("botocore.exceptions")
    response: dict[str, Any] = {
        "Error": {
            "Code": "ConditionalCheckFailedException",
            "Message": "The conditional request failed",
        }
    }
    if item is not None:
        response["Item"] = item
    return exceptions.ClientError(response, "UpdateItem")


def split_on(text: str, separator: str) -> list[str]:
    """Split on a separator that is not inside brackets — if_not_exists holds a comma."""
    parts, depth, current = [], 0, ""
    for character in text:
        depth += (character == "(") - (character == ")")
        if character == separator and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += character
    return [part.strip() for part in [*parts, current] if part.strip()]


def operand(text: str, before: dict[str, Any], values: dict[str, Any]) -> dict[str, str]:
    """One operand of a SET action: a placeholder, an attribute, or if_not_exists of both."""
    if text.startswith(":"):
        return values[text]
    if text.startswith("if_not_exists("):
        path, fallback = split_on(text[len("if_not_exists(") : -1], ",")
        return before.get(path, values[fallback])
    return before[text]


def apply_update(expression: str, before: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """The SET actions of an update expression, every operand read from `before`.

    That is DynamoDB's rule: the actions of one expression are applied
    together, so an operand always names the value an attribute had before
    the update, even when another action in the same expression changes it.
    """
    assert expression.startswith("SET "), expression
    after = dict(before)
    for action in split_on(expression.removeprefix("SET "), ","):
        path, _, value = action.partition(" = ")
        summands = split_on(value, "+")
        resolved = [operand(part, before, values) for part in summands]
        after[path] = (
            resolved[0]
            if len(resolved) == 1
            else {"N": str(sum(int(number["N"]) for number in resolved))}
        )
    return after


#: The comparisons a condition expression can make on numbers.
COMPARISONS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "=": operator.eq,
    "<>": operator.ne,
}


class ConditionReader:
    """A condition expression, read from its text and evaluated on one item.

    Written the way `apply_update` is written, and for the same reason: what
    the handler sends is a string, and a fake that reproduces its *meaning*
    from memory proves nothing about the string. Spelled out by hand, a
    ``<`` that turned into ``<=`` in `handler.BUDGET_CONDITION` would leave
    every test green while the deployed budget let an eleventh question
    through. Read from the text, the same change turns tests red.

    Enough of the grammar for the conditions this handler sends: ``AND``,
    ``OR``, brackets, ``attribute_exists`` and ``attribute_not_exists``, and
    a comparison between two operands, each an attribute name or a ``:``
    placeholder. DynamoDB's rule for a missing attribute is kept: it does not
    raise, it makes the comparison false.
    """

    def __init__(self, expression: str) -> None:
        self.tokens = expression.replace("(", " ( ").replace(")", " ) ").split()
        self.at = 0

    def peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> str:
        token = self.tokens[self.at]
        self.at += 1
        return token

    def either(self, item: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
        """``a OR b``, with both sides always read — the reader may not skip tokens."""
        held = self.both(item, values)
        while self.peek() == "OR":
            self.take()
            held = self.both(item, values) or held
        return held

    def both(self, item: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
        held = self.term(item, values)
        while self.peek() == "AND":
            self.take()
            held = self.term(item, values) and held
        return held

    def term(self, item: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
        if self.peek() == "(":
            self.take()
            held = self.either(item, values)
            assert self.take() == ")", "a bracket was not closed"
            return held
        name = self.take()
        if self.peek() == "(":
            self.take()
            path = self.take()
            assert self.take() == ")", "a bracket was not closed"
            assert name in {"attribute_exists", "attribute_not_exists"}, name
            return (path in item) == (name == "attribute_exists")
        comparison = self.take()
        assert comparison in COMPARISONS, comparison
        left, right = self.number(name, item, values), self.number(self.take(), item, values)
        if left is None or right is None:
            return False
        return COMPARISONS[comparison](left, right)

    @staticmethod
    def number(token: str, item: Mapping[str, Any], values: Mapping[str, Any]) -> int | None:
        """One side of a comparison: a placeholder or an attribute, as a whole number."""
        source = values if token.startswith(":") else item
        entry = source.get(token)
        if not isinstance(entry, Mapping) or "N" not in entry:
            return None
        return int(entry["N"])


def condition_holds(expression: str, item: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    """Whether DynamoDB would let an update with this condition through."""
    reader = ConditionReader(expression)
    held = reader.either(item, values)
    assert reader.peek() is None, f"not read to the end: {expression}"
    return held


class FakeDynamoDB:
    """The budget table: one item per key hash, and this handler's updates.

    ``items`` is the table as it is now and can be changed between calls, the
    way a CLI command would change it. Both expressions are executed, not
    described: `apply_update` for the SET actions, `condition_holds` for the
    condition.
    """

    def __init__(self, error: Exception | None = None, error_from_call: int = 1) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.error = error
        #: Which call starts failing. A table that only fails the second write
        #: is how a repair that does not get through is tested.
        self.error_from_call = error_from_call
        self.calls: list[dict[str, Any]] = []

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error and len(self.calls) >= self.error_from_call:
            raise self.error
        key = kwargs["Key"][handler.BUDGET_KEY_ATTRIBUTE]["S"]
        before = self.items.get(key, {})
        values = kwargs.get("ExpressionAttributeValues", {})
        condition = kwargs.get("ConditionExpression")
        if condition is not None and not condition_holds(condition, before, values):
            returned = (
                before if kwargs.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD" else None
            )
            raise conditional_check_failed(returned or None)
        after = apply_update(kwargs["UpdateExpression"], before, values)
        self.items[key] = after
        return {"Attributes": after} if kwargs.get("ReturnValues") == "ALL_NEW" else {}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWallClock(FakeClock):
    """The clock the budget runs on. Starts at a fixed moment in 2026."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 1_790_000_000.0


@pytest.fixture(autouse=True)
def cold_start(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Every test starts as a fresh function instance: no keys read, a clock of its own."""
    clock = FakeClock()
    monkeypatch.setattr(handler, "_key_cache", handler.KeyCache())
    monkeypatch.setattr(handler, "clock", clock)
    monkeypatch.setattr(handler, "wall_clock", FakeWallClock())
    handler.log_sdk_versions.cache_clear()
    return clock


@pytest.fixture
def budget_clock(monkeypatch: pytest.MonkeyPatch) -> FakeWallClock:
    """The budget's clock, for a test that has to move time forward."""
    clock = FakeWallClock()
    monkeypatch.setattr(handler, "wall_clock", clock)
    return clock


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The function's environment, its keys and one fake client per service."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    clients: dict[str, Any] = {
        "ssm": FakeSSM([parameters(KEYS)]),
        "bedrock-agent-runtime": FakeAgentRuntime(
            [hit(), hit(layer="inbox", name="draft--01.md", text="Draft.")]
        ),
        "bedrock-runtime": FakeBedrockRuntime(),
        "guardrail": FakeGuardrail(),
        "dynamodb": FakeDynamoDB(),
    }
    monkeypatch.setattr(handler, "client", lambda service, region: clients[service])
    return clients


def event(
    body: Any = None,
    key: str | None = KEYS["quillmoor"],
    method: str = "POST",
    **extra: Any,
) -> dict[str, Any]:
    """A Function URL event (payload format 2.0)."""
    if body is None:
        body = {"question": "How do I export project data?", "workspace": "quillmoor"}
    # Lowercase: that is how a Function URL hands header names over.
    headers = {"content-type": "application/json"}
    if key is not None:
        headers[KEY_HEADER.lower()] = key
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": method}},
        "headers": headers,
        "body": body if isinstance(body, str) else json.dumps(body),
        "isBase64Encoded": False,
        **extra,
    }


def respond(request: dict[str, Any]) -> dict[str, Any]:
    response = handler.handler(request, None)
    assert response["headers"]["Content-Type"].startswith("application/json")
    return response


def call(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = respond(request)
    return response["statusCode"], json.loads(response["body"])


# --- Settings ------------------------------------------------------------


def test_settings_come_from_the_environment() -> None:
    assert SETTINGS.knowledge_base_id == "KB123"
    assert SETTINGS.model_id == "nvidia.nemotron-super-3-120b"
    assert SETTINGS.number_of_results == 6
    assert SETTINGS.key_parameter_path == "/andur/api-keys"
    assert SETTINGS.budget_table == "andur-key-budgets"
    assert (SETTINGS.question_limit, SETTINGS.window_hours, SETTINGS.keep_days) == (10, 48, 365)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("QUESTION_LIMIT", "0"),
        ("QUESTION_LIMIT", "ten"),
        ("QUESTION_LIMIT", "-1"),
        ("WINDOW_HOURS", "0"),
        ("WINDOW_HOURS", "48.5"),
        ("KEEP_DAYS", "0"),
        ("KEEP_DAYS", "many"),
    ],
)
def test_a_budget_setting_must_be_a_sensible_whole_number(variable: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=variable):
        handler.load_settings({**ENVIRONMENT, variable: value})


@pytest.mark.parametrize("value", ["²", "٣", "１０"])
def test_a_digit_that_is_not_an_ascii_digit_names_the_variable_too(value: str) -> None:
    """`isdigit()` alone says yes to these, and `int()` then says no with a bare ValueError."""
    with pytest.raises(ConfigurationError, match="QUESTION_LIMIT"):
        handler.load_settings({**ENVIRONMENT, "QUESTION_LIMIT": value})


def test_the_item_must_outlast_the_window_it_holds() -> None:
    """An item that goes first hands the key a fresh budget while the old one runs."""
    with pytest.raises(ConfigurationError, match="KEEP_DAYS"):
        handler.load_settings({**ENVIRONMENT, "WINDOW_HOURS": "24", "KEEP_DAYS": "1"})
    handler.load_settings({**ENVIRONMENT, "WINDOW_HOURS": "23", "KEEP_DAYS": "1"})


@pytest.mark.parametrize("variable", sorted(ENVIRONMENT))
def test_a_missing_setting_is_named(variable: str) -> None:
    environment = {**ENVIRONMENT, variable: "  "}
    with pytest.raises(ConfigurationError, match=variable):
        handler.load_settings(environment)


@pytest.mark.parametrize("value", ["0", "31", "101", "six", "-1", "6.5"])
def test_number_of_results_must_be_a_sensible_whole_number(value: str) -> None:
    with pytest.raises(ConfigurationError, match="NUMBER_OF_RESULTS"):
        handler.load_settings({**ENVIRONMENT, "NUMBER_OF_RESULTS": value})


def test_the_most_passages_a_deployment_may_ask_for_fit_the_grounding_source() -> None:
    """The guardrail takes at most 100,000 characters of grounding source.

    Thirty passages, every one cut at its limit, with a header naming a long
    file: if that fits, every configurable number of results does.
    """
    long_name = "a-rather-long-file-name-for-a-document--" + "x" * 60 + ".md"
    results = [hit(name=long_name, text="word " * 2000) for _ in range(handler.MAX_RESULTS)]
    assert len(handler.build_sources(results)) < 100_000


def test_guardrail_and_prices_come_from_the_environment() -> None:
    assert (SETTINGS.guardrail_id, SETTINGS.guardrail_version) == ("gr-test", "1")
    assert (SETTINGS.excerpt_guardrail_id, SETTINGS.excerpt_guardrail_version) == (
        "gr-excerpts",
        "2",
    )
    assert SETTINGS.prices == handler.Prices(
        input_tokens=Decimal("0.00015"),
        output_tokens=Decimal("0.00065"),
        topic_units=Decimal("0.15"),
        sensitive_information_units=Decimal("0.10"),
        grounding_units=Decimal("0.10"),
    )
    assert SETTINGS.price_date == "2026-09-23"


@pytest.mark.parametrize("variable", ["GUARDRAIL_VERSION", "EXCERPT_GUARDRAIL_VERSION"])
@pytest.mark.parametrize("value", ["DRAFT", "", "1.0", "v1", "²"])
def test_the_guardrail_version_is_a_number_and_never_the_draft(variable: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=variable):
        handler.load_settings({**ENVIRONMENT, variable: value})


@pytest.mark.parametrize(
    "variable",
    [
        "PRICE_INPUT_PER_1K_TOKENS",
        "PRICE_OUTPUT_PER_1K_TOKENS",
        "PRICE_TOPIC_PER_1K_UNITS",
        "PRICE_PII_PER_1K_UNITS",
        "PRICE_GROUNDING_PER_1K_UNITS",
    ],
)
@pytest.mark.parametrize("value", ["cheap", "-0.1", "11", "NaN", "Infinity", "0,15"])
def test_a_price_must_be_a_plain_decimal_in_range(variable: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=variable):
        handler.load_settings({**ENVIRONMENT, variable: value})


@pytest.mark.parametrize(
    "value", ["23.09.2026", "2026-9-23", "2026-02-30", "yesterday", "20260923"]
)
def test_the_price_date_is_a_real_iso_date(value: str) -> None:
    with pytest.raises(ConfigurationError, match="PRICE_DATE"):
        handler.load_settings({**ENVIRONMENT, "PRICE_DATE": value})


@pytest.mark.parametrize("path", ["/andur/api-keys/", "andur/api-keys"])
def test_key_path_has_a_leading_slash_and_no_trailing_one(path: str) -> None:
    with pytest.raises(ConfigurationError, match="API_KEY_PARAMETER_PATH"):
        handler.load_settings({**ENVIRONMENT, "API_KEY_PARAMETER_PATH": path})


def test_workspace_rule_and_layers_match_the_fill_script() -> None:
    """The handler cannot import scripts/ (Lambda gets app/ only), so a test keeps them equal."""
    ingest = pytest.importorskip("scripts.ingest")

    assert handler.WORKSPACE_NAME.pattern == ingest.WORKSPACE_NAME.pattern
    assert handler.LAYERS == ingest.LAYERS
    assert handler.MAX_WORKSPACE_CHARACTERS == ingest.MAX_WORKSPACE_CHARACTERS


# --- Reading the request -------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PUT", "OPTIONS", ""])
def test_only_post_is_accepted(method: str) -> None:
    with pytest.raises(MethodNotAllowed):
        handler.require_post({"requestContext": {"http": {"method": method}}})


def test_body_is_read_as_json_also_when_base64_encoded() -> None:
    body = {"question": "How?", "workspace": "quillmoor"}
    encoded = base64.b64encode(json.dumps(body).encode()).decode()
    assert handler.read_body({"body": json.dumps(body)}) == body
    assert handler.read_body({"body": encoded, "isBase64Encoded": True}) == body


@pytest.mark.parametrize(
    "request_event",
    [
        {"body": "{broken"},
        {"body": None},
        {},
        {"body": "[1, 2]"},
        {"body": "%%%", "isBase64Encoded": True},
    ],
    ids=["broken", "none", "missing", "array", "bad-base64"],
)
def test_a_body_that_is_no_json_object_is_a_bad_request(request_event: dict[str, Any]) -> None:
    with pytest.raises(BadRequest):
        handler.read_body(request_event)


def test_a_number_past_the_digit_limit_is_a_bad_request_not_a_crash() -> None:
    """Python refuses to turn more than 4,300 digits into an int (ValueError)."""
    with pytest.raises(BadRequest, match="not valid JSON"):
        handler.read_body({"body": '{"question": ' + "1" * 5000 + "}"})


def test_a_body_nested_past_the_recursion_limit_is_a_bad_request_not_a_crash() -> None:
    with pytest.raises(BadRequest, match="not valid JSON"):
        handler.read_body({"body": "[" * 100_000 + "]" * 100_000})


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"workspace": ""},
        {"workspace": 7},
        {"workspace": "Quill Moor"},
        {"workspace": "a--b"},
        {"workspace": "quillmoor\n"},
    ],
    ids=[
        "missing",
        "empty",
        "not-a-string",
        "spaces-and-capitals",
        "double-hyphen",
        "trailing-newline",
    ],
)
def test_workspace_must_be_a_valid_name(body: dict[str, Any]) -> None:
    with pytest.raises(BadRequest):
        handler.requested_workspace(body)


def test_a_workspace_name_at_the_limit_passes_and_one_over_it_does_not() -> None:
    at_limit = "a" * handler.MAX_WORKSPACE_CHARACTERS
    assert handler.requested_workspace({"workspace": at_limit}) == at_limit
    with pytest.raises(BadRequest, match=str(handler.MAX_WORKSPACE_CHARACTERS)):
        handler.requested_workspace({"workspace": "a" * (handler.MAX_WORKSPACE_CHARACTERS + 1)})


def test_an_outsized_workspace_name_is_400_and_reaches_neither_log_nor_answer(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The name is the one thing from outside that a log line carries, so it is capped first."""
    caplog.set_level(logging.INFO, logger="andur")
    huge = "q" * 5000

    status, body = call(event({"question": "How?", "workspace": huge}))
    assert status == 400
    assert huge not in json.dumps(body)
    assert huge not in caplog.text
    (line,) = [line for line in log_lines(caplog) if line["event"] == "refused"]
    assert line["workspace"] is None
    assert aws["dynamodb"].calls == []


def test_question_is_stripped_and_limited() -> None:
    assert handler.requested_question({"question": "  How?  "}) == "How?"
    assert (
        len(handler.requested_question({"question": "x" * MAX_QUESTION_CHARACTERS}))
        == MAX_QUESTION_CHARACTERS
    )
    for body in (
        {},
        {"question": None},
        {"question": " \n "},
        {"question": "x" * (MAX_QUESTION_CHARACTERS + 1)},
    ):
        with pytest.raises(BadRequest):
            handler.requested_question(body)


def test_the_key_is_read_from_its_own_header_in_any_case() -> None:
    assert handler.request_key({KEY_HEADER.lower(): "abc"}) == "abc"
    assert handler.request_key({KEY_HEADER.upper(): "  abc "}) == "abc"
    assert handler.request_key({"X-aNdUr-KeY": "abc"}) == "abc"


def test_a_key_in_the_authorization_header_is_not_a_key() -> None:
    """CloudFront overwrites that header on its way to the function; nothing may read it."""
    with pytest.raises(Unauthorized):
        handler.request_key({"authorization": "Bearer abc"})


@pytest.mark.parametrize(
    "headers",
    [None, {}, {"x-api-key": "abc"}, {KEY_HEADER.lower(): ""}, {KEY_HEADER.lower(): "   "}],
)
def test_no_key_header_is_unauthorized(headers: dict[str, str] | None) -> None:
    with pytest.raises(Unauthorized):
        handler.request_key(headers)


def test_the_401_does_not_announce_an_authentication_scheme() -> None:
    """``WWW-Authenticate`` names a scheme for a header the key does not travel in."""
    assert Unauthorized.headers == {}


# --- Keys ----------------------------------------------------------------


def test_keys_are_mapped_to_workspaces_by_parameter_name() -> None:
    entries = [
        {"Name": "/andur/api-keys/quillmoor", "Value": "K1"},
        {"Name": "/andur/api-keys/Not A Workspace", "Value": "K2"},
        {"Name": "/andur/api-keys-other/pellwick", "Value": "K3"},
        {"Name": "/other/pellwick", "Value": "K4"},
    ]
    assert handler.keys_by_workspace(entries, "/andur/api-keys") == {"quillmoor": "K1"}


def test_a_key_opens_only_its_own_workspace() -> None:
    assert handler.workspace_for_key(KEYS["pellwick"], KEYS) == "pellwick"
    assert handler.workspace_for_key("unknown", KEYS) is None
    assert handler.workspace_for_key("", KEYS) is None
    assert handler.workspace_for_key("schlüssel", KEYS) is None

    handler.check_access(KEYS["quillmoor"], "quillmoor", KEYS)
    with pytest.raises(Unauthorized):
        handler.check_access("unknown", "quillmoor", KEYS)
    with pytest.raises(Forbidden):
        handler.check_access(KEYS["quillmoor"], "pellwick", KEYS)


def test_every_key_is_compared_even_after_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """No early exit: the number of comparisons does not depend on which key matched."""
    compared: list[bytes] = []
    real_compare = handler.hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        compared.append(b)
        return real_compare(a, b)

    monkeypatch.setattr(handler.hmac, "compare_digest", counting)
    handler.workspace_for_key(KEYS["quillmoor"], KEYS)
    assert len(compared) == len(KEYS)


def test_keys_are_read_decrypted_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    ssm = FakeSSM([parameters({"quillmoor": "K1"}), parameters({"pellwick": "K2"})])
    monkeypatch.setattr(handler, "client", lambda service, region: ssm)
    assert handler.read_workspace_keys("/andur/api-keys", "us-east-1") == {
        "quillmoor": "K1",
        "pellwick": "K2",
    }
    assert ssm.calls == [{"Path": "/andur/api-keys", "Recursive": False, "WithDecryption": True}]


def test_a_key_shared_by_two_workspaces_opens_neither(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    ssm = FakeSSM([parameters({"quillmoor": "SAME", "pellwick": "SAME", "third": "OWN"})])
    monkeypatch.setattr(handler, "client", lambda service, region: ssm)

    assert handler.read_workspace_keys("/andur/api-keys", "us-east-1") == {"third": "OWN"}
    (line,) = [line for line in log_lines(caplog) if line["event"] == "duplicate-key"]
    assert line["workspaces"] == ["pellwick", "quillmoor"]
    assert "SAME" not in caplog.text


def test_shared_key_workspaces() -> None:
    assert handler.shared_key_workspaces({"a": "K1", "b": "K1", "c": "K2"}) == {"a", "b"}
    assert handler.shared_key_workspaces(KEYS) == set()


def test_no_usable_keys_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for store in ([[]], [parameters({"a": "SAME", "b": "SAME"})]):
        monkeypatch.setattr(handler, "client", lambda service, region, store=store: FakeSSM(store))
        with pytest.raises(ConfigurationError, match="No usable workspace keys"):
            handler.read_workspace_keys("/andur/api-keys", "us-east-1")


def test_unreadable_keys_are_a_configuration_error_and_not_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = FakeSSM(error=client_error("AccessDeniedException", operation="GetParametersByPath"))
    monkeypatch.setattr(handler, "client", lambda service, region: failing)
    with pytest.raises(ConfigurationError, match="could not be read: access denied"):
        handler.workspace_keys("/andur/api-keys", "us-east-1")
    assert handler._key_cache.read_at is None

    monkeypatch.setattr(
        handler, "client", lambda service, region: FakeSSM([parameters({"quillmoor": "K1"})])
    )
    assert handler.workspace_keys("/andur/api-keys", "us-east-1") == {"quillmoor": "K1"}


def test_keys_are_kept_until_they_expire(
    monkeypatch: pytest.MonkeyPatch, cold_start: FakeClock
) -> None:
    ssm = FakeSSM([parameters(KEYS)])
    monkeypatch.setattr(handler, "client", lambda service, region: ssm)

    handler.workspace_keys("/andur/api-keys", "us-east-1")
    cold_start.advance(handler.KEY_CACHE_SECONDS - 1)
    handler.workspace_keys("/andur/api-keys", "us-east-1")
    assert len(ssm.calls) == 1

    cold_start.advance(1)
    handler.workspace_keys("/andur/api-keys", "us-east-1")
    assert len(ssm.calls) == 2


def test_a_deleted_key_stops_working_once_the_keys_expire(
    aws: dict[str, Any], cold_start: FakeClock
) -> None:
    assert call(event())[0] == 200
    aws["ssm"].pages = [parameters({"pellwick": KEYS["pellwick"]})]

    cold_start.advance(handler.KEY_CACHE_SECONDS)
    assert call(event())[0] == 401


def test_a_new_key_works_at_once_without_waiting_for_expiry(
    aws: dict[str, Any], cold_start: FakeClock
) -> None:
    assert call(event())[0] == 200
    aws["ssm"].pages = [parameters({**KEYS, "newcomer": "newcomer-key-0003"})]
    cold_start.advance(handler.KEY_RELOAD_MIN_SECONDS)

    status, _ = call(event({"question": "How?", "workspace": "newcomer"}, key="newcomer-key-0003"))
    assert status == 200
    assert len(aws["ssm"].calls) == 2


def test_an_unknown_key_rereads_the_keys_once_per_request_not_twice(aws: dict[str, Any]) -> None:
    """On a cold start the keys were just read; an unknown key does not read them again."""
    assert call(event(key="wrong"))[0] == 401
    assert len(aws["ssm"].calls) == 1


def test_a_flood_of_unknown_keys_rereads_at_most_once_per_interval(
    aws: dict[str, Any], cold_start: FakeClock
) -> None:
    call(event())
    cold_start.advance(handler.KEY_RELOAD_MIN_SECONDS)
    for _ in range(20):
        assert call(event(key="wrong"))[0] == 401
        cold_start.advance(0.1)
    assert len(aws["ssm"].calls) == 2

    cold_start.advance(handler.KEY_RELOAD_MIN_SECONDS)
    call(event(key="wrong"))
    assert len(aws["ssm"].calls) == 3


def test_a_failed_reread_still_answers_an_unknown_key_with_401(
    aws: dict[str, Any], cold_start: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    call(event())
    aws["ssm"].error = client_error("ThrottlingException", operation="GetParametersByPath")
    cold_start.advance(handler.KEY_RELOAD_MIN_SECONDS)

    assert call(event(key="wrong"))[0] == 401
    assert call(event())[0] == 200
    assert any(line["event"] == "keys-reload-failed" for line in log_lines(caplog))


def test_expired_keys_that_cannot_be_read_again_keep_working(
    aws: dict[str, Any], cold_start: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """A brief Parameter Store outage must not turn every request into a 500.

    The keys in hand are old, not wrong. They go on deciding, and the next
    attempt waits `KEY_RELOAD_MIN_SECONDS` — the same brake an unknown key
    runs into, so an outage cannot become a read per request.
    """
    caplog.set_level(logging.INFO, logger="andur")
    call(event())
    aws["ssm"].error = client_error("ThrottlingException", operation="GetParametersByPath")
    cold_start.advance(handler.KEY_CACHE_SECONDS)

    assert call(event())[0] == 200
    assert len(aws["ssm"].calls) == 2
    (line,) = [line for line in log_lines(caplog) if line["event"] == "keys-reload-failed"]
    assert line["stale"] is True

    cold_start.advance(handler.KEY_RELOAD_MIN_SECONDS - 1)
    assert call(event())[0] == 200
    assert len(aws["ssm"].calls) == 2

    cold_start.advance(1)
    assert call(event())[0] == 200
    assert len(aws["ssm"].calls) == 3


def test_keys_deleted_from_the_store_stop_working_at_once(
    aws: dict[str, Any], cold_start: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty key path is an answer, not an outage.

    Deleting every parameter under the path is how all workspaces are
    retired at once. The store is readable and says there are no keys, so the
    keys in hand are not old but gone. Treated as a failed read, they would
    have gone on opening workspaces until a deploy wrote new ones — and the
    five minutes the README promises would have meant nothing.
    """
    caplog.set_level(logging.INFO, logger="andur")
    assert call(event())[0] == 200
    aws["ssm"].pages = []
    cold_start.advance(handler.KEY_CACHE_SECONDS)

    status, body = call(event())
    assert status == 500
    assert body == {"error": NOT_CONFIGURED_MESSAGE}
    assert handler._key_cache.keys == {}
    assert not [line for line in log_lines(caplog) if line["event"] == "keys-reload-failed"]
    # The caller reads the fixed sentence; the reason is in the log.
    (line,) = [line for line in log_lines(caplog) if line["event"] == "failed"]
    assert line["status"] == 500 and "No usable workspace keys" in line["error"]

    # And it stays refused: nothing old is left to fall back on.
    assert call(event())[0] == 500


def test_an_unreadable_store_and_an_empty_one_are_not_the_same_error() -> None:
    """The stale path catches one and not the other; that is what the class is for."""
    assert issubclass(handler.NoWorkspaceKeys, handler.ConfigurationError)
    assert handler.NoWorkspaceKeys.status == 500


def test_a_function_that_never_read_any_keys_lets_nobody_in(aws: dict[str, Any]) -> None:
    """There is nothing old to fall back on, so this stays a 500."""
    aws["ssm"].error = client_error("ThrottlingException", operation="GetParametersByPath")
    status, body = call(event())
    assert status == 500
    assert body == {"error": NOT_CONFIGURED_MESSAGE}


def test_an_expired_key_still_stops_working_once_the_keys_can_be_read_again(
    aws: dict[str, Any], cold_start: FakeClock
) -> None:
    """The price of keeping old keys: a deleted key lives until the next good read."""
    assert call(event())[0] == 200
    aws["ssm"].error = client_error("ThrottlingException", operation="GetParametersByPath")
    cold_start.advance(handler.KEY_CACHE_SECONDS)
    assert call(event())[0] == 200

    aws["ssm"].error = None
    aws["ssm"].pages = [parameters({"pellwick": KEYS["pellwick"]})]
    cold_start.advance(handler.KEY_RELOAD_MIN_SECONDS)
    assert call(event())[0] == 401


# --- AWS clients ---------------------------------------------------------


def test_missing_boto3_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import aws as aws_module

    def no_sdk(service: str, region: str) -> Any:
        raise ImportError("simulated: boto3 not installed")

    monkeypatch.setattr(aws_module, "client", no_sdk)
    with pytest.raises(ConfigurationError, match="boto3"):
        handler.client("ssm", "us-east-1")


def test_the_modules_import_and_read_requests_without_boto3() -> None:
    """boto3 is imported lazily: a missing SDK must not break the import."""
    code = (
        "import sys; sys.modules['boto3'] = None; sys.modules['botocore'] = None\n"
        "sys.path.insert(0, 'app')\n"
        "import handler\n"
        "print(handler.requested_workspace({'workspace': 'quillmoor'}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPOSITORY_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "quillmoor"


# --- Key budgets ---------------------------------------------------------


def budget_item(aws: dict[str, Any], key: str = KEYS["quillmoor"]) -> dict[str, Any]:
    """The table item of one key, found the way the handler finds it."""
    return aws["dynamodb"].items[handler.key_hash(key)]


def number(item: dict[str, Any], name: str) -> int | None:
    return handler.budget_number(item, name)


def test_a_key_is_hashed_and_the_key_itself_never_leaves_the_function(aws: dict[str, Any]) -> None:
    """The table may be read by anyone with the console; no key may fall out of it."""
    import hashlib

    assert call(event())[0] == 200
    (update,) = aws["dynamodb"].calls
    assert update["Key"] == {
        handler.BUDGET_KEY_ATTRIBUTE: {"S": hashlib.sha256(KEYS["quillmoor"].encode()).hexdigest()}
    }
    assert KEYS["quillmoor"] not in json.dumps(update)
    assert KEYS["quillmoor"] not in json.dumps(aws["dynamodb"].items, default=str)


def test_the_first_question_opens_the_window_and_counts_one(aws: dict[str, Any]) -> None:
    started = int(handler.wall_clock())
    assert call(event())[0] == 200

    item = budget_item(aws)
    assert number(item, "questions_used") == 1
    assert number(item, "question_limit") == int(ENVIRONMENT["QUESTION_LIMIT"])
    assert number(item, "first_used_at") == started
    assert number(item, "window_seconds") == 48 * SECONDS_PER_HOUR
    assert number(item, "expires_at") == started + 48 * SECONDS_PER_HOUR
    assert number(item, handler.BUDGET_TTL_ATTRIBUTE) == started + 365 * 24 * SECONDS_PER_HOUR
    assert item["workspace"] == {"S": "quillmoor"}


def test_the_window_does_not_move_with_later_questions(
    aws: dict[str, Any], budget_clock: "FakeWallClock"
) -> None:
    call(event())
    opened = number(budget_item(aws), "expires_at")
    budget_clock.advance(SECONDS_PER_HOUR)
    call(event())

    item = budget_item(aws)
    assert number(item, "expires_at") == opened
    assert number(item, "questions_used") == 2


def test_the_tenth_question_goes_through_and_the_eleventh_does_not(aws: dict[str, Any]) -> None:
    for asked in range(1, 11):
        status, _ = call(event())
        assert status == 200, f"question {asked}"

    status, body = call(event())
    assert status == 402
    assert body == {"error": QUESTIONS_SPENT_MESSAGE.format(limit=10)}
    assert number(budget_item(aws), "questions_used") == 10
    assert len(aws["bedrock-runtime"].calls) == 10


def test_the_window_closes_even_with_questions_left(
    aws: dict[str, Any], budget_clock: "FakeWallClock"
) -> None:
    assert call(event())[0] == 200
    budget_clock.advance(48 * SECONDS_PER_HOUR)

    status, body = call(event())
    assert status == 402
    assert body == {"error": WINDOW_CLOSED_MESSAGE}
    assert number(budget_item(aws), "questions_used") == 1


def test_the_last_second_of_the_window_still_answers(
    aws: dict[str, Any], budget_clock: "FakeWallClock"
) -> None:
    call(event())
    budget_clock.advance(48 * SECONDS_PER_HOUR - 1)
    assert call(event())[0] == 200


def test_a_budget_set_before_the_first_question_overrides_the_defaults(
    aws: dict[str, Any], budget_clock: "FakeWallClock"
) -> None:
    """Two questions and a six-hour window, written the way the CLI command writes them."""
    aws["dynamodb"].items[handler.key_hash(KEYS["quillmoor"])] = {
        handler.BUDGET_KEY_ATTRIBUTE: {"S": handler.key_hash(KEYS["quillmoor"])},
        "question_limit": {"N": "2"},
        "window_seconds": {"N": str(6 * SECONDS_PER_HOUR)},
        "keep_seconds": {"N": str(7 * 24 * SECONDS_PER_HOUR)},
    }
    started = int(budget_clock())

    assert call(event())[0] == 200
    item = budget_item(aws)
    assert number(item, "expires_at") == started + 6 * SECONDS_PER_HOUR
    assert number(item, handler.BUDGET_TTL_ATTRIBUTE) == started + 7 * 24 * SECONDS_PER_HOUR
    assert call(event())[0] == 200

    status, body = call(event())
    assert status == 402
    assert body == {"error": QUESTIONS_SPENT_MESSAGE.format(limit=2)}


def test_deleting_the_item_gives_the_key_a_fresh_budget(aws: dict[str, Any]) -> None:
    """That is the reset command: the budget is the item, and nothing else."""
    for _ in range(10):
        call(event())
    assert call(event())[0] == 402

    del aws["dynamodb"].items[handler.key_hash(KEYS["quillmoor"])]
    assert call(event())[0] == 200
    assert number(budget_item(aws), "questions_used") == 1


def test_a_question_that_is_refused_costs_nothing(aws: dict[str, Any]) -> None:
    """The budget is the sixth check, behind the question — a 400 spends nothing."""
    assert call(event({"question": "", "workspace": "quillmoor"}))[0] == 400
    assert (
        call(event({"question": "x" * (MAX_QUESTION_CHARACTERS + 1), "workspace": "quillmoor"}))[0]
        == 400
    )
    assert aws["dynamodb"].calls == []


@pytest.mark.parametrize(
    ("request_event", "status"),
    [
        (event(key="wrong-key"), 401),
        (event(key=None), 401),
        (event({"question": "How?", "workspace": "pellwick"}), 403),
    ],
    ids=["wrong-key", "no-key", "other-workspace"],
)
def test_a_key_that_does_not_open_the_workspace_never_reaches_the_budget(
    aws: dict[str, Any], request_event: dict[str, Any], status: int
) -> None:
    assert call(request_event)[0] == status
    assert aws["dynamodb"].calls == []


def test_a_table_that_cannot_be_reached_is_500_and_not_a_free_pass(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    aws["dynamodb"].error = client_error("ResourceNotFoundException", operation="UpdateItem")

    status, body = call(event())
    assert status == 500
    assert body == {"error": NOT_CONFIGURED_MESSAGE}
    assert aws["bedrock-agent-runtime"].calls == []
    assert aws["bedrock-runtime"].calls == []
    assert any(line["event"] == "budget-unreadable" for line in log_lines(caplog))


def test_a_throttled_table_is_500_too(aws: dict[str, Any]) -> None:
    aws["dynamodb"].error = client_error("ThrottlingException", operation="UpdateItem")
    assert call(event())[0] == 500
    assert aws["bedrock-runtime"].calls == []


def test_a_failed_condition_becomes_402_and_never_an_answer(aws: dict[str, Any]) -> None:
    """What a lost race looks like from inside the handler.

    Not a test of concurrency — nothing here runs at the same time. It pins
    the one thing the handler decides when DynamoDB says the condition was
    false: 402, no retrieval, no model call. With the item, the way DynamoDB
    sends it, the sentence names what ran out; without it, the plain one.
    """
    spent = {"questions_used": {"N": "10"}, "question_limit": {"N": "10"}}
    aws["dynamodb"].error = conditional_check_failed(spent)
    status, body = call(event())
    assert status == 402
    assert body == {"error": QUESTIONS_SPENT_MESSAGE.format(limit=10)}

    aws["dynamodb"].error = conditional_check_failed(None)
    status, body = call(event())
    assert status == 402
    assert body == {"error": BUDGET_SPENT_MESSAGE}

    assert aws["bedrock-agent-runtime"].calls == []
    assert aws["bedrock-runtime"].calls == []


def test_the_budget_is_never_read_before_it_is_written(aws: dict[str, Any]) -> None:
    """One conditional write per question, and no read: that is what makes the limit hard."""
    call(event())
    (update,) = aws["dynamodb"].calls
    assert set(update) == {
        "TableName",
        "Key",
        "UpdateExpression",
        "ConditionExpression",
        "ExpressionAttributeValues",
        "ReturnValues",
        "ReturnValuesOnConditionCheckFailure",
    }
    assert update["TableName"] == ENVIRONMENT["BUDGET_TABLE_NAME"]
    assert update["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"
    # What the write itself wrote, not a read of its own: `ReturnValues` is
    # answered from the update, costs no second call and no extra capacity.
    assert update["ReturnValues"] == "ALL_NEW"


def test_a_question_that_passed_the_budget_is_not_given_back(aws: dict[str, Any]) -> None:
    """Simple, hard, and easy to explain: what got through the budget stays counted."""
    aws["bedrock-runtime"].error = client_error("ThrottlingException")
    assert call(event())[0] == 502
    assert number(budget_item(aws), "questions_used") == 1

    aws["bedrock-runtime"].error = None
    aws["bedrock-agent-runtime"].error = client_error("AccessDeniedException", operation="Retrieve")
    assert call(event())[0] == 502
    assert number(budget_item(aws), "questions_used") == 2


def test_the_table_is_asked_again_for_every_question_although_the_keys_are_cached(
    aws: dict[str, Any],
) -> None:
    """The key cache keeps keys, never counts — a cached count would be no count."""
    for _ in range(3):
        call(event())
    assert len(aws["ssm"].calls) == 1
    assert len(aws["dynamodb"].calls) == 3


def test_two_keys_have_two_budgets(aws: dict[str, Any]) -> None:
    for _ in range(10):
        call(event())
    assert call(event())[0] == 402
    assert (
        call(event({"question": "How?", "workspace": "pellwick"}, key=KEYS["pellwick"]))[0] == 200
    )


def test_a_spent_key_is_logged_as_refused_with_the_reason_and_without_the_key(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    for _ in range(10):
        call(event())
    caplog.clear()
    caplog.set_level(logging.INFO, logger="andur")

    response = respond(event())
    assert response["statusCode"] == 402
    assert "WWW-Authenticate" not in response["headers"]
    (line,) = [line for line in log_lines(caplog) if line["event"] == "refused"]
    assert line["status"] == 402
    assert line["workspace"] == "quillmoor"
    assert line["error"] == QUESTIONS_SPENT_MESSAGE.format(limit=10)
    assert KEYS["quillmoor"] not in caplog.text
    assert handler.key_hash(KEYS["quillmoor"]) not in caplog.text


def test_which_half_ran_out_is_read_from_the_item_the_condition_refused() -> None:
    now = 1_790_000_000
    closed = {
        "expires_at": {"N": str(now)},
        "questions_used": {"N": "3"},
        "question_limit": {"N": "10"},
    }
    spent = {
        "expires_at": {"N": str(now + 1)},
        "questions_used": {"N": "10"},
        "question_limit": {"N": "10"},
    }
    assert handler.spent_message(closed, now) == WINDOW_CLOSED_MESSAGE
    assert handler.spent_message(spent, now) == QUESTIONS_SPENT_MESSAGE.format(limit=10)
    assert handler.spent_message({}, now) == BUDGET_SPENT_MESSAGE
    assert handler.spent_message({"questions_used": {"S": "many"}}, now) == BUDGET_SPENT_MESSAGE


def test_the_spent_status_is_none_of_the_three_it_could_be_mistaken_for() -> None:
    assert BudgetSpent.status == 402
    assert BudgetSpent.status not in {Unauthorized.status, Forbidden.status, 429}
    assert BudgetSpent.headers == {}


def test_the_update_writes_the_attributes_the_condition_and_the_table_watch() -> None:
    assert f"{handler.BUDGET_TTL_ATTRIBUTE} = " in handler.BUDGET_UPDATE_EXPRESSION
    assert "first_used_at = " in handler.BUDGET_UPDATE_EXPRESSION
    assert "first_used_at" in handler.BUDGET_CONDITION
    assert handler.BUDGET_UPDATE_EXPRESSION.startswith("SET ")


#: `handler.BUDGET_CONDITION` with one operator changed, the two changes that
#: would cost real money: a key that gets an eleventh question, and a window
#: that stays open one second past its end. Each is a single character.
ONE_TOO_MANY = handler.BUDGET_CONDITION.replace(
    "questions_used < question_limit", "questions_used <= question_limit"
)
ONE_SECOND_TOO_LONG = handler.BUDGET_CONDITION.replace("expires_at > :now", "expires_at >= :now")


def test_a_condition_that_allowed_one_question_too_many_would_show_here(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eleventh question is refused because of the string, not despite it.

    With the real condition the eleventh question is a 402
    (`test_the_tenth_question_goes_through_and_the_eleventh_does_not`). This
    changes ``<`` to ``<=`` and nothing else, and the eleventh goes through —
    which is what proves the tests are reading the condition the handler
    sends rather than a copy of its meaning.
    """
    monkeypatch.setattr(handler, "BUDGET_CONDITION", ONE_TOO_MANY)
    for asked in range(1, 11):
        assert call(event())[0] == 200, f"question {asked}"
    assert call(event())[0] == 200
    assert number(budget_item(aws), "questions_used") == 11


def test_a_condition_that_held_the_window_open_a_second_too_long_would_show_here(
    aws: dict[str, Any], budget_clock: "FakeWallClock", monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for the other half: ``>`` against ``>=`` at the exact second."""
    monkeypatch.setattr(handler, "BUDGET_CONDITION", ONE_SECOND_TOO_LONG)
    assert call(event())[0] == 200
    budget_clock.advance(48 * SECONDS_PER_HOUR)
    assert call(event())[0] == 200


def test_an_item_that_lost_an_attribute_does_not_lock_its_key_out(aws: dict[str, Any]) -> None:
    """A half-written item costs one question and is repaired, not a key for a year.

    ``question_limit`` gone, ``first_used_at`` there: in DynamoDB both sides
    of the comparison are false, so before this the condition refused every
    question this key would ever ask, until the TTL removed the item — 365
    days by default. The message said nothing either, because there was no
    limit in the item to name.
    """
    started = int(handler.wall_clock())
    aws["dynamodb"].items[handler.key_hash(KEYS["quillmoor"])] = {
        handler.BUDGET_KEY_ATTRIBUTE: {"S": handler.key_hash(KEYS["quillmoor"])},
        "first_used_at": {"N": str(started)},
        "questions_used": {"N": "3"},
        "expires_at": {"N": str(started + 48 * SECONDS_PER_HOUR)},
    }

    assert call(event())[0] == 200
    item = budget_item(aws)
    assert number(item, "question_limit") == int(ENVIRONMENT["QUESTION_LIMIT"])
    assert number(item, "questions_used") == 4


def test_an_item_whose_own_window_outlasts_it_has_its_end_pushed_out(
    aws: dict[str, Any], budget_clock: "FakeWallClock"
) -> None:
    """A seven-day window kept for one day would hand the key a fresh budget.

    The item is what makes a spent key stay spent. Written by hand this way
    (app/README.md warns about it, and nothing in DynamoDB checks it), the
    item would disappear on day two with six days of its window left, and the
    key would start over with ten questions. The counting write hands back
    what it wrote, and the second write puts the end past the window.
    """
    aws["dynamodb"].items[handler.key_hash(KEYS["quillmoor"])] = {
        handler.BUDGET_KEY_ATTRIBUTE: {"S": handler.key_hash(KEYS["quillmoor"])},
        "window_seconds": {"N": str(7 * SECONDS_PER_DAY)},
        "keep_seconds": {"N": str(SECONDS_PER_DAY)},
    }
    started = int(budget_clock())

    assert call(event())[0] == 200
    item = budget_item(aws)
    end_of_the_item = (
        started + 7 * SECONDS_PER_DAY + int(ENVIRONMENT["KEEP_DAYS"]) * SECONDS_PER_DAY
    )
    assert number(item, "expires_at") == started + 7 * SECONDS_PER_DAY
    assert number(item, handler.BUDGET_TTL_ATTRIBUTE) == end_of_the_item
    counted, repaired = aws["dynamodb"].calls
    assert "ConditionExpression" in counted and "ConditionExpression" not in repaired


def test_the_repair_holds_and_is_not_written_a_second_time(
    aws: dict[str, Any], budget_clock: "FakeWallClock"
) -> None:
    """The counting update rewrites `delete_after` on every question.

    So a repair that only raised `delete_after` would be undone by the next
    question and written again by the next repair: two writes per question
    for as long as the key lives, and an item left short whenever one of
    those repairs is throttled. Raising ``keep_seconds`` with it is what
    makes the counting update arrive at the same number by itself.
    """
    aws["dynamodb"].items[handler.key_hash(KEYS["quillmoor"])] = {
        handler.BUDGET_KEY_ATTRIBUTE: {"S": handler.key_hash(KEYS["quillmoor"])},
        "window_seconds": {"N": str(7 * SECONDS_PER_DAY)},
        "keep_seconds": {"N": str(SECONDS_PER_DAY)},
    }
    started = int(budget_clock())
    end_of_the_item = (
        started + 7 * SECONDS_PER_DAY + int(ENVIRONMENT["KEEP_DAYS"]) * SECONDS_PER_DAY
    )

    assert call(event())[0] == 200
    budget_clock.advance(SECONDS_PER_HOUR)
    assert call(event())[0] == 200

    item = budget_item(aws)
    assert number(item, "expires_at") == started + 7 * SECONDS_PER_DAY
    assert number(item, handler.BUDGET_TTL_ATTRIBUTE) == end_of_the_item
    # Count, repair, count — and no repair behind the second question.
    assert len(aws["dynamodb"].calls) == 3


def test_a_repair_that_does_not_get_through_does_not_cost_the_answer(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The question is paid for by then, so a second write that fails goes to the log."""
    caplog.set_level(logging.INFO, logger="andur")
    aws["dynamodb"].items[handler.key_hash(KEYS["quillmoor"])] = {
        handler.BUDGET_KEY_ATTRIBUTE: {"S": handler.key_hash(KEYS["quillmoor"])},
        "window_seconds": {"N": str(7 * SECONDS_PER_DAY)},
        "keep_seconds": {"N": str(SECONDS_PER_DAY)},
    }
    aws["dynamodb"].error = client_error("ThrottlingException", operation="UpdateItem")
    aws["dynamodb"].error_from_call = 2

    assert call(event())[0] == 200
    (line,) = [line for line in log_lines(caplog) if line["event"] == "budget-ttl-unrepaired"]
    assert line["detail"] == "ClientError: ThrottlingException"


# --- Retrieval and prompt ------------------------------------------------


def test_retrieval_is_filtered_to_the_workspace() -> None:
    runtime = FakeAgentRuntime([hit()])
    assert handler.retrieve_passages(runtime, "KB1", "How?", "pellwick", 6) == [hit()]
    (call_arguments,) = runtime.calls
    assert call_arguments["knowledgeBaseId"] == "KB1"
    assert call_arguments["retrievalQuery"] == {"text": "How?"}
    assert call_arguments["retrievalConfiguration"]["vectorSearchConfiguration"] == {
        "numberOfResults": 6,
        "filter": {"equals": {"key": "workspace", "value": "pellwick"}},
    }


def test_a_failed_retrieval_is_a_retrieval_error() -> None:
    runtime = FakeAgentRuntime(error=client_error("ThrottlingException", operation="Retrieve"))
    with pytest.raises(RetrievalError, match="throttled") as error:
        handler.retrieve_passages(runtime, "KB1", "How?", "quillmoor", 6)
    assert error.value.status == 502


def test_passages_of_another_workspace_are_dropped() -> None:
    results = [hit(), hit(workspace="pellwick"), {"content": {"text": "no metadata"}}]
    kept, dropped = handler.keep_workspace(results, "quillmoor")
    assert kept == [hit()]
    assert dropped == 2


def test_source_carries_file_layer_workspace_score_words_and_excerpt() -> None:
    long_text = "# Title\n\n" + "word " * 200
    source = handler.to_source(3, hit(layer="agent", name="summary--02-x.md", text=long_text))
    assert source["number"] == 3
    assert source["file"] == "summary--02-x.md"
    assert source["layer"] == "agent"
    assert source["workspace"] == "quillmoor"
    assert source["score"] == 0.8123
    assert source["words"] == 202
    assert source["cited"] is False
    assert source["excerpt"].endswith(" ...")
    assert len(source["excerpt"]) <= handler.EXCERPT_CHARACTERS + 4
    assert "\n" not in source["excerpt"]


def test_source_file_name_leaves_out_the_bucket_and_its_account_id() -> None:
    assert "000000000000" not in json.dumps(handler.to_source(1, hit()))


def test_source_of_an_incomplete_hit_does_not_fail() -> None:
    assert handler.to_source(1, {}) == {
        "number": 1,
        "file": "unknown",
        "layer": "unknown",
        "workspace": "unknown",
        "score": None,
        "words": 0,
        "cited": False,
        "excerpt": "",
    }


def test_sources_are_numbered_with_layer_and_file() -> None:
    table = "| Plan | Price |\n|---|---|\n| Team | €19 |"
    sources = handler.build_sources(
        [hit(text="First passage."), hit(layer="inbox", name="draft--01.md", text=table)]
    )
    assert sources.startswith(
        "Sources:\n\n[1] layer: human | file: faq--03-projects-and-exports.md\nFirst passage."
    )
    assert sources.endswith("[2] layer: inbox | file: draft--01.md\n" + table)


def test_the_message_marks_sources_and_question_for_the_guardrail() -> None:
    """Sources are only the grounding reference; the question is the query and is guarded.

    The qualifiers decide what each policy reads. Were the sources guarded,
    a phone number in a document would be masked before the model saw it,
    and a document about taxes would be refused as a tax question.
    """
    content = handler.build_content("What does Team cost?", [hit(text="First passage.")])
    sources, label, question = content
    assert sources["guardContent"]["text"]["qualifiers"] == ["grounding_source"]
    assert sources["guardContent"]["text"]["text"] == handler.build_sources(
        [hit(text="First passage.")]
    )
    assert label == {"text": "\n\nQuestion:"}
    assert question == {
        "guardContent": {
            "text": {"text": "What does Team cost?", "qualifiers": ["query", "guard_content"]}
        }
    }


def test_the_longest_question_fits_the_guardrail_query() -> None:
    """A guardrail query may be 1,000 characters; the question block holds the question alone."""
    question = "x" * MAX_QUESTION_CHARACTERS
    (_, _, block) = handler.build_content(question, [hit()])
    assert len(block["guardContent"]["text"]["text"]) <= 1000


def test_an_outlier_passage_is_cut_in_the_prompt_with_a_note() -> None:
    outlier = "word " * 2000
    prompt = handler.build_sources([hit(text=outlier), hit(text="Short.")])
    first_block = prompt.split("\n\n[2]")[0]
    assert len(first_block) < MAX_SOURCE_CHARACTERS + 200
    assert (
        f"[... cut: the passage is longer than {MAX_SOURCE_CHARACTERS} characters]" in first_block
    )
    assert "[2] layer: human | file: faq--03-projects-and-exports.md\nShort." in prompt


def test_a_passage_at_the_limit_is_not_cut() -> None:
    text = "x" * MAX_SOURCE_CHARACTERS
    assert handler.capped(text) == text


def test_system_prompt_holds_the_rules_the_response_relies_on() -> None:
    assert NOT_IN_SOURCES_ANSWER in handler.SYSTEM_PROMPT
    assert "[1]" in handler.SYSTEM_PROMPT
    assert "only from the numbered sources" in handler.SYSTEM_PROMPT


# --- Model ---------------------------------------------------------------


def test_model_gets_system_rules_message_token_limit_and_guardrail() -> None:
    bedrock = FakeBedrockRuntime()
    content = [{"text": "Sources: ..."}]
    handler.ask_model(bedrock, SETTINGS, content)
    (arguments,) = bedrock.calls
    assert arguments["modelId"] == ENVIRONMENT["MODEL_ID"]
    assert arguments["system"] == [{"text": handler.SYSTEM_PROMPT}]
    assert arguments["messages"] == [{"role": "user", "content": content}]
    assert arguments["inferenceConfig"] == {"maxTokens": MAX_ANSWER_TOKENS}
    assert arguments["guardrailConfig"] == {
        "guardrailIdentifier": "gr-test",
        "guardrailVersion": "1",
        "trace": "enabled",
    }


def test_a_blocked_model_is_an_llm_error_with_a_hint() -> None:
    bedrock = FakeBedrockRuntime(error=client_error("AccessDeniedException"))
    with pytest.raises(LLMError, match="may not be open to this function") as error:
        handler.ask_model(bedrock, SETTINGS, [{"text": "p"}])
    assert error.value.status == 502


def test_a_model_timeout_is_an_llm_error() -> None:
    exceptions = pytest.importorskip("botocore.exceptions")
    bedrock = FakeBedrockRuntime(
        error=exceptions.ReadTimeoutError(endpoint_url="https://bedrock-runtime")
    )
    with pytest.raises(LLMError, match="timeout"):
        handler.ask_model(bedrock, SETTINGS, [{"text": "p"}])


class SequencedBedrockRuntime(FakeBedrockRuntime):
    """A model that fails with each of ``errors`` in turn, then answers."""

    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = errors

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return self.response


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The pauses the handler takes, instead of taking them."""
    taken: list[float] = []
    monkeypatch.setattr(handler, "pause", taken.append)
    return taken


@pytest.mark.parametrize(
    "code", ["ServiceUnavailableException", "ThrottlingException", "ModelNotReadyException"]
)
def test_a_rejected_model_call_is_made_once_more_and_logged(
    code: str, pauses: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    bedrock = SequencedBedrockRuntime([client_error(code)])
    record = handler.RequestRecord()
    response = handler.ask_model(bedrock, SETTINGS, [{"text": "p"}], record)
    assert response == bedrock.response
    assert len(bedrock.calls) == 2 and bedrock.calls[0] == bedrock.calls[1]
    assert pauses == [2]
    assert record.model_rejections == 1
    (line,) = [line for line in log_lines(caplog) if line["event"] == "converse-rejected"]
    assert line["retry"] is True and line["detail"] == f"ClientError: {code}"
    assert not [line for line in log_lines(caplog) if line["event"] == "converse-failed"]


def test_a_second_rejection_is_an_llm_error_and_there_is_no_third_call(
    pauses: list[float],
) -> None:
    errors = [client_error("ServiceUnavailableException") for _ in range(3)]
    bedrock = SequencedBedrockRuntime(errors)
    record = handler.RequestRecord()
    with pytest.raises(LLMError, match="the service is unavailable") as error:
        handler.ask_model(bedrock, SETTINGS, [{"text": "p"}], record)
    assert error.value.status == 502
    assert len(bedrock.calls) == 2 and pauses == [2]
    assert record.model_rejections == 2


def test_a_timed_out_model_call_is_never_made_again(pauses: list[float]) -> None:
    """A converse that timed out may already have been answered and paid for."""
    exceptions = pytest.importorskip("botocore.exceptions")
    bedrock = SequencedBedrockRuntime(
        [exceptions.ReadTimeoutError(endpoint_url="https://bedrock-runtime")]
    )
    with pytest.raises(LLMError, match="timeout"):
        handler.ask_model(bedrock, SETTINGS, [{"text": "p"}])
    assert len(bedrock.calls) == 1 and pauses == []


@pytest.mark.parametrize(
    "code", ["InternalServerException", "Throttling", "ServiceQuotaExceededException"]
)
def test_a_call_that_may_have_run_or_is_no_documented_rejection_is_not_made_again(
    code: str, pauses: list[float]
) -> None:
    """An internal error may come after the model has begun; the others are no Converse
    errors, so nothing documents that the call did not run."""
    bedrock = SequencedBedrockRuntime([client_error(code)])
    record = handler.RequestRecord()
    with pytest.raises(LLMError):
        handler.ask_model(bedrock, SETTINGS, [{"text": "p"}], record)
    assert len(bedrock.calls) == 1 and pauses == [] and record.model_rejections == 0


def test_an_access_error_is_not_made_again(pauses: list[float]) -> None:
    bedrock = SequencedBedrockRuntime([client_error("AccessDeniedException")])
    with pytest.raises(LLMError, match="may not be open"):
        handler.ask_model(bedrock, SETTINGS, [{"text": "p"}])
    assert len(bedrock.calls) == 1 and pauses == []


@pytest.mark.parametrize(("elapsed", "calls"), [(50, 2), (50.001, 1)])
def test_the_second_model_call_starts_only_while_cloudfront_still_waits(
    elapsed: float,
    calls: int,
    cold_start: FakeClock,
    pauses: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At second 50 there is still time for the pause, the call and the excerpts."""
    caplog.set_level(logging.INFO, logger="andur")
    record = handler.RequestRecord()
    cold_start.advance(elapsed)
    bedrock = SequencedBedrockRuntime([client_error("ServiceUnavailableException")])
    if calls == 2:
        handler.ask_model(bedrock, SETTINGS, [{"text": "p"}], record)
    else:
        with pytest.raises(LLMError, match="unavailable"):
            handler.ask_model(bedrock, SETTINGS, [{"text": "p"}], record)
    assert len(bedrock.calls) == calls
    (line,) = [line for line in log_lines(caplog) if line["event"] == "converse-rejected"]
    assert line["retry"] is (calls == 2)
    assert line["elapsed_ms"] == round(elapsed * 1000)
    assert record.model_rejections == 1


def test_extract_answer_returns_the_text() -> None:
    assert handler.extract_answer(bedrock_response([{"text": "Hello."}])) == "Hello."


def test_extract_answer_skips_a_block_without_text() -> None:
    response = bedrock_response([{"reasoningContent": {}}, {"text": "Second block."}])
    assert handler.extract_answer(response) == "Second block."


@pytest.mark.parametrize(
    ("stop_reason", "note"),
    [
        ("max_tokens", f"cut off: the limit of {MAX_ANSWER_TOKENS} tokens"),
        ("content_filtered", "content filter"),
        ("guardrail_intervened", "guardrail"),
    ],
)
def test_extract_answer_marks_an_answer_the_model_did_not_finish(
    stop_reason: str, note: str
) -> None:
    result = handler.extract_answer(bedrock_response([{"text": "Start"}], stop_reason=stop_reason))
    assert result.startswith("Start\n\n[")
    assert note in result


def test_extract_answer_raises_without_any_text_block() -> None:
    with pytest.raises(LLMError, match="tool_use"):
        handler.extract_answer(bedrock_response([{"toolUse": {}}], stop_reason="tool_use"))


@pytest.mark.parametrize("text", ["", "   ", "\n\t\n"])
def test_an_empty_answer_is_an_llm_error(text: str) -> None:
    with pytest.raises(LLMError, match="no text") as error:
        handler.extract_answer(bedrock_response([{"text": text}]))
    assert error.value.status == 502


def test_extract_answer_raises_on_an_empty_response() -> None:
    with pytest.raises(LLMError, match="unknown"):
        handler.extract_answer({})


# --- Guardrail and cost --------------------------------------------------

#: The input assessment of a tax question the topic filter refused.
TAX_QUESTION_BLOCKED = {
    "topicPolicy": {
        "topics": [{"name": "Tax advice", "type": "DENY", "action": "BLOCKED", "detected": True}]
    },
    "invocationMetrics": {"guardrailProcessingLatency": 90, "usage": guardrail_usage(1, 0, 0)},
}

#: The output assessment of an answer with a phone number and a name in it.
PHONE_MASKED = {
    "sensitiveInformationPolicy": {
        "piiEntities": [
            {"match": "Ada Quill", "type": "NAME", "action": "ANONYMIZED", "detected": True},
            {
                "match": "+44 20 7946 0958",
                "type": "PHONE",
                "action": "ANONYMIZED",
                "detected": True,
            },
        ]
    },
    "contextualGroundingPolicy": {
        "filters": [
            {
                "type": "GROUNDING",
                "threshold": 0.7,
                "score": 0.97,
                "action": "NONE",
                "detected": False,
            },
            {
                "type": "RELEVANCE",
                "threshold": 0.7,
                "score": 0.91,
                "action": "NONE",
                "detected": False,
            },
        ]
    },
    "invocationMetrics": {"guardrailProcessingLatency": 250, "usage": guardrail_usage(1, 1, 3)},
}

#: The output assessment of an answer the passages do not carry.
UNGROUNDED = {
    "contextualGroundingPolicy": {
        "filters": [
            {
                "type": "GROUNDING",
                "threshold": 0.7,
                "score": 0.12,
                "action": "BLOCKED",
                "detected": True,
            },
            {
                "type": "RELEVANCE",
                "threshold": 0.7,
                "score": 0.9,
                "action": "NONE",
                "detected": False,
            },
        ]
    },
    "invocationMetrics": {"guardrailProcessingLatency": 260, "usage": guardrail_usage(1, 1, 3)},
}


def test_a_guardrail_that_did_nothing_reports_its_scores_and_units() -> None:
    assert handler.read_guardrail(bedrock_response([{"text": "A [1]."}])) == {
        "action": "none",
        "policies": [],
        "grounding_score": 0.93,
        "relevance_score": 0.88,
        "units": {
            "topicPolicyUnits": 2,
            "sensitiveInformationPolicyUnits": 1,
            "contextualGroundingPolicyUnits": 3,
        },
        "question_refused": False,
    }


def test_a_refused_topic_is_blocked_and_named() -> None:
    response = bedrock_response(
        [{"text": "Refused."}],
        "guardrail_intervened",
        guardrail_trace(input_assessment=TAX_QUESTION_BLOCKED),
    )
    guardrail = handler.read_guardrail(response)
    assert (guardrail["action"], guardrail["policies"]) == ("blocked", ["topic:Tax advice"])
    assert guardrail["question_refused"] is True


def test_masked_personal_details_are_named_by_type_and_never_by_what_matched() -> None:
    response = bedrock_response(
        [{"text": "Call {NAME} on {PHONE} [1]."}],
        "guardrail_intervened",
        guardrail_trace(output_assessment=PHONE_MASKED),
    )
    guardrail = handler.read_guardrail(response)
    assert (guardrail["action"], guardrail["policies"]) == ("masked", ["pii:NAME", "pii:PHONE"])
    assert "Ada Quill" not in json.dumps(guardrail) and "7946" not in json.dumps(guardrail)


def test_an_ungrounded_answer_is_blocked_with_its_score() -> None:
    response = bedrock_response(
        [{"text": "Withheld."}],
        "guardrail_intervened",
        guardrail_trace(output_assessment=UNGROUNDED),
    )
    guardrail = handler.read_guardrail(response)
    assert (guardrail["action"], guardrail["policies"]) == ("blocked", ["grounding"])
    assert guardrail["question_refused"] is False
    assert (guardrail["grounding_score"], guardrail["relevance_score"]) == (0.12, 0.9)


def test_blocking_outranks_masking() -> None:
    both = {**PHONE_MASKED, "contextualGroundingPolicy": UNGROUNDED["contextualGroundingPolicy"]}
    response = bedrock_response(
        [{"text": "Withheld."}], "guardrail_intervened", guardrail_trace(output_assessment=both)
    )
    guardrail = handler.read_guardrail(response)
    assert guardrail["action"] == "blocked"
    assert guardrail["policies"] == ["grounding", "pii:NAME", "pii:PHONE"]


def test_an_intervention_without_a_trace_is_named_as_such_and_its_units_are_unknown() -> None:
    response = bedrock_response([{"text": "Stopped."}], "guardrail_intervened", trace={})
    guardrail = handler.read_guardrail(response)
    assert guardrail["action"] == "intervened"
    assert guardrail["units"] is None
    assert (guardrail["grounding_score"], guardrail["relevance_score"]) == (None, None)


def test_a_masked_answer_says_so_and_a_blocked_one_is_the_guardrail_message_alone() -> None:
    masked = bedrock_response([{"text": "Call {PHONE} [1]."}], "guardrail_intervened")
    assert (
        handler.extract_answer(masked, "masked")
        == "Call {PHONE} [1].\n\n" + handler.GUARDRAIL_NOTES["masked"]
    )
    blocked = bedrock_response([{"text": "Andur does not advise on this."}], "guardrail_intervened")
    assert handler.extract_answer(blocked, "blocked") == "Andur does not advise on this."
    assert handler.extract_answer(blocked, "intervened").endswith(
        handler.STOP_NOTES["guardrail_intervened"]
    )


def test_the_cost_is_tokens_and_units_times_their_prices_rounded_to_six_places() -> None:
    usage = {"input_tokens": 1624, "output_tokens": 50}
    units = {
        "topicPolicyUnits": 2,
        "sensitiveInformationPolicyUnits": 1,
        "contextualGroundingPolicyUnits": 3,
    }
    # 1624 x 0.00015 + 50 x 0.00065 + 2 x 0.15 + 1 x 0.10 + 3 x 0.10, per thousand
    # = (0.2436 + 0.0325 + 0.3 + 0.1 + 0.3) / 1000 = 0.0009761
    assert handler.answer_cost(SETTINGS.prices, usage, units) == 0.000976
    assert handler.answer_cost(SETTINGS.prices, usage, None) is None
    zero = {name: 0 for name in handler.PRICED_UNITS}
    assert (
        handler.answer_cost(SETTINGS.prices, {"input_tokens": 0, "output_tokens": 0}, zero) == 0.0
    )


def test_the_cost_is_rounded_half_even_at_the_sixth_place() -> None:
    """Decimal arithmetic, so 0.1 + 0.2 is 0.3 here and the rounding is exact."""
    prices = handler.Prices(Decimal("0.0000025"), Decimal(0), Decimal(0), Decimal(0), Decimal(0))
    none = {name: 0 for name in handler.PRICED_UNITS}
    assert handler.answer_cost(prices, {"input_tokens": 1000, "output_tokens": 0}, none) == 0.000002
    assert handler.answer_cost(prices, {"input_tokens": 3000, "output_tokens": 0}, none) == 0.000008


def test_usage_is_read_from_the_response() -> None:
    assert handler.read_usage(bedrock_response([])) == {"input_tokens": 1624, "output_tokens": 50}
    assert handler.read_usage({}) == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Export it [1].", {1}),
        ("A [2][3]. B [4, 6]. C [ 5 ].", {2, 3, 4, 5, 6}),
        ("A range [1-3].", {1, 2, 3}),
        ("An en dash [2–4].", {2, 3, 4}),
        ("An em dash [4—6].", {4, 5, 6}),
        ("Semicolons [1; 3].", {1, 3}),
        ("Mixed [1-2; 5, 6].", {1, 2, 5, 6}),
        ("Clipped [5-99999999].", {5, 6}),
        ("Backwards [3-1].", set()),
        ("Invented [9] and [0].", set()),
        ("A date [2026-09-17] is no citation.", set()),
        ("Years [2020-2025] are no citation.", set()),
        (NOT_IN_SOURCES_ANSWER, set()),
    ],
)
def test_cited_numbers(answer: str, expected: set[int]) -> None:
    assert handler.cited_numbers(answer, 6) == expected


def test_cited_layers_come_from_cited_sources_only_most_trusted_first() -> None:
    sources = [
        {"layer": "inbox", "cited": True},
        {"layer": "human", "cited": True},
        {"layer": "agent", "cited": False},
        {"layer": "inbox", "cited": True},
    ]
    assert handler.cited_layers(sources) == ["human", "inbox"]
    assert handler.cited_layers([]) == []


# --- The whole request ---------------------------------------------------


def test_an_answer_carries_sources_layers_model_usage_and_latency(aws: dict[str, Any]) -> None:
    aws["bedrock-runtime"].response = bedrock_response(
        [{"text": "Settings > Export [1]. A draft adds more [2]."}]
    )
    status, body = call(event())

    assert status == 200
    assert body["answer"] == "Settings > Export [1]. A draft adds more [2]."
    assert [(s["number"], s["layer"], s["cited"]) for s in body["sources"]] == [
        (1, "human", True),
        (2, "inbox", True),
    ]
    assert {s["workspace"] for s in body["sources"]} == {"quillmoor"}
    assert body["layers"] == ["human", "inbox"]
    assert body["model"] == ENVIRONMENT["MODEL_ID"]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 1624, "output_tokens": 50}
    assert body["guardrail"] == {"action": "none", "policies": []}
    # 0.000976 for the answer, plus one PII unit at 0.10 per thousand for the excerpts.
    assert body["cost_usd"] == 0.001076
    assert body["price_date"] == "2026-09-23"
    assert isinstance(body["latency_ms"], int)
    assert set(body) == {
        "answer",
        "sources",
        "layers",
        "model",
        "stop_reason",
        "usage",
        "guardrail",
        "cost_usd",
        "price_date",
        "latency_ms",
    }


def test_a_tax_question_comes_back_as_the_guardrail_message_and_is_still_a_200(
    aws: dict[str, Any],
) -> None:
    """A refusal by the guardrail is an answer, not an error: the key's question is spent
    either way."""
    aws["bedrock-runtime"].response = {
        "output": {"message": {"content": [{"text": "Andur does not advise on this."}]}},
        "stopReason": "guardrail_intervened",
        "usage": {"inputTokens": 0, "outputTokens": 0},
        "trace": guardrail_trace(input_assessment=TAX_QUESTION_BLOCKED, output_assessment={}),
    }
    status, body = call(event())
    assert status == 200
    assert body["answer"] == "Andur does not advise on this."
    assert body["guardrail"] == {"action": "blocked", "policies": ["topic:Tax advice"]}
    # One topic unit at 0.15 per thousand, nothing else: no excerpt was masked.
    assert body["cost_usd"] == 0.00015


def test_a_refused_question_gets_no_sources_and_its_excerpts_are_never_guarded(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing was answered from the passages; their text next to a refusal is only a leak."""
    caplog.set_level(logging.INFO, logger="andur")
    aws["bedrock-runtime"].response = {
        "output": {"message": {"content": [{"text": "Andur does not advise on this."}]}},
        "stopReason": "guardrail_intervened",
        "usage": {"inputTokens": 0, "outputTokens": 0},
        "trace": guardrail_trace(input_assessment=TAX_QUESTION_BLOCKED, output_assessment={}),
    }
    _, body = call(event())
    assert body["sources"] == [] and body["layers"] == []
    assert aws["guardrail"].calls == []
    (line,) = [line for line in log_lines(caplog) if line["event"] == "answered"]
    # The search still ran: the log says how many passages it found.
    assert (line["hits"], line["cited"]) == (2, 0)


def test_an_answer_withheld_by_the_grounding_check_keeps_its_sources(aws: dict[str, Any]) -> None:
    """Only a refused question loses them; a withheld answer still shows what was searched."""
    aws["bedrock-runtime"].response = bedrock_response(
        [{"text": "Withheld."}],
        "guardrail_intervened",
        guardrail_trace(output_assessment=UNGROUNDED),
    )
    _, body = call(event())
    assert len(body["sources"]) == 2
    assert len(aws["guardrail"].calls) == 1


def test_excerpts_go_through_the_excerpt_guardrail_in_one_block(aws: dict[str, Any]) -> None:
    aws["bedrock-agent-runtime"].results = [
        hit(text="Anja answers the phone from 08:00."),
        hit(layer="inbox", name="draft--01.md", text="Write to workshop@pellwick.example."),
    ]
    aws["guardrail"].response = excerpt_guard_response(
        ["{NAME} answers the phone from 08:00.\nWrite to {EMAIL}."],
        pii_units=1,
        masked=("NAME", "EMAIL"),
    )
    _, body = call(event())
    (request,) = aws["guardrail"].calls
    assert request["guardrailIdentifier"] == "gr-excerpts"
    assert request["guardrailVersion"] == "2"
    assert request["source"] == "OUTPUT"
    assert request["content"] == [
        {
            "text": {
                "text": "Anja answers the phone from 08:00.\nWrite to workshop@pellwick.example."
            }
        }
    ]
    assert [source["excerpt"] for source in body["sources"]] == [
        "{NAME} answers the phone from 08:00.",
        "Write to {EMAIL}.",
    ]
    assert "Anja" not in json.dumps(body) and "workshop@" not in json.dumps(body)


def test_excerpts_the_guardrail_leaves_alone_come_back_unchanged(aws: dict[str, Any]) -> None:
    _, body = call(event())
    assert [source["excerpt"] for source in body["sources"]] == [
        "Open the project and go to Settings > Export.",
        "Draft.",
    ]


def test_the_masked_types_are_logged_and_never_what_they_matched(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    aws["guardrail"].response = excerpt_guard_response(["{NAME} opens.\nDraft."], masked=("NAME",))
    call(event())
    (line,) = [line for line in log_lines(caplog) if line["event"] == "answered"]
    assert line["excerpt_policies"] == ["pii:NAME"]
    assert "Anja Pell" not in caplog.text


@pytest.mark.parametrize(
    "outputs",
    [["only one line"], ["a", "b"], ["too\nmany\nlines"]],
    ids=["too-few-lines", "two-outputs", "too-many-lines"],
)
def test_a_masked_text_that_does_not_split_back_drops_every_excerpt(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture, outputs: list[str]
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    aws["guardrail"].response = excerpt_guard_response(outputs, masked=("NAME",))
    status, body = call(event())
    assert status == 200
    assert [source["excerpt"] for source in body["sources"]] == [None, None]
    assert [line for line in log_lines(caplog) if line["event"] == "excerpt-guard-unusable"]


@pytest.mark.parametrize(
    "action", ["BLOCKED", "", None, "SOMETHING_NEW"], ids=["blocked", "empty", "missing", "unknown"]
)
def test_an_action_other_than_none_or_intervened_drops_every_excerpt(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture, action: str | None
) -> None:
    """Only the two documented actions keep the excerpts; anything else is no masking to trust.

    The call went through and reported its units, so the cost stays an amount.
    """
    caplog.set_level(logging.INFO, logger="andur")
    # Outputs that would split into two lines, so the action alone decides.
    response = excerpt_guard_response(["{NAME} opens.\nDraft."], masked=("NAME",))
    if action is None:
        del response["action"]
    else:
        response["action"] = action
    aws["guardrail"].response = response
    status, body = call(event())
    assert status == 200
    assert [source["excerpt"] for source in body["sources"]] == [None, None]
    assert body["cost_usd"] == 0.001076
    (line,) = [line for line in log_lines(caplog) if line["event"] == "excerpt-guard-unusable"]
    assert line["action"] == action


def test_a_split_that_does_not_fit_keeps_the_units_counted(aws: dict[str, Any]) -> None:
    """The call was billed although its text was of no use: the cost is an amount, not null."""
    aws["guardrail"].response = excerpt_guard_response(
        ["only one line"], pii_units=2, masked=("NAME",)
    )
    _, body = call(event())
    assert [source["excerpt"] for source in body["sources"]] == [None, None]
    # 0.000976 for the answer, plus two PII units at 0.10 per thousand.
    assert body["cost_usd"] == 0.001176


def test_a_failed_excerpt_guardrail_drops_every_excerpt_and_the_cost_is_unknown(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: an answer without its passages' openings, never with them unmasked."""
    caplog.set_level(logging.INFO, logger="andur")
    aws["guardrail"].error = client_error(
        "ThrottlingException", "near 'Anja Pell'", operation="ApplyGuardrail"
    )
    status, body = call(event())
    assert status == 200
    assert body["answer"] == "Use Settings > Export [1]."
    assert [source["excerpt"] for source in body["sources"]] == [None, None]
    assert body["cost_usd"] is None
    (line,) = [line for line in log_lines(caplog) if line["event"] == "excerpt-guard-failed"]
    assert line["detail"] == "ClientError: ThrottlingException"
    assert "Anja Pell" not in caplog.text


def test_excerpt_units_count_as_pii_units() -> None:
    units = {
        "topicPolicyUnits": 2,
        "sensitiveInformationPolicyUnits": 1,
        "contextualGroundingPolicyUnits": 3,
    }
    assert handler.add_units(units, 2)["sensitiveInformationPolicyUnits"] == 3
    assert units["sensitiveInformationPolicyUnits"] == 1
    assert handler.add_units(units, None) is None
    assert handler.add_units(None, 2) is None


def test_the_unmasked_answer_in_the_trace_reaches_neither_body_nor_log(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    aws["bedrock-runtime"].response = bedrock_response(
        [{"text": "Call {NAME} on {PHONE} [1]."}],
        "guardrail_intervened",
        guardrail_trace(output_assessment=PHONE_MASKED),
    )
    status, body = call(event())
    assert status == 200
    assert body["answer"] == "Call {NAME} on {PHONE} [1].\n\n" + handler.GUARDRAIL_NOTES["masked"]
    for text in (json.dumps(body), caplog.text):
        assert "Ada Quill" not in text and "7946" not in text


def test_the_request_goes_to_the_workspace_and_model_from_the_environment(
    aws: dict[str, Any],
) -> None:
    call(event())
    (retrieve,) = aws["bedrock-agent-runtime"].calls
    assert retrieve["knowledgeBaseId"] == "KB123"
    assert (
        retrieve["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]["equals"]["value"]
        == "quillmoor"
    )
    assert retrieve["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 6
    (converse,) = aws["bedrock-runtime"].calls
    assert converse["modelId"] == ENVIRONMENT["MODEL_ID"]


def test_a_passage_of_another_workspace_never_reaches_the_model(aws: dict[str, Any]) -> None:
    aws["bedrock-agent-runtime"].results = [
        hit(),
        hit(workspace="pellwick", text="Pellwick secret."),
    ]
    status, body = call(event())
    assert status == 200
    assert [source["workspace"] for source in body["sources"]] == ["quillmoor"]
    (converse,) = aws["bedrock-runtime"].calls
    assert "Pellwick secret." not in json.dumps(converse)


def test_no_hits_means_no_model_call(aws: dict[str, Any]) -> None:
    aws["bedrock-agent-runtime"].results = []
    status, body = call(event())

    assert status == 200
    assert body["answer"] == NO_SOURCES_ANSWER
    assert body["sources"] == [] and body["layers"] == []
    assert body["model"] is None
    assert body["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert body["cost_usd"] == 0.0
    assert aws["bedrock-runtime"].calls == []


def test_only_foreign_hits_count_as_no_hits(aws: dict[str, Any]) -> None:
    aws["bedrock-agent-runtime"].results = [hit(workspace="pellwick")]
    status, body = call(event())
    assert status == 200
    assert body["answer"] == NO_SOURCES_ANSWER
    assert aws["bedrock-runtime"].calls == []


@pytest.mark.parametrize(
    ("request_event", "status"),
    [
        (event(key="wrong-key"), 401),
        (event(key=None), 401),
        (event(key="quillmoor-key-0001, pellwick-key-0002"), 401),
        (event({"question": "How?", "workspace": "pellwick"}), 403),
        (event({"question": "", "workspace": "quillmoor"}), 400),
        (event({"question": "x" * (MAX_QUESTION_CHARACTERS + 1), "workspace": "quillmoor"}), 400),
        (event({"question": "How?"}), 400),
        (event("{not json"), 400),
        (event(method="GET"), 405),
    ],
    ids=[
        "wrong-key",
        "no-key",
        "two-keys-joined-by-comma",
        "other-workspace",
        "empty-question",
        "long-question",
        "no-workspace",
        "broken-json",
        "get",
    ],
)
def test_refused_requests_get_a_status_and_one_sentence(
    aws: dict[str, Any], request_event: dict[str, Any], status: int
) -> None:
    actual_status, body = call(request_event)
    assert actual_status == status
    assert set(body) == {"error"}
    assert body["error"].endswith(".")
    assert "Traceback" not in body["error"]
    assert aws["bedrock-agent-runtime"].calls == []
    assert aws["bedrock-runtime"].calls == []


def test_an_event_without_headers_is_401(aws: dict[str, Any]) -> None:
    request = event()
    del request["headers"]
    assert call(request)[0] == 401


def test_the_401_carries_no_scheme_and_the_405_the_allowed_method(aws: dict[str, Any]) -> None:
    unauthorized = respond(event(key="wrong-key"))
    assert unauthorized["statusCode"] == 401
    assert "WWW-Authenticate" not in unauthorized["headers"]

    # Without any key the sentence names the header to send it in.
    assert KEY_HEADER in respond(event(key=None))["body"]

    not_allowed = respond(event(method="PUT"))
    assert not_allowed["statusCode"] == 405
    assert not_allowed["headers"]["Allow"] == "POST"

    answered = respond(event())
    assert "Allow" not in answered["headers"]


def test_the_key_is_checked_before_the_question(aws: dict[str, Any]) -> None:
    """An empty question without a valid key learns nothing but 401."""
    status, _ = call(event({"question": "", "workspace": "quillmoor"}, key="wrong-key"))
    assert status == 401


def test_bedrock_failures_are_502_with_the_cause(aws: dict[str, Any]) -> None:
    aws["bedrock-runtime"].error = client_error("ThrottlingException")
    status, body = call(event())
    assert status == 502
    assert "throttled" in body["error"]

    aws["bedrock-runtime"].error = None
    aws["bedrock-agent-runtime"].error = client_error("AccessDeniedException", operation="Retrieve")
    status, body = call(event())
    assert status == 502
    assert body["error"].startswith("The knowledge base could not be searched")


def test_an_empty_model_answer_is_502(aws: dict[str, Any]) -> None:
    aws["bedrock-runtime"].response = bedrock_response([{"text": " "}])
    status, body = call(event())
    assert status == 502
    assert "no text" in body["error"]


def test_a_broken_configuration_tells_the_caller_nothing_but_the_log_everything(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    monkeypatch.setenv("NUMBER_OF_RESULTS", "zero")
    status, body = call(event())
    assert status == 500
    assert body == {"error": NOT_CONFIGURED_MESSAGE}
    (line,) = [line for line in log_lines(caplog) if line["event"] == "failed"]
    assert "NUMBER_OF_RESULTS" in line["error"]


def test_a_bug_is_500_without_a_traceback_in_the_body(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any) -> Any:
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(handler, "answer_question", broken)
    status, body = call(event())
    assert status == 500
    assert "ZeroDivisionError" not in body["error"]
    assert "Traceback" not in json.dumps(body)


# --- Logging -------------------------------------------------------------


def log_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(record.getMessage()) for record in caplog.records if record.name == "andur"]


def test_an_answer_logs_one_line_with_tokens_hits_dropped_and_latency(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    aws["bedrock-agent-runtime"].results.append(hit(workspace="pellwick"))
    call(event())
    (line,) = [line for line in log_lines(caplog) if line["event"] == "answered"]
    assert line == {
        "event": "answered",
        "status": 200,
        "workspace": "quillmoor",
        "model": ENVIRONMENT["MODEL_ID"],
        "input_tokens": 1624,
        "output_tokens": 50,
        "hits": 2,
        "dropped": 1,
        "cited": 1,
        "guardrail": "none",
        "guardrail_policies": [],
        "excerpt_policies": [],
        "grounding_score": 0.93,
        "relevance_score": 0.88,
        "guardrail_units": {
            "topicPolicyUnits": 2,
            "sensitiveInformationPolicyUnits": 2,
            "contextualGroundingPolicyUnits": 3,
        },
        "cost_usd": 0.001076,
        "price_date": "2026-09-23",
        "latency_ms": line["latency_ms"],
    }


@pytest.mark.parametrize(
    ("request_event", "status", "workspace"),
    [
        (
            event({"question": "What is the secret plan?", "workspace": "quillmoor"}, key="wrong"),
            401,
            "quillmoor",
        ),
        (event({"question": "What is the secret plan?", "workspace": "pellwick"}), 403, "pellwick"),
        (
            event({"question": "What is the secret plan?", "workspace": "quillmoor"}),
            200,
            "quillmoor",
        ),
    ],
    ids=["401", "403", "200"],
)
def test_every_request_is_logged_with_its_workspace_and_never_with_the_question(
    aws: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    request_event: dict[str, Any],
    status: int,
    workspace: str,
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    call(request_event)
    request_lines = [
        line for line in log_lines(caplog) if line["event"] in {"answered", "refused", "failed"}
    ]
    assert [(line["status"], line["workspace"]) for line in request_lines] == [(status, workspace)]
    assert "secret plan" not in caplog.text
    assert KEYS["quillmoor"] not in caplog.text


def test_a_bedrock_failure_is_logged_by_its_code_and_never_by_its_message(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A service message quotes what it choked on — for converse, the prompt.

    The prompt carries the question and the passages behind it, out of a
    company's documents. Which call failed and why is the error code; the
    message adds nothing a person needs and everything that may not be kept.
    """
    caplog.set_level(logging.INFO, logger="andur")
    aws["bedrock-runtime"].error = client_error(
        "ValidationException",
        "malformed input near 'the secret plan' (arn:aws:bedrock:us-east-1:123456789012:thing)",
    )
    _, body = call(event())
    (line,) = [line for line in log_lines(caplog) if line["event"] == "converse-failed"]
    assert line["detail"] == "ClientError: ValidationException"
    assert "secret plan" not in caplog.text and "123456789012" not in caplog.text
    assert "secret plan" not in json.dumps(body) and "123456789012" not in json.dumps(body)


def test_a_retrieval_failure_is_logged_by_its_code_and_never_by_its_message(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The same for the search: a retrieve message can quote the question."""
    caplog.set_level(logging.INFO, logger="andur")
    aws["bedrock-agent-runtime"].error = client_error(
        "ValidationException",
        "bad query 'the secret plan' (arn:aws:bedrock:us-east-1:123456789012:thing)",
    )
    _, body = call(event())
    (line,) = [line for line in log_lines(caplog) if line["event"] == "retrieve-failed"]
    assert line["detail"] == "ClientError: ValidationException"
    assert "secret plan" not in caplog.text and "123456789012" not in caplog.text
    assert "secret plan" not in json.dumps(body) and "123456789012" not in json.dumps(body)


def test_a_crash_is_one_json_line_with_the_traceback_as_a_field(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")

    def broken(*_: Any) -> Any:
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(handler, "answer_question", broken)
    call(event())
    (record,) = [
        record
        for record in caplog.records
        if record.name == "andur" and "crashed" in record.getMessage()
    ]
    message = record.getMessage()
    assert "\n" not in message
    line = json.loads(message)
    assert line["event"] == "crashed" and line["workspace"] == "quillmoor"
    assert "ZeroDivisionError: a bug" in line["traceback"]
    assert record.exc_info is None


def test_every_log_line_is_single_line_json(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    call(event({"question": "Zeilen\numbruch?", "workspace": "quillmoor"}))
    call(event(key="wrong"))
    messages = [record.getMessage() for record in caplog.records if record.name == "andur"]
    assert messages
    for message in messages:
        assert "\n" not in message
        json.loads(message)

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
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import handler  # app/handler.py
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
            "s3Location": {"uri": f"s3://andur-documents-000000000000-us-east-1/workspaces/{workspace}/{layer}/{name}"},
            "type": "S3",
        },
        "metadata": {"workspace": workspace, "layer": layer, "source": "fictional"},
        "score": score,
    }


def bedrock_response(content: list[dict[str, Any]], stop_reason: str = "end_turn") -> dict[str, Any]:
    """The shape the Bedrock converse API returns."""
    return {
        "output": {"message": {"content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 1624, "outputTokens": 50, "totalTokens": 1674},
    }


def parameters(keys: dict[str, str]) -> list[dict[str, str]]:
    return [{"Name": f"/andur/api-keys/{workspace}", "Value": key} for workspace, key in keys.items()]


class FakeAgentRuntime:
    def __init__(self, results: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.results = results if results is not None else []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"retrievalResults": self.results}


class FakeBedrockRuntime:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response or bedrock_response([{"text": "Use Settings > Export [1]."}])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeSSM:
    """Parameter Store; ``pages`` is the store as it is now and can change between reads."""

    def __init__(self, pages: list[list[dict[str, str]]] | None = None, error: Exception | None = None) -> None:
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
        "Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}
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


class FakeDynamoDB:
    """The budget table: one item per key hash, and this handler's one update.

    ``items`` is the table as it is now and can be changed between calls, the
    way a CLI command would change it.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        assert kwargs["ConditionExpression"] == handler.BUDGET_CONDITION, "unknown condition"
        key = kwargs["Key"][handler.BUDGET_KEY_ATTRIBUTE]["S"]
        before, values = self.items.get(key, {}), kwargs["ExpressionAttributeValues"]
        if not self.passes(before, values):
            returned = before if kwargs.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD" else None
            raise conditional_check_failed(returned or None)
        self.items[key] = apply_update(kwargs["UpdateExpression"], before, values)
        return {}

    @staticmethod
    def passes(before: dict[str, Any], values: dict[str, Any]) -> bool:
        """`handler.BUDGET_CONDITION`, spelled out. A missing attribute compares false."""
        if "first_used_at" not in before:
            return True
        used = handler.budget_number(before, "questions_used")
        limit = handler.budget_number(before, "question_limit")
        expires = handler.budget_number(before, "expires_at")
        if used is None or limit is None or expires is None:
            return False
        return used < limit and expires > int(values[":now"]["N"])


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
        "bedrock-agent-runtime": FakeAgentRuntime([hit(), hit(layer="inbox", name="draft--01.md", text="Draft.")]),
        "bedrock-runtime": FakeBedrockRuntime(),
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


@pytest.mark.parametrize("value", ["0", "101", "six", "-1", "6.5"])
def test_number_of_results_must_be_a_sensible_whole_number(value: str) -> None:
    with pytest.raises(ConfigurationError, match="NUMBER_OF_RESULTS"):
        handler.load_settings({**ENVIRONMENT, "NUMBER_OF_RESULTS": value})


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
    [{"body": "{broken"}, {"body": None}, {}, {"body": "[1, 2]"}, {"body": "%%%", "isBase64Encoded": True}],
    ids=["broken", "none", "missing", "array", "bad-base64"],
)
def test_a_body_that_is_no_json_object_is_a_bad_request(request_event: dict[str, Any]) -> None:
    with pytest.raises(BadRequest):
        handler.read_body(request_event)


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
    ids=["missing", "empty", "not-a-string", "spaces-and-capitals", "double-hyphen", "trailing-newline"],
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
    assert len(handler.requested_question({"question": "x" * MAX_QUESTION_CHARACTERS})) == MAX_QUESTION_CHARACTERS
    for body in ({}, {"question": None}, {"question": " \n "}, {"question": "x" * (MAX_QUESTION_CHARACTERS + 1)}):
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
    assert handler.read_workspace_keys("/andur/api-keys", "us-east-1") == {"quillmoor": "K1", "pellwick": "K2"}
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


def test_unreadable_keys_are_a_configuration_error_and_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = FakeSSM(error=client_error("AccessDeniedException", operation="GetParametersByPath"))
    monkeypatch.setattr(handler, "client", lambda service, region: failing)
    with pytest.raises(ConfigurationError, match="could not be read: access denied"):
        handler.workspace_keys("/andur/api-keys", "us-east-1")
    assert handler._key_cache.read_at is None

    monkeypatch.setattr(handler, "client", lambda service, region: FakeSSM([parameters({"quillmoor": "K1"})]))
    assert handler.workspace_keys("/andur/api-keys", "us-east-1") == {"quillmoor": "K1"}


def test_keys_are_kept_until_they_expire(monkeypatch: pytest.MonkeyPatch, cold_start: FakeClock) -> None:
    ssm = FakeSSM([parameters(KEYS)])
    monkeypatch.setattr(handler, "client", lambda service, region: ssm)

    handler.workspace_keys("/andur/api-keys", "us-east-1")
    cold_start.advance(handler.KEY_CACHE_SECONDS - 1)
    handler.workspace_keys("/andur/api-keys", "us-east-1")
    assert len(ssm.calls) == 1

    cold_start.advance(1)
    handler.workspace_keys("/andur/api-keys", "us-east-1")
    assert len(ssm.calls) == 2


def test_a_deleted_key_stops_working_once_the_keys_expire(aws: dict[str, Any], cold_start: FakeClock) -> None:
    assert call(event())[0] == 200
    aws["ssm"].pages = [parameters({"pellwick": KEYS["pellwick"]})]

    cold_start.advance(handler.KEY_CACHE_SECONDS)
    assert call(event())[0] == 401


def test_a_new_key_works_at_once_without_waiting_for_expiry(aws: dict[str, Any], cold_start: FakeClock) -> None:
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


def test_a_flood_of_unknown_keys_rereads_at_most_once_per_interval(aws: dict[str, Any], cold_start: FakeClock) -> None:
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
    result = subprocess.run([sys.executable, "-c", code], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
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
    assert call(event({"question": "x" * (MAX_QUESTION_CHARACTERS + 1), "workspace": "quillmoor"}))[0] == 400
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
        "ReturnValuesOnConditionCheckFailure",
    }
    assert update["TableName"] == ENVIRONMENT["BUDGET_TABLE_NAME"]
    assert update["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"


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
    assert call(event({"question": "How?", "workspace": "pellwick"}, key=KEYS["pellwick"]))[0] == 200


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
    closed = {"expires_at": {"N": str(now)}, "questions_used": {"N": "3"}, "question_limit": {"N": "10"}}
    spent = {"expires_at": {"N": str(now + 1)}, "questions_used": {"N": "10"}, "question_limit": {"N": "10"}}
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
        "number": 1, "file": "unknown", "layer": "unknown", "workspace": "unknown",
        "score": None, "words": 0, "cited": False, "excerpt": "",
    }


def test_prompt_numbers_the_sources_and_ends_with_the_question() -> None:
    table = "| Plan | Price |\n|---|---|\n| Team | €19 |"
    prompt = handler.build_prompt(
        "What does Team cost?",
        [hit(text="First passage."), hit(layer="inbox", name="draft--01.md", text=table)],
    )
    assert prompt.startswith("Sources:\n\n[1] layer: human | file: faq--03-projects-and-exports.md\nFirst passage.")
    assert "[2] layer: inbox | file: draft--01.md\n" + table in prompt
    assert prompt.endswith("\n\nQuestion: What does Team cost?")


def test_an_outlier_passage_is_cut_in_the_prompt_with_a_note() -> None:
    outlier = "word " * 2000
    prompt = handler.build_prompt("How?", [hit(text=outlier), hit(text="Short.")])
    first_block = prompt.split("\n\n[2]")[0]
    assert len(first_block) < MAX_SOURCE_CHARACTERS + 200
    assert f"[... cut: the passage is longer than {MAX_SOURCE_CHARACTERS} characters]" in first_block
    assert "[2] layer: human | file: faq--03-projects-and-exports.md\nShort." in prompt


def test_a_passage_at_the_limit_is_not_cut() -> None:
    text = "x" * MAX_SOURCE_CHARACTERS
    assert handler.capped(text) == text


def test_system_prompt_holds_the_rules_the_response_relies_on() -> None:
    assert NOT_IN_SOURCES_ANSWER in handler.SYSTEM_PROMPT
    assert "[1]" in handler.SYSTEM_PROMPT
    assert "only from the numbered sources" in handler.SYSTEM_PROMPT


# --- Model ---------------------------------------------------------------


def test_model_gets_system_rules_prompt_and_token_limit() -> None:
    bedrock = FakeBedrockRuntime()
    handler.ask_model(bedrock, "model-x", "Sources: ...")
    (arguments,) = bedrock.calls
    assert arguments["modelId"] == "model-x"
    assert arguments["system"] == [{"text": handler.SYSTEM_PROMPT}]
    assert arguments["messages"] == [{"role": "user", "content": [{"text": "Sources: ..."}]}]
    assert arguments["inferenceConfig"] == {"maxTokens": MAX_ANSWER_TOKENS}


def test_a_blocked_model_is_an_llm_error_with_a_hint() -> None:
    bedrock = FakeBedrockRuntime(error=client_error("AccessDeniedException"))
    with pytest.raises(LLMError, match="may not be enabled") as error:
        handler.ask_model(bedrock, "model-x", "p")
    assert error.value.status == 502


def test_a_model_timeout_is_an_llm_error() -> None:
    exceptions = pytest.importorskip("botocore.exceptions")
    bedrock = FakeBedrockRuntime(error=exceptions.ReadTimeoutError(endpoint_url="https://bedrock-runtime"))
    with pytest.raises(LLMError, match="timeout"):
        handler.ask_model(bedrock, "model-x", "p")


def test_extract_answer_returns_the_text() -> None:
    assert handler.extract_answer(bedrock_response([{"text": "Hello."}])) == "Hello."


def test_extract_answer_skips_a_block_without_text() -> None:
    response = bedrock_response([{"reasoningContent": {}}, {"text": "Second block."}])
    assert handler.extract_answer(response) == "Second block."


@pytest.mark.parametrize(
    ("stop_reason", "note"),
    [("max_tokens", f"cut off: the limit of {MAX_ANSWER_TOKENS} tokens"), ("content_filtered", "content filter"),
     ("guardrail_intervened", "guardrail")],
)
def test_extract_answer_marks_an_answer_the_model_did_not_finish(stop_reason: str, note: str) -> None:
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
    aws["bedrock-runtime"].response = bedrock_response([{"text": "Settings > Export [1]. A draft adds more [2]."}])
    status, body = call(event())

    assert status == 200
    assert body["answer"] == "Settings > Export [1]. A draft adds more [2]."
    assert [(s["number"], s["layer"], s["cited"]) for s in body["sources"]] == [(1, "human", True), (2, "inbox", True)]
    assert {s["workspace"] for s in body["sources"]} == {"quillmoor"}
    assert body["layers"] == ["human", "inbox"]
    assert body["model"] == ENVIRONMENT["MODEL_ID"]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 1624, "output_tokens": 50}
    assert isinstance(body["latency_ms"], int)
    assert set(body) == {"answer", "sources", "layers", "model", "stop_reason", "usage", "latency_ms"}


def test_the_request_goes_to_the_workspace_and_model_from_the_environment(aws: dict[str, Any]) -> None:
    call(event())
    (retrieve,) = aws["bedrock-agent-runtime"].calls
    assert retrieve["knowledgeBaseId"] == "KB123"
    assert retrieve["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]["equals"]["value"] == "quillmoor"
    assert retrieve["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 6
    (converse,) = aws["bedrock-runtime"].calls
    assert converse["modelId"] == ENVIRONMENT["MODEL_ID"]


def test_a_passage_of_another_workspace_never_reaches_the_model(aws: dict[str, Any]) -> None:
    aws["bedrock-agent-runtime"].results = [hit(), hit(workspace="pellwick", text="Pellwick secret.")]
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
    ids=["wrong-key", "no-key", "two-keys-joined-by-comma", "other-workspace", "empty-question",
         "long-question", "no-workspace", "broken-json", "get"],
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


def test_a_bug_is_500_without_a_traceback_in_the_body(aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
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
        "latency_ms": line["latency_ms"],
    }


@pytest.mark.parametrize(
    ("request_event", "status", "workspace"),
    [
        (event({"question": "What is the secret plan?", "workspace": "quillmoor"}, key="wrong"), 401, "quillmoor"),
        (event({"question": "What is the secret plan?", "workspace": "pellwick"}), 403, "pellwick"),
        (event({"question": "What is the secret plan?", "workspace": "quillmoor"}), 200, "quillmoor"),
    ],
    ids=["401", "403", "200"],
)
def test_every_request_is_logged_with_its_workspace_and_never_with_the_question(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture, request_event: dict[str, Any], status: int, workspace: str
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    call(request_event)
    request_lines = [line for line in log_lines(caplog) if line["event"] in {"answered", "refused", "failed"}]
    assert [(line["status"], line["workspace"]) for line in request_lines] == [(status, workspace)]
    assert "secret plan" not in caplog.text
    assert KEYS["quillmoor"] not in caplog.text


def test_bedrock_failure_details_go_to_the_log_not_the_body(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    aws["bedrock-runtime"].error = client_error("AccessDeniedException", "arn:aws:bedrock:us-east-1:123456789012:thing")
    _, body = call(event())
    assert "123456789012" not in json.dumps(body)
    assert any(line["event"] == "converse-failed" and "123456789012" in line["detail"] for line in log_lines(caplog))


def test_a_crash_is_one_json_line_with_the_traceback_as_a_field(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")

    def broken(*_: Any) -> Any:
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(handler, "answer_question", broken)
    call(event())
    (record,) = [record for record in caplog.records if record.name == "andur" and "crashed" in record.getMessage()]
    message = record.getMessage()
    assert "\n" not in message
    line = json.loads(message)
    assert line["event"] == "crashed" and line["workspace"] == "quillmoor"
    assert "ZeroDivisionError: a bug" in line["traceback"]
    assert record.exc_info is None


def test_every_log_line_is_single_line_json(aws: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    call(event({"question": "Zeilen\numbruch?", "workspace": "quillmoor"}))
    call(event(key="wrong"))
    messages = [record.getMessage() for record in caplog.records if record.name == "andur"]
    assert messages
    for message in messages:
        assert "\n" not in message
        json.loads(message)

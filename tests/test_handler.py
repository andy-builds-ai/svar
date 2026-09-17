"""Tests for the answer function (app/handler.py), without AWS.

Every AWS client is a small fake, and every Bedrock response a hand-built
dict in the shape Bedrock returns — the pattern of the learning chatbot's
tests. Error paths use botocore's real exception classes and are skipped
when botocore is not installed; one test proves the module imports without
boto3 at all.
"""

import base64
import json
import logging
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app import handler
from app.handler import (
    MAX_ANSWER_TOKENS,
    MAX_QUESTION_CHARACTERS,
    NO_SOURCES_ANSWER,
    NOT_IN_SOURCES_ANSWER,
    BadRequest,
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
}
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


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    """Clients and keys are cached per cold start; every test starts cold."""
    handler.aws_client.cache_clear()
    handler.workspace_keys.cache_clear()
    handler.log_sdk_versions.cache_clear()
    yield
    handler.aws_client.cache_clear()
    handler.workspace_keys.cache_clear()
    handler.log_sdk_versions.cache_clear()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The function's environment, its keys and one fake client per service."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    clients: dict[str, Any] = {
        "ssm": FakeSSM([[{"Name": f"/andur/api-keys/{w}", "Value": k} for w, k in KEYS.items()]]),
        "bedrock-agent-runtime": FakeAgentRuntime([hit(), hit(layer="inbox", name="draft--01.md", text="Draft.")]),
        "bedrock-runtime": FakeBedrockRuntime(),
    }
    monkeypatch.setattr(handler, "aws_client", lambda service, region: clients[service])
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
    headers = {"content-type": "application/json"}
    if key is not None:
        headers["authorization"] = f"Bearer {key}"
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": method}},
        "headers": headers,
        "body": body if isinstance(body, str) else json.dumps(body),
        "isBase64Encoded": False,
        **extra,
    }


def call(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = handler.handler(request, None)
    assert response["headers"]["Content-Type"].startswith("application/json")
    return response["statusCode"], json.loads(response["body"])


# --- Settings ------------------------------------------------------------


def test_settings_come_from_the_environment() -> None:
    settings = handler.load_settings(ENVIRONMENT)
    assert settings.knowledge_base_id == "KB123"
    assert settings.model_id == "nvidia.nemotron-super-3-120b"
    assert settings.number_of_results == 6
    assert settings.key_parameter_path == "/andur/api-keys"


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
    from scripts import ingest

    assert handler.WORKSPACE_NAME.pattern == ingest.WORKSPACE_NAME.pattern
    assert handler.LAYERS == ingest.LAYERS


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


@pytest.mark.parametrize("body", [{}, {"workspace": ""}, {"workspace": 7}, {"workspace": "Quill Moor"}, {"workspace": "a--b"}])
def test_workspace_must_be_a_valid_name(body: dict[str, Any]) -> None:
    with pytest.raises(BadRequest):
        handler.requested_workspace(body)


def test_question_is_stripped_and_limited() -> None:
    assert handler.requested_question({"question": "  How?  "}) == "How?"
    assert len(handler.requested_question({"question": "x" * MAX_QUESTION_CHARACTERS})) == MAX_QUESTION_CHARACTERS
    for body in ({}, {"question": None}, {"question": " \n "}, {"question": "x" * (MAX_QUESTION_CHARACTERS + 1)}):
        with pytest.raises(BadRequest):
            handler.requested_question(body)


def test_bearer_key_is_read_from_the_authorization_header_in_any_case() -> None:
    assert handler.bearer_key({"authorization": "Bearer abc"}) == "abc"
    assert handler.bearer_key({"Authorization": "bearer  abc "}) == "abc"


@pytest.mark.parametrize(
    "headers",
    [None, {}, {"x-api-key": "abc"}, {"authorization": "Bearer "}, {"authorization": "Basic abc"}, {"authorization": "abc"}],
)
def test_no_bearer_key_is_unauthorized(headers: dict[str, str] | None) -> None:
    with pytest.raises(Unauthorized):
        handler.bearer_key(headers)


# --- Keys ----------------------------------------------------------------


def test_keys_are_mapped_to_workspaces_by_parameter_name() -> None:
    parameters = [
        {"Name": "/andur/api-keys/quillmoor", "Value": "K1"},
        {"Name": "/andur/api-keys/Not A Workspace", "Value": "K2"},
        {"Name": "/andur/api-keysX/pellwick", "Value": "K3"},
        {"Name": "/other/pellwick", "Value": "K4"},
    ]
    assert handler.keys_by_workspace(parameters, "/andur/api-keys") == {"quillmoor": "K1"}


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


def test_keys_are_read_decrypted_across_pages_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    ssm = FakeSSM([[{"Name": "/andur/api-keys/quillmoor", "Value": "K1"}], [{"Name": "/andur/api-keys/pellwick", "Value": "K2"}]])
    created: list[str] = []
    monkeypatch.setattr(handler, "aws_client", lambda service, region: created.append(service) or ssm)

    assert handler.workspace_keys("/andur/api-keys", "us-east-1") == {"quillmoor": "K1", "pellwick": "K2"}
    assert handler.workspace_keys("/andur/api-keys", "us-east-1") == {"quillmoor": "K1", "pellwick": "K2"}
    assert ssm.calls == [{"Path": "/andur/api-keys", "Recursive": False, "WithDecryption": True}]
    assert created == ["ssm"]


def test_no_stored_keys_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "aws_client", lambda service, region: FakeSSM([[]]))
    with pytest.raises(ConfigurationError, match="No workspace keys"):
        handler.workspace_keys("/andur/api-keys", "us-east-1")


def test_unreadable_keys_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = FakeSSM(error=client_error("AccessDeniedException", operation="GetParametersByPath"))
    monkeypatch.setattr(handler, "aws_client", lambda service, region: failing)
    with pytest.raises(ConfigurationError, match="could not be read: access denied"):
        handler.workspace_keys("/andur/api-keys", "us-east-1")

    working = FakeSSM([[{"Name": "/andur/api-keys/quillmoor", "Value": "K1"}]])
    monkeypatch.setattr(handler, "aws_client", lambda service, region: working)
    assert handler.workspace_keys("/andur/api-keys", "us-east-1") == {"quillmoor": "K1"}


# --- AWS errors ----------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ThrottlingException", "throttled"),
        ("ServiceQuotaExceededException", "throttled"),
        ("AccessDeniedException", "access denied"),
        ("ResourceNotFoundException", "not found"),
        ("ModelNotReadyException", "unavailable"),
        ("ValidationException", "ValidationException"),
    ],
)
def test_aws_error_cause_names_the_cause(code: str, expected: str) -> None:
    assert expected in handler.aws_error_cause(client_error(code))


def test_aws_error_cause_for_timeouts_and_unreachable_endpoints() -> None:
    exceptions = pytest.importorskip("botocore.exceptions")
    assert "timeout" in handler.aws_error_cause(exceptions.ReadTimeoutError(endpoint_url="https://x"))
    assert "timeout" in handler.aws_error_cause(exceptions.ConnectTimeoutError(endpoint_url="https://x"))
    assert "could not be reached" in handler.aws_error_cause(exceptions.EndpointConnectionError(endpoint_url="https://x"))


def test_aws_error_cause_never_repeats_the_message() -> None:
    """AWS messages carry ARNs, and ARNs the account id."""
    error = client_error("AccessDeniedException", "User: arn:aws:sts::123456789012:assumed-role/x is not authorized")
    assert "123456789012" not in handler.aws_error_cause(error)


def test_worst_case_counts_every_attempt_and_retry_pause() -> None:
    expected = sum(
        handler.MAX_ATTEMPTS * (connect + read) + (handler.MAX_ATTEMPTS - 1) * handler.RETRY_DELAY_SECONDS
        for connect, read in handler.CLIENT_TIMEOUTS.values()
    )
    assert handler.worst_case_seconds() == expected
    assert set(handler.CLIENT_TIMEOUTS) == {"ssm", "bedrock-agent-runtime", "bedrock-runtime"}


def test_clients_get_timeouts_and_standard_retries() -> None:
    pytest.importorskip("boto3")
    client = handler.aws_client("bedrock-runtime", "us-east-1")
    connect, read = handler.CLIENT_TIMEOUTS["bedrock-runtime"]
    assert client.meta.config.connect_timeout == connect
    assert client.meta.config.read_timeout == read
    assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": handler.MAX_ATTEMPTS}
    assert handler.aws_client("bedrock-runtime", "us-east-1") is client


def test_missing_boto3_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def without_boto3(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {"boto3", "botocore.config"}:
            raise ImportError(f"simulated: {name} not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_boto3)
    with pytest.raises(ConfigurationError, match="boto3"):
        handler.aws_client("ssm", "us-east-1")


def test_the_module_imports_and_reads_requests_without_boto3() -> None:
    """boto3 is imported lazily: a missing SDK must not break the import."""
    code = (
        "import sys; sys.modules['boto3'] = None; sys.modules['botocore'] = None\n"
        "from app import handler\n"
        "print(handler.requested_workspace({'workspace': 'quillmoor'}))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "quillmoor"


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


def test_extract_answer_marks_a_truncated_answer() -> None:
    result = handler.extract_answer(bedrock_response([{"text": "Start"}], stop_reason="max_tokens"))
    assert result.startswith("Start")
    assert "cut off" in result
    assert str(MAX_ANSWER_TOKENS) in result


def test_extract_answer_raises_without_any_text_block() -> None:
    with pytest.raises(LLMError, match="tool_use"):
        handler.extract_answer(bedrock_response([{"toolUse": {}}], stop_reason="tool_use"))


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
        ("Invented [9] and [0].", set()),
        ("A date [2026-09-17] is no citation.", set()),
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


def test_no_hits_means_no_model_call(aws: dict[str, Any]) -> None:
    aws["bedrock-agent-runtime"].results = []
    status, body = call(event())

    assert status == 200
    assert body["answer"] == NO_SOURCES_ANSWER
    assert body["sources"] == [] and body["layers"] == []
    assert body["model"] is None
    assert body["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert aws["bedrock-runtime"].calls == []


@pytest.mark.parametrize(
    ("request_event", "status"),
    [
        (event(key="wrong-key"), 401),
        (event(key=None), 401),
        (event({"question": "How?", "workspace": "pellwick"}), 403),
        (event({"question": "", "workspace": "quillmoor"}), 400),
        (event({"question": "x" * (MAX_QUESTION_CHARACTERS + 1), "workspace": "quillmoor"}), 400),
        (event({"question": "How?"}), 400),
        (event("{not json"), 400),
        (event(method="GET"), 405),
    ],
    ids=["wrong-key", "no-key", "other-workspace", "empty-question", "long-question", "no-workspace", "broken-json", "get"],
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


def test_a_broken_configuration_is_500(aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_ID")
    status, body = call(event())
    assert status == 500
    assert "MODEL_ID" in body["error"]


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


def test_an_answer_logs_one_line_with_tokens_hits_and_latency(
    aws: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
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


def test_log_lines_are_single_line_json(aws: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    call(event({"question": "Zeilen\numbruch?", "workspace": "quillmoor"}))
    assert all(not re.search(r"\n", record.getMessage()) for record in caplog.records if record.name == "andur")

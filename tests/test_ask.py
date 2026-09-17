"""Tests for the ask script, without AWS and without the network."""

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from scripts import ask
from scripts.aws import AwsProblem

KEY = "a-secret-workspace-key-that-must-never-be-printed"

ANSWER = {
    "answer": "Open Settings > Export [1].",
    "sources": [
        {"number": 1, "file": "faq--03.md", "layer": "human", "workspace": "quillmoor", "score": 0.8147,
         "words": 156, "cited": True, "excerpt": "..."},
        {"number": 2, "file": "draft--01.md", "layer": "inbox", "workspace": "quillmoor", "score": 0.7,
         "words": 22, "cited": False, "excerpt": "..."},
    ],
    "layers": ["human"],
    "model": "nvidia.nemotron-super-3-120b",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1900, "output_tokens": 131},
    "latency_ms": 2820,
}


class FakeClientError(Exception):
    __module__ = "botocore.exceptions"

    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeSSM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": KEY}}


def test_request_is_a_json_post_with_the_bearer_key() -> None:
    request = ask.build_request("https://example.lambda-url.us-east-1.on.aws/", KEY, "How?", "quillmoor")
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("Content-type") == "application/json"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {"question": "How?", "workspace": "quillmoor"}


def test_key_is_read_decrypted_from_the_workspace_parameter() -> None:
    ssm = FakeSSM()
    assert ask.read_key(ssm, "/andur/api-keys", "pellwick") == KEY
    assert ssm.calls == [{"Name": "/andur/api-keys/pellwick", "WithDecryption": True}]


def test_missing_key_parameter_says_what_to_check() -> None:
    with pytest.raises(AwsProblem, match="WORKSPACES"):
        ask.read_key(FakeSSM(FakeClientError("ParameterNotFound")), "/andur/api-keys", "nobody")


def http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", status, "reason", {}, io.BytesIO(body))  # type: ignore[arg-type]


def test_send_returns_the_error_body_of_a_refused_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise http_error(401, b'{"error": "The key is not valid."}')

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert ask.send(ask.build_request("https://x", KEY, "How?", "quillmoor")) == (401, {"error": "The key is not valid."})


def test_send_wraps_a_body_that_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def gateway(*_: Any, **__: Any) -> Any:
        raise http_error(502, b"Internal Server Error")

    monkeypatch.setattr(urllib.request, "urlopen", gateway)
    assert ask.send(ask.build_request("https://x", KEY, "How?", "quillmoor")) == (502, {"error": "Internal Server Error"})


def test_an_unreachable_url_is_a_readable_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*_: Any, **__: Any) -> Any:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(AwsProblem, match="could not be reached"):
        ask.send(ask.build_request("https://x", KEY, "How?", "quillmoor"))


def test_answer_is_printed_with_layers_sources_and_usage() -> None:
    lines = ask.format_answer("quillmoor", ANSWER)
    assert lines[:3] == ["Answer (quillmoor):", "", "Open Settings > Export [1]."]
    assert "Layers cited: human" in lines
    assert any("[1] cited" in line and "faq--03.md" in line and "156 words" in line for line in lines)
    assert any("[2]" in line and "cited" not in line for line in lines)
    assert lines[-1] == "nvidia.nemotron-super-3-120b · 1900 tokens in, 131 out · 2820 ms"


def test_an_answer_without_sources_says_so() -> None:
    body = {**ANSWER, "sources": [], "layers": [], "model": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
    lines = ask.format_answer("pellwick", body)
    assert "Sources: none" in lines
    assert "Layers cited: none" in lines
    assert lines[-1].startswith("no model call")


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stack outputs, Parameter Store and the Function URL, all faked."""
    seen: dict[str, Any] = {"services": [], "requests": []}
    ssm = FakeSSM()

    def fake_client(service: str, region: str) -> Any:
        seen["services"].append(service)
        return ssm

    def fake_send(request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        seen["requests"].append(request)
        return seen.get("response", (200, ANSWER))

    monkeypatch.setattr(ask, "client", fake_client)
    monkeypatch.setattr(ask, "read_stack_outputs", lambda cloudformation, stack: {
        "FunctionUrl": "https://example.lambda-url.us-east-1.on.aws/", "ApiKeyParameterPath": "/andur/api-keys",
    })
    monkeypatch.setattr(ask, "send", fake_send)
    monkeypatch.delenv(ask.API_KEY_ENVIRONMENT_VARIABLE, raising=False)
    return seen


def test_main_reads_url_and_key_and_never_prints_the_key(
    deployed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ask.main(["How do I export?", "--workspace", "quillmoor"]) == 0
    (request,) = deployed["requests"]
    assert request.full_url == "https://example.lambda-url.us-east-1.on.aws/"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    output = capsys.readouterr()
    assert "Open Settings > Export [1]." in output.out
    assert KEY not in output.out + output.err


def test_main_json_prints_the_raw_response(deployed: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert ask.main(["How?", "--workspace", "quillmoor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ANSWER


def test_main_exits_1_on_a_refused_request(deployed: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    deployed["response"] = (403, {"error": "This key does not open the workspace pellwick."})
    assert ask.main(["How?", "--workspace", "pellwick"]) == 1
    assert "HTTP 403: This key does not open the workspace pellwick." in capsys.readouterr().err


def test_key_from_the_environment_skips_parameter_store_and_stack(
    deployed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ask.API_KEY_ENVIRONMENT_VARIABLE, "key-from-env")
    assert ask.main(["How?", "--workspace", "quillmoor", "--url", "https://given.example/"]) == 0
    assert deployed["services"] == []
    (request,) = deployed["requests"]
    assert request.get_header("Authorization") == "Bearer key-from-env"


def test_main_exits_2_when_an_aws_step_fails(deployed: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def expired(cloudformation: Any, stack: str) -> dict[str, str]:
        raise AwsProblem("the AWS login is missing or expired")

    monkeypatch.setattr(ask, "read_stack_outputs", expired)
    assert ask.main(["How?", "--workspace", "quillmoor"]) == 2

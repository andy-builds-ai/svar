"""Tests for the ask script, without AWS and without the network."""

import hashlib
import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from scripts import ask
from scripts.aws import AwsProblem

KEY = "a-secret-workspace-key-that-must-never-be-printed"

WEB_URL = "https://d111111abcdef8.cloudfront.net"
ASK_URL = WEB_URL + "/ask"

ANSWER = {
    "answer": "Open Settings > Export [1].",
    "sources": [
        {
            "number": 1,
            "file": "faq--03.md",
            "layer": "human",
            "workspace": "quillmoor",
            "score": 0.8147,
            "words": 156,
            "cited": True,
            "excerpt": "...",
        },
        {
            "number": 2,
            "file": "draft--01.md",
            "layer": "inbox",
            "workspace": "quillmoor",
            "score": 0.7,
            "words": 22,
            "cited": False,
            "excerpt": "...",
        },
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


def test_request_is_a_json_post_with_the_key_in_its_own_header() -> None:
    request = ask.build_request(ASK_URL, KEY, "How?", "quillmoor")
    assert request.get_method() == "POST"
    assert request.get_header(ask.KEY_HEADER.capitalize()) == KEY
    assert request.get_header("Authorization") is None
    assert request.get_header("Content-type") == "application/json"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {"question": "How?", "workspace": "quillmoor"}


def test_the_language_goes_in_the_body_only_when_given() -> None:
    """Without it Andur answers in English; the script never guesses a language."""
    german = ask.build_request(ASK_URL, KEY, "Wie?", "quillmoor", "de")
    assert isinstance(german.data, bytes)
    assert json.loads(german.data) == {
        "question": "Wie?",
        "workspace": "quillmoor",
        "language": "de",
    }
    # The hash is of the body that is sent, the language included.
    assert (
        german.get_header(ask.BODY_HASH_HEADER.capitalize())
        == hashlib.sha256(german.data).hexdigest()
    )

    for none in (None, ""):
        plain = ask.build_request(ASK_URL, KEY, "How?", "quillmoor", none)
        assert isinstance(plain.data, bytes)
        assert "language" not in json.loads(plain.data)


def test_the_language_option_takes_german_or_english_and_nothing_else() -> None:
    assert ask.parse_args(["Wie?", "--workspace", "quillmoor"]).language is None
    assert ask.parse_args(["Wie?", "--workspace", "quillmoor", "--language", "de"]).language == "de"
    with pytest.raises(SystemExit):
        ask.parse_args(["Wie?", "--workspace", "quillmoor", "--language", "fr"])


def test_the_body_hash_header_is_the_hash_of_the_body_that_is_sent() -> None:
    """CloudFront signs the request, and a signed call to a function URL needs the hash."""
    request = ask.build_request(ASK_URL, KEY, "How?", "quillmoor")
    assert isinstance(request.data, bytes)
    expected = hashlib.sha256(request.data).hexdigest()
    assert len(expected) == 64
    assert request.get_header(ask.BODY_HASH_HEADER.capitalize()) == expected


@pytest.mark.parametrize("given", [WEB_URL, WEB_URL + "/"])
def test_the_ask_path_is_appended_with_exactly_one_slash(given: str) -> None:
    assert ask.ask_url(given) == ASK_URL


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
    assert ask.send(ask.build_request(ASK_URL, KEY, "How?", "quillmoor")) == (
        401,
        {"error": "The key is not valid."},
    )


def test_a_cloudfront_error_page_is_reported_as_what_came_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 or 504 from CloudFront never passed through the function, so it has no sentence."""

    def refuse(*_: Any, **__: Any) -> Any:
        raise http_error(504, b"<!DOCTYPE HTML><html><head><title>ERROR</title>")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    status, body = ask.send(ask.build_request(ASK_URL, KEY, "How?", "quillmoor"))
    assert status == 504
    assert body["error"].startswith("The response is not JSON:")


def test_send_wraps_a_body_that_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def gateway(*_: Any, **__: Any) -> Any:
        raise http_error(502, b"Internal Server Error")

    monkeypatch.setattr(urllib.request, "urlopen", gateway)
    assert ask.send(ask.build_request("https://x", KEY, "How?", "quillmoor")) == (
        502,
        {"error": "The response is not JSON: Internal Server Error"},
    )


def test_send_falls_back_to_the_reason_for_an_empty_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def empty(*_: Any, **__: Any) -> Any:
        raise http_error(429, b"")

    monkeypatch.setattr(urllib.request, "urlopen", empty)
    assert ask.send(ask.build_request("https://x", KEY, "How?", "quillmoor")) == (
        429,
        {"error": "reason"},
    )


class FakeResponse(io.BytesIO):
    status = 200


def test_send_reports_a_200_that_is_not_json_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_, **__: FakeResponse(b"<html>proxy page</html>")
    )
    assert ask.send(ask.build_request("https://x", KEY, "How?", "quillmoor")) == (
        200,
        {"error": "The response is not JSON: <html>proxy page</html>"},
    )


def test_main_exits_1_on_a_200_without_an_answer(
    deployed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    deployed["response"] = (200, {"error": "The response is not JSON: <html>proxy page</html>"})
    assert ask.main(["How?", "--workspace", "quillmoor"]) == 1
    assert "HTTP 200: The response is not JSON" in capsys.readouterr().err


def test_an_unreachable_url_is_a_readable_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*_: Any, **__: Any) -> Any:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(AwsProblem, match="could not be reached"):
        ask.send(ask.build_request(ASK_URL, KEY, "How?", "quillmoor"))


def test_answer_is_printed_with_layers_sources_and_usage() -> None:
    lines = ask.format_answer("quillmoor", ANSWER)
    assert lines[:3] == ["Answer (quillmoor):", "", "Open Settings > Export [1]."]
    assert "Layers cited: human" in lines
    assert any(
        "[1] cited" in line and "faq--03.md" in line and "156 words" in line for line in lines
    )
    assert any("[2]" in line and "cited" not in line for line in lines)
    assert lines[-1] == "nvidia.nemotron-super-3-120b · 1900 tokens in, 131 out · 2820 ms"


def test_an_answer_without_sources_says_so() -> None:
    body = {
        **ANSWER,
        "sources": [],
        "layers": [],
        "model": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    lines = ask.format_answer("pellwick", body)
    assert "Sources: none" in lines
    assert "Layers cited: none" in lines
    assert lines[-1].startswith("no model call")


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stack outputs, Parameter Store and the web address, all faked."""
    seen: dict[str, Any] = {"services": [], "requests": []}
    ssm = FakeSSM()

    def fake_client(service: str, region: str) -> Any:
        seen["services"].append(service)
        return ssm

    def fake_send(request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        seen["requests"].append(request)
        return seen.get("response", (200, ANSWER))

    monkeypatch.setattr(ask, "client", fake_client)
    monkeypatch.setattr(
        ask,
        "read_stack_outputs",
        lambda cloudformation, stack: {
            ask.WEB_URL_OUTPUT: WEB_URL,
            "ApiKeyParameterPath": "/andur/api-keys",
        },
    )
    monkeypatch.setattr(ask, "send", fake_send)
    monkeypatch.delenv(ask.API_KEY_ENVIRONMENT_VARIABLE, raising=False)
    return seen


def test_main_asks_the_web_address_and_never_prints_the_key(
    deployed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ask.main(["How do I export?", "--workspace", "quillmoor"]) == 0
    (request,) = deployed["requests"]
    assert request.full_url == ASK_URL
    assert request.get_header(ask.KEY_HEADER.capitalize()) == KEY
    output = capsys.readouterr()
    assert "Open Settings > Export [1]." in output.out
    assert KEY not in output.out + output.err


def test_the_function_url_output_is_not_what_the_script_asks(deployed: dict[str, Any]) -> None:
    """The function URL answers 403 to everyone but CloudFront; the script must not go there."""
    assert ask.main(["How?", "--workspace", "quillmoor"]) == 0
    (request,) = deployed["requests"]
    assert "lambda-url" not in request.full_url


def test_main_json_prints_the_raw_response(
    deployed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ask.main(["How?", "--workspace", "quillmoor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ANSWER


def test_main_exits_1_on_a_refused_request(
    deployed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
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
    assert request.full_url == "https://given.example/ask"
    assert request.get_header(ask.KEY_HEADER.capitalize()) == "key-from-env"


def test_main_exits_2_when_an_aws_step_fails(
    deployed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def expired(cloudformation: Any, stack: str) -> dict[str, str]:
        raise AwsProblem("the AWS login is missing or expired")

    monkeypatch.setattr(ask, "read_stack_outputs", expired)
    assert ask.main(["How?", "--workspace", "quillmoor"]) == 2

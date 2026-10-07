"""The function as its own address (app/page.py): the page, the way to /ask, the headers."""

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

import handler  # app/handler.py, imported by its Lambda name (tests/conftest.py)
import page
import pytest

from tests.test_handler import (  # noqa: F401 (aws and cold_start are fixtures)
    SETTINGS,
    FakeClock,
    SequencedBedrockRuntime,
    aws,
    client_error,
    cold_start,
    event,
    log_lines,
)

WEB_FOLDER = Path(__file__).resolve().parents[1] / "web"


@pytest.fixture
def served(aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # noqa: F811
    """The function as the stack sets it up without CloudFront: it serves web/ itself."""
    monkeypatch.setenv(page.PAGE_FOLDER_VARIABLE, str(WEB_FOLDER))
    page.page_files.cache_clear()
    return aws


def request(path: str, method: str = "GET", **extra: Any) -> dict[str, Any]:
    return handler.handler(event(method=method, rawPath=path, **extra), None)


@pytest.mark.parametrize(
    ("path", "name", "content_type"),
    [
        ("/", "index.html", "text/html; charset=utf-8"),
        ("/index.html", "index.html", "text/html; charset=utf-8"),
        ("/app.js", "app.js", "text/javascript; charset=utf-8"),
        ("/language.js", "language.js", "text/javascript; charset=utf-8"),
        ("/markdown.js", "markdown.js", "text/javascript; charset=utf-8"),
        ("/style.css", "style.css", "text/css; charset=utf-8"),
    ],
)
def test_every_file_of_the_page_is_served_with_its_type_and_the_security_headers(
    served: dict[str, Any], path: str, name: str, content_type: str
) -> None:
    response = request(path)
    assert response["statusCode"] == 200
    assert response["body"] == (WEB_FOLDER / name).read_text(encoding="utf-8")
    assert response["headers"]["Content-Type"] == content_type
    assert response["headers"]["Cache-Control"] == "no-cache"
    assert page.SECURITY_HEADERS.items() <= response["headers"].items()
    # Reading the page is no question: no key is read, nothing is charged.
    assert served["ssm"].calls == [] and served["dynamodb"].calls == []


def test_every_file_the_page_loads_is_one_it_serves(served: dict[str, Any]) -> None:
    """index.html loads its script and style from the same address; each must be there."""
    markup = (WEB_FOLDER / "index.html").read_text(encoding="utf-8")
    loaded = {"style.css", "app.js", "language.js", "markdown.js"}
    assert all(f'"{name}"' in markup for name in loaded)
    assert loaded <= set(page.page_files(str(WEB_FOLDER)))


def test_head_gives_the_headers_without_the_file(served: dict[str, Any]) -> None:
    response = request("/", "HEAD")
    assert response["statusCode"] == 200 and response["body"] == ""
    assert response["headers"]["Content-Type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize(
    "path",
    [
        "/README.md",
        "/../app/handler.py",
        "/..%2Fapp%2Fhandler.py",
        "/handler.py",
        "//index.html",
        "/web/index.html",
        "/favicon.ico",
        "/ask/",
        "/Index.html",
    ],
)
def test_nothing_but_the_page_is_served(served: dict[str, Any], path: str) -> None:
    """Only the page's own files, by their plain names: no other file of the folder, nothing
    above it."""
    response = request(path)
    assert response["statusCode"] == 404
    assert response["body"] == "Not found."
    assert page.SECURITY_HEADERS.items() <= response["headers"].items()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_the_page_is_only_read(served: dict[str, Any], method: str) -> None:
    response = request("/", method)
    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "GET, HEAD"
    assert served["bedrock-runtime"].calls == []


def test_a_question_to_ask_is_answered_with_the_security_headers(served: dict[str, Any]) -> None:
    response = request("/ask", "POST")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["answer"] == "Use Settings > Export [1]."
    assert response["headers"]["Content-Type"].startswith("application/json")
    assert page.SECURITY_HEADERS.items() <= response["headers"].items()


def test_ask_is_only_posted_to(served: dict[str, Any]) -> None:
    """GET /ask reaches the answer function, which allows nothing but POST, as behind
    CloudFront."""
    response = request("/ask", "GET")
    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "POST"
    assert page.SECURITY_HEADERS.items() <= response["headers"].items()


def test_a_refused_question_carries_the_security_headers_too(served: dict[str, Any]) -> None:
    response = request("/ask", "POST", headers={"content-type": "application/json"})
    assert response["statusCode"] == 401
    assert page.SECURITY_HEADERS.items() <= response["headers"].items()


def test_a_page_that_cannot_be_read_is_a_500_not_a_crash(
    served: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    monkeypatch.setenv(page.PAGE_FOLDER_VARIABLE, str(WEB_FOLDER / "missing"))
    response = request("/")
    assert response["statusCode"] == 500
    assert page.SECURITY_HEADERS.items() <= response["headers"].items()
    assert [line for line in log_lines(caplog) if line["event"] == "crashed"]


def test_behind_cloudfront_the_function_answers_as_before(aws: dict[str, Any]) -> None:  # noqa: F811
    """Without the page folder, every path is a question, as CloudFront sends only /ask, and
    CloudFront sets the headers."""
    response = handler.handler(event(rawPath="/"), None)
    assert response["statusCode"] == 200
    assert "answer" in json.loads(response["body"])
    assert "Content-Security-Policy" not in response["headers"]


def test_the_page_folder_is_a_setting_only_where_the_function_is_the_address() -> None:
    assert handler.PAGE_VARIABLES == {"page_folder": "PAGE_FOLDER"}
    assert SETTINGS.page_folder is None


@pytest.mark.parametrize(("page_folder", "retried"), [(None, False), ("/opt", True)])
def test_a_second_model_call_may_start_later_without_cloudfront(
    cold_start: FakeClock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    page_folder: str | None,
    retried: bool,
) -> None:
    """At second 100 the second call still ends inside the function's 195 seconds, not before
    CloudFront's 120 (app/aws.py)."""
    monkeypatch.setattr(handler, "pause", lambda seconds: None)
    settings = dataclasses.replace(SETTINGS, page_folder=page_folder)
    bedrock = SequencedBedrockRuntime([client_error("ThrottlingException")])
    record = handler.RequestRecord()
    cold_start.advance(100)
    if retried:
        assert handler.ask_model(bedrock, settings, [{"text": "p"}], record) == bedrock.response
    else:
        with pytest.raises(handler.LLMError):
            handler.ask_model(bedrock, settings, [{"text": "p"}], record)
    assert len(bedrock.calls) == (2 if retried else 1)

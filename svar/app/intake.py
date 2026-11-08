"""Reading the request: its method, its body's fields and the key header."""

import base64
import json
import re
from collections.abc import Mapping
from typing import Any

import errors
import sentences

WORKSPACE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

MAX_WORKSPACE_CHARACTERS = 64

MAX_QUESTION_CHARACTERS = 1000

KEY_HEADER = "X-Andur-Key"


def require_post(event: Mapping[str, Any]) -> None:
    """Refuse every HTTP method but POST."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method != "POST":
        raise errors.MethodNotAllowed(
            f"Send the question with POST, not {method or 'an unknown method'}."
        )


def read_body(event: Mapping[str, Any]) -> dict[str, Any]:
    """The request body as a JSON object."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except ValueError as exc:
            # Bad base64 (binascii.Error), a non-ASCII character in the text, or
            # bytes that are not UTF-8 (UnicodeDecodeError): all are ValueErrors.
            raise errors.BadRequest("The body is not valid base64-encoded UTF-8.") from exc
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # ValueError: malformed JSON, or an integer past Python's digit limit;
        # RecursionError: arrays or objects nested past the recursion limit.
        raise errors.BadRequest("The body is not valid JSON.") from exc
    if not isinstance(body, dict):
        raise errors.BadRequest('The body must be a JSON object with "question" and "workspace".')
    return body


def requested_language(body: Mapping[str, Any]) -> str:
    """The language the request names, or English: no error for a missing or unknown one."""
    language = body.get("language")
    if isinstance(language, str) and language.strip().lower() in sentences.LANGUAGES:
        return language.strip().lower()
    return sentences.DEFAULT_LANGUAGE


def requested_workspace(body: Mapping[str, Any]) -> str:
    """The workspace the request names, checked against the naming rule."""
    workspace = body.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        raise errors.BadRequest(
            'The body has no "workspace".', 'Die Anfrage enthält kein "workspace".'
        )
    if len(workspace) > MAX_WORKSPACE_CHARACTERS:
        raise errors.BadRequest(
            f"A workspace name is at most {MAX_WORKSPACE_CHARACTERS} characters.",
            f"Der Name eines Arbeitsbereichs hat höchstens {MAX_WORKSPACE_CHARACTERS} Zeichen.",
        )
    if not WORKSPACE_NAME.match(workspace):
        raise errors.BadRequest(
            "A workspace name is lowercase letters, digits and single hyphens.",
            "Der Name eines Arbeitsbereichs besteht aus Kleinbuchstaben, Ziffern und einzelnen "
            "Bindestrichen.",
        )
    return workspace


def requested_question(body: Mapping[str, Any]) -> str:
    """The question, stripped, between 1 and `MAX_QUESTION_CHARACTERS` long."""
    question = body.get("question")
    if not isinstance(question, str):
        raise errors.BadRequest(
            'The body has no "question" text.', 'Die Anfrage enthält keinen "question"-Text.'
        )
    question = question.strip()
    if not question:
        raise errors.BadRequest("The question is empty.", "Die Frage ist leer.")
    if len(question) > MAX_QUESTION_CHARACTERS:
        raise errors.BadRequest(
            f"The question is longer than {MAX_QUESTION_CHARACTERS} characters.",
            f"Die Frage ist länger als {MAX_QUESTION_CHARACTERS} Zeichen.",
        )
    return question


def request_key(headers: Mapping[str, str] | None) -> str:
    """The workspace key from the `KEY_HEADER` header."""
    lowered = {name.lower(): value for name, value in (headers or {}).items()}
    key = lowered.get(KEY_HEADER.lower(), "").strip()
    if not key:
        raise errors.Unauthorized(
            f"A key is required: send it as the header {KEY_HEADER}.",
            f"Ein Schlüssel ist nötig: Senden Sie ihn im Header {KEY_HEADER}.",
        )
    return key

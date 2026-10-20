"""Ask Svar a question through the web address and print the answer with its sources."""

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

from scripts import utf8_output
from scripts.aws import (
    REGION,
    STACK_NAME,
    AwsProblem,
    client,
    error_code,
    explained,
    read_stack_outputs,
    resolve,
)

#: Set this to use a key you already have instead of reading Parameter Store.
API_KEY_ENVIRONMENT_VARIABLE = "ANDUR_API_KEY"

#: The stack output that carries the address; ``/ask`` is appended to it.
WEB_URL_OUTPUT = "WebUrl"

#: The path on that address that reaches the answer function.
ASK_PATH = "/ask"

#: The header the workspace key travels in — ``KEY_HEADER`` in app/intake.py.
KEY_HEADER = "X-Andur-Key"

BODY_HASH_HEADER = "x-amz-content-sha256"

HTTP_TIMEOUT_SECONDS = 200


def read_key(ssm: Any, key_path: str, workspace: str) -> str:
    """The workspace key from Parameter Store, decrypted."""
    name = f"{key_path}/{workspace}"
    try:
        with explained(f"Reading the key {name}"):
            response = ssm.get_parameter(Name=name, WithDecryption=True)
    except AwsProblem as problem:
        if error_code(problem.__cause__) == "ParameterNotFound":
            raise AwsProblem(
                f"There is no key {name}. Is {workspace!r} spelled right? A new workspace "
                f"gets its key with `python -m scripts.workspace add {workspace}`."
            ) from problem.__cause__
        raise
    return response["Parameter"]["Value"]


#: What a header value may hold here: printable ASCII and Latin-1, no control character.
_HEADER_VALUE = re.compile(r"[ -~\xa0-\xff]+")


def header_value(key: str) -> str:
    """The key as it goes into the header: without the space and line end around it, which the
    function drops too (`request_key` in app/intake.py). A key that still cannot be a header
    is an `AwsProblem` that does not repeat it; `http.client` would name the value."""
    key = key.strip()
    if not _HEADER_VALUE.fullmatch(key):
        raise AwsProblem(
            "The workspace key cannot go into an HTTP header: it is empty, or it holds a line "
            "break, a control character or a character outside Latin-1. Nothing was sent."
        )
    return key


def ask_url(web_url: str) -> str:
    """``<WebUrl>/ask`` — with exactly one slash, whatever the output ends in."""
    return web_url.rstrip("/") + ASK_PATH


def build_request(
    url: str, key: str, question: str, workspace: str, language: str | None = None
) -> urllib.request.Request:
    """The POST the way in expects: JSON, the key header, and the body's hash. The language
    goes in only when given; without it Svar answers in English."""
    fields = {"question": question, "workspace": workspace}
    if language:
        fields["language"] = language
    body = json.dumps(fields).encode("utf-8")
    return urllib.request.Request(
        url,
        data=body,
        headers={
            KEY_HEADER: key,
            "Content-Type": "application/json",
            BODY_HASH_HEADER: hashlib.sha256(body).hexdigest(),
        },
        method="POST",
    )


def json_body(raw: str) -> dict[str, Any]:
    """A response body as a JSON object, or an ``error`` saying what came instead."""
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": f"The response is not JSON: {raw.strip()[:200] or '(empty)'}"}
    if not isinstance(body, dict):
        return {"error": f"The response is not a JSON object: {raw.strip()[:200]}"}
    return body


def send(request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
    """Send the request; return the HTTP status and the JSON body, for errors too."""
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, json_body(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        return error.code, json_body(raw) if raw.strip() else {"error": str(error.reason)}
    except (urllib.error.URLError, TimeoutError) as error:
        raise AwsProblem(f"The web address could not be reached: {error}") from error
    except (http.client.HTTPException, OSError) as error:
        # A connection that breaks while the answer is awaited: urllib wraps only the errors
        # of sending the request, not those of reading the response.
        raise AwsProblem(f"The connection broke off before the answer came: {error}") from error


def format_answer(workspace: str, body: dict[str, Any]) -> list[str]:
    """The answer, the cited layers, one line per source, then model, tokens and latency."""
    lines = [f"Answer ({workspace}):", "", body["answer"], ""]
    lines.append(f"Layers cited: {', '.join(body['layers']) or 'none'}")
    if body["sources"]:
        lines.append("Sources:")
    else:
        lines.append("Sources: none")
    for source in body["sources"]:
        lines.append(
            f"  [{source['number']}] {'cited' if source['cited'] else '     '}  "
            f"{source['layer']:<5}  score {source['score']}  {source['words']:>3} words  "
            f"{source['workspace']}/{source['file']}"
        )
    usage = body["usage"]
    lines.append(
        f"{body['model'] or 'no model call'} · {usage['input_tokens']} tokens in, "
        f"{usage['output_tokens']} out · {body['latency_ms']} ms"
    )
    return lines


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ask",
        description="Ask Svar's deployed answer function a question in one workspace.",
    )
    parser.add_argument("question")
    parser.add_argument("--workspace", required=True, help="the workspace to ask in")
    parser.add_argument(
        "--language",
        choices=["de", "en"],
        help="the language Svar's own sentences come in; default: the function's, English",
    )
    parser.add_argument("--json", action="store_true", help="print the raw JSON response")
    parser.add_argument("--stack-name", default=STACK_NAME, help=f"default: {STACK_NAME}")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    parser.add_argument("--url", help=f"the web address; default: stack output {WEB_URL_OUTPUT}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)
    given_key = os.environ.get(API_KEY_ENVIRONMENT_VARIABLE, "").strip()
    # The key path is only needed when the key has to come from Parameter Store.
    wanted: dict[str, str | None] = {WEB_URL_OUTPUT: args.url}
    if not given_key:
        wanted["ApiKeyParameterPath"] = None
    try:
        settings = resolve(
            wanted,
            lambda: read_stack_outputs(client("cloudformation", args.region), args.stack_name),
        )
        key = header_value(
            given_key
            or read_key(client("ssm", args.region), settings["ApiKeyParameterPath"], args.workspace)
        )
        url = ask_url(settings[WEB_URL_OUTPUT])
        status, body = send(build_request(url, key, args.question, args.workspace, args.language))
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(body, ensure_ascii=False, indent=2))
    if status != 200 or "answer" not in body:
        print(f"error: HTTP {status}: {body.get('error', body)}", file=sys.stderr)
        return 1
    if not args.json:
        for line in format_answer(args.workspace, body):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

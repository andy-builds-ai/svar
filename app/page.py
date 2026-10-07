"""The page, where the function is its own address: no CloudFront in front of it.

In a region whose stack has no CloudFront (`behind_cloudfront` in infra/answer_models.py), the
function's URL is the one address of the page and of `ASK_PATH`. The page's files come with the
function as a layer, in the folder `PAGE_FOLDER_VARIABLE` names; the function hands them out
itself and sets on every response the headers CloudFront's response headers policy sets
elsewhere. The stack builds that policy from the constants here, so the two cannot drift.
"""

import functools
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The path the page posts its questions to.
ASK_PATH = "/ask"

#: The file the bare address serves.
ROOT_DOCUMENT = "index.html"

#: The environment variable that names the folder of the page's files. Set only where the
#: function is the address itself; there, the function serves the page.
PAGE_FOLDER_VARIABLE = "PAGE_FOLDER"

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)

HSTS_MAX_AGE_DAYS = 730

#: What every response carries, page and answer alike: the same values CloudFront's policy sets.
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Strict-Transport-Security": f"max-age={HSTS_MAX_AGE_DAYS * 24 * 60 * 60}; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

#: The kinds of file the page is made of; no other file of the folder is ever served.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

#: A file name the page may ask for: one name, no folder, nothing to climb out with.
_FILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\.[a-z]+")


@functools.cache
def page_files(folder: str) -> dict[str, tuple[str, str]]:
    """Every file of the page in ``folder``, by name: its content type and its text. Read once
    per cold start; a file of another kind, or in a subfolder, is not part of the page."""
    files = {}
    for path in sorted(Path(folder).iterdir()):
        content_type = CONTENT_TYPES.get(path.suffix)
        if path.is_file() and content_type and _FILE_NAME.fullmatch(path.name):
            files[path.name] = (content_type, path.read_text(encoding="utf-8"))
    return files


def plain(status: int, text: str, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A short plain-text response, for a path or method the page does not have."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/plain; charset=utf-8", **(headers or {})},
        "body": text,
    }


def route(event: Mapping[str, Any], folder: str) -> dict[str, Any] | None:
    """The response to a request for the page, or None for one to `ASK_PATH`, which the
    answer function handles as before. GET and HEAD reach the page's files; any other path
    is not found, and any other method on a file is not allowed."""
    path = event.get("rawPath") or "/"
    if path == ASK_PATH:
        return None
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    name = ROOT_DOCUMENT if path == "/" else path.removeprefix("/")
    files = page_files(folder)
    if name not in files:
        return plain(404, "Not found.")
    if method not in ("GET", "HEAD"):
        return plain(
            405,
            f"Read the page with GET, not {method or 'an unknown method'}.",
            {"Allow": "GET, HEAD"},
        )
    content_type, text = files[name]
    return {
        "statusCode": 200,
        # A deploy changes the page: the browser asks again each time instead of keeping an
        # old copy, and gets the same files the function was deployed with.
        "headers": {"Content-Type": content_type, "Cache-Control": "no-cache"},
        "body": "" if method == "HEAD" else text,
    }


def with_security_headers(response: dict[str, Any]) -> dict[str, Any]:
    """``response`` with `SECURITY_HEADERS` added; they win over any header of the same name."""
    return {**response, "headers": {**response.get("headers", {}), **SECURITY_HEADERS}}

"""The workspace rules: what a workspace may be called, its layers, the header its key
travels in, and how a search stays inside one workspace."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

WORKSPACE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

MAX_WORKSPACE_CHARACTERS = 64

#: The three layers a document can come from, in the order they are trusted.
LAYERS = ("human", "agent", "inbox")

KEY_HEADER = "X-Andur-Key"


def workspace_filter(workspace: str) -> dict[str, Any]:
    """The retrieval filter that keeps a query inside one workspace."""
    return {"equals": {"key": "workspace", "value": workspace}}


def keep_workspace(
    results: Sequence[Mapping[str, Any]], workspace: str
) -> tuple[list[Mapping[str, Any]], int]:
    """Drop every passage whose workspace is not the requested one; return what is left and
    how many went."""
    kept = [
        result for result in results if result.get("metadata", {}).get("workspace") == workspace
    ]
    return kept, len(results) - len(kept)

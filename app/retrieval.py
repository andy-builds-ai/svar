"""Searching the knowledge base inside one workspace, and the passages as the response lists
them."""

from collections.abc import Mapping, Sequence
from typing import Any

import aws
import errors
import log

#: How much of each passage the response carries as its excerpt.
EXCERPT_CHARACTERS = 300


def workspace_filter(workspace: str) -> dict[str, Any]:
    """The retrieval filter that keeps a query inside one workspace."""
    return {"equals": {"key": "workspace", "value": workspace}}


def retrieve_passages(
    agent_runtime: Any,
    knowledge_base_id: str,
    question: str,
    workspace: str,
    number_of_results: int,
) -> list[dict[str, Any]]:
    """Ask the knowledge base for the passages that best match the question, in one workspace."""
    try:
        response = agent_runtime.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": question},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": number_of_results,
                    "filter": workspace_filter(workspace),
                }
            },
        )
    except aws.botocore_errors() as exc:
        log.log_event("retrieve-failed", workspace=workspace, detail=aws.error_detail(exc))
        cause = aws.error_cause(exc)
        raise errors.RetrievalError(
            f"The knowledge base could not be searched: {cause}.",
            f"Die Wissensbasis konnte nicht durchsucht werden: {cause}.",
        ) from exc
    return response.get("retrievalResults", [])


def keep_workspace(
    results: Sequence[Mapping[str, Any]], workspace: str
) -> tuple[list[Mapping[str, Any]], int]:
    """Drop every passage whose workspace is not the requested one; return what is left and
    how many went."""
    kept = [
        result for result in results if result.get("metadata", {}).get("workspace") == workspace
    ]
    return kept, len(results) - len(kept)


def passage_text(result: Mapping[str, Any]) -> str:
    """The text of a retrieved passage, line breaks kept — a Markdown table needs them."""
    return result.get("content", {}).get("text", "").strip()


def excerpt(text: str) -> str:
    """The start of a passage on one line, cut at a word boundary."""
    flat = " ".join(text.split())
    if len(flat) <= EXCERPT_CHARACTERS:
        return flat
    return flat[:EXCERPT_CHARACTERS].rsplit(" ", 1)[0] + " ..."


def file_name(result: Mapping[str, Any]) -> str:
    """The file a passage comes from, without the bucket path."""
    uri = result.get("location", {}).get("s3Location", {}).get("uri", "")
    return uri.rsplit("/", 1)[-1] or "unknown"


def to_source(number: int, result: Mapping[str, Any]) -> dict[str, Any]:
    """One passage as the response lists it."""
    metadata = result.get("metadata", {})
    text = passage_text(result)
    score = result.get("score")
    return {
        "number": number,
        "file": file_name(result),
        "layer": metadata.get("layer", "unknown"),
        "workspace": metadata.get("workspace", "unknown"),
        "score": round(score, 4) if isinstance(score, (int, float)) else None,
        "words": len(text.split()),
        "cited": False,
        "excerpt": excerpt(text),
    }

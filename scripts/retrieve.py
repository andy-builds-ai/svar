"""Ask the knowledge base a question inside one workspace and print the hits."""

import argparse
import sys
from typing import Any

from scripts import utf8_output
from scripts.aws import (
    REGION,
    STACK_NAME,
    AwsProblem,
    client,
    explained,
    read_stack_outputs,
    resolve,
)
from scripts.layout import LAYERS

EXCERPT_CHARACTERS = 160


def build_filter(workspace: str, layer: str | None = None) -> dict[str, Any]:
    """The Bedrock retrieval filter: always the workspace, optionally the layer."""
    by_workspace = {"equals": {"key": "workspace", "value": workspace}}
    if layer is None:
        return by_workspace
    return {"andAll": [by_workspace, {"equals": {"key": "layer", "value": layer}}]}


def format_hit(rank: int, result: dict[str, Any]) -> list[str]:
    """Two lines per hit: score, workspace, layer, source; then a text excerpt."""
    metadata = result.get("metadata", {})
    uri = result.get("location", {}).get("s3Location", {}).get("uri", "?")
    text = " ".join(result.get("content", {}).get("text", "").split())
    if len(text) > EXCERPT_CHARACTERS:
        text = text[:EXCERPT_CHARACTERS].rstrip() + " ..."
    score = result.get("score")
    score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "?"
    return [
        f"{rank}. score {score_text}  workspace={metadata.get('workspace', '?')}  "
        f"layer={metadata.get('layer', '?')}  {uri.rsplit('/', 1)[-1]}",
        f"   {text}",
    ]


def retrieve(
    agent_runtime: Any,
    knowledge_base_id: str,
    question: str,
    search_filter: dict[str, Any],
    results: int,
) -> list[dict[str, Any]]:
    with explained("Retrieving from the knowledge base"):
        response = agent_runtime.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": question},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": results, "filter": search_filter}
            },
        )
    return response.get("retrievalResults", [])


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.retrieve",
        description="Retrieve chunks for a question from one Andur workspace.",
    )
    parser.add_argument("question")
    parser.add_argument("--workspace", required=True, help="the workspace to search in")
    parser.add_argument("--layer", choices=LAYERS, help="only chunks from this layer")
    parser.add_argument("--results", type=int, default=5, help="number of hits (default: 5)")
    parser.add_argument("--stack-name", default=STACK_NAME, help=f"default: {STACK_NAME}")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    parser.add_argument("--knowledge-base-id", help="default: stack output KnowledgeBaseId")
    args = parser.parse_args(argv)
    if not 1 <= args.results <= 100:
        parser.error("--results must be between 1 and 100")
    return args


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)
    search_filter = build_filter(args.workspace, args.layer)
    try:
        settings = resolve(
            {"KnowledgeBaseId": args.knowledge_base_id},
            lambda: read_stack_outputs(client("cloudformation", args.region), args.stack_name),
        )
        hits = retrieve(
            client("bedrock-agent-runtime", args.region),
            settings["KnowledgeBaseId"],
            args.question,
            search_filter,
            args.results,
        )
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    scope = f"workspace={args.workspace}" + (f", layer={args.layer}" if args.layer else "")
    print(f"{len(hits)} hit(s) for {args.question!r} ({scope}):")
    for rank, hit in enumerate(hits, start=1):
        for line in format_hit(rank, hit):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

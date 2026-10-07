"""Fill the knowledge base from a local folder."""

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
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
from scripts.layout import (
    BUCKET_PREFIX,
    CONTENT_TYPES,
    IGNORED_TOP_LEVEL_FILES,
    LAYERS,
    MAX_WORKSPACE_CHARACTERS,
    REQUIRED_KEYS,
    SIDECAR_SUFFIX,
    TEXT_SUFFIXES,
    WORKSPACE_NAME,
    Document,
    count_sections,
    detect_encoding,
    format_conversions,
    upload_items,
)

# --- Step 1: check the local folder --------------------------------------


def check_sidecar(path: Path, workspace: str, layer: str) -> list[str]:
    """Problems with one sidecar file; an empty list means it is valid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{path}: not readable as JSON ({exc})"]

    attributes = data.get("metadataAttributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        return [f"{path}: top-level key metadataAttributes is missing or not an object"]

    problems = []
    missing = [key for key in REQUIRED_KEYS if key not in attributes]
    extra = sorted(set(attributes) - set(REQUIRED_KEYS))
    if missing:
        problems.append(f"{path}: missing keys {', '.join(missing)}")
    if extra:
        # Every extra key would cost filterable-metadata budget in S3 Vectors.
        problems.append(f"{path}: unexpected keys {', '.join(extra)}")

    values: dict[str, str] = {}
    for key in REQUIRED_KEYS:
        attribute = attributes.get(key)
        if attribute is None:
            continue
        value = attribute.get("value") if isinstance(attribute, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("type") != "STRING"
            or not isinstance(value.get("stringValue"), str)
            or not value["stringValue"]
        ):
            problems.append(f"{path}: {key} must be a non-empty STRING value")
            continue
        if attribute.get("includeForEmbedding") is not False:
            problems.append(f"{path}: {key} must have includeForEmbedding: false")
        values[key] = value["stringValue"]

    if "workspace" in values and values["workspace"] != workspace:
        problems.append(
            f"{path}: workspace is {values['workspace']!r}, but the folder is {workspace!r}"
        )
    if "layer" in values and values["layer"] != layer:
        problems.append(f"{path}: layer is {values['layer']!r}, but the folder is {layer!r}")
    return problems


def _visible(entries: Iterable[Path]) -> list[Path]:
    """Directory entries without hidden files such as .DS_Store, sorted."""
    return sorted(entry for entry in entries if not entry.name.startswith("."))


def _check_layer_folder(
    folder: Path, workspace: str, layer: str
) -> tuple[list[Document], list[str]]:
    documents: list[Document] = []
    problems: list[str] = []
    entries = _visible(folder.iterdir())
    names = {entry.name for entry in entries}

    for entry in entries:
        if entry.is_dir():
            problems.append(f"{entry}: subfolders inside a layer are not allowed")
        elif entry.name.endswith(SIDECAR_SUFFIX):
            if entry.name.removesuffix(SIDECAR_SUFFIX) not in names:
                problems.append(f"{entry}: sidecar without a document next to it")
        elif entry.suffix.lower() not in CONTENT_TYPES:
            problems.append(
                f"{entry}: file type {entry.suffix or '(none)'} cannot be read by the "
                "knowledge base"
            )
        else:
            document = Document(entry, workspace, layer)
            document_problems = []
            if entry.suffix.lower() in TEXT_SUFFIXES:
                try:
                    document = Document(
                        entry, workspace, layer, detect_encoding(entry.read_bytes())
                    )
                except (OSError, ValueError) as exc:
                    document_problems.append(f"{entry}: {exc}")
            if not document.sidecar.is_file():
                document_problems.append(f"{entry}: sidecar {document.sidecar.name} is missing")
            else:
                document_problems.extend(check_sidecar(document.sidecar, workspace, layer))
            problems.extend(document_problems)
            if not document_problems:
                documents.append(document)
    return documents, problems


def discover(source: Path, only_workspace: str | None = None) -> tuple[list[Document], list[str]]:
    """Every valid document under ``source``, and every problem found on the way."""
    if not source.is_dir():
        return [], [f"{source}: folder not found"]

    documents: list[Document] = []
    problems: list[str] = []
    seen_workspaces: list[str] = []

    for entry in _visible(source.iterdir()):
        if entry.is_file():
            if entry.name not in IGNORED_TOP_LEVEL_FILES:
                problems.append(f"{entry}: files must sit in <workspace>/<layer>/")
            continue
        workspace = entry.name
        if only_workspace is not None and workspace != only_workspace:
            continue
        seen_workspaces.append(workspace)
        if len(workspace) > MAX_WORKSPACE_CHARACTERS:
            problems.append(
                f"{entry}: workspace name is longer than {MAX_WORKSPACE_CHARACTERS} characters"
            )
            continue
        if not WORKSPACE_NAME.match(workspace):
            problems.append(
                f"{entry}: workspace name must be lowercase letters, digits and single hyphens"
            )
            continue
        for layer_entry in _visible(entry.iterdir()):
            if layer_entry.is_file():
                problems.append(
                    f"{layer_entry}: files must sit in a layer folder ({', '.join(LAYERS)})"
                )
            elif layer_entry.name not in LAYERS:
                problems.append(
                    f"{layer_entry}: unknown layer {layer_entry.name!r}, expected one of "
                    f"{', '.join(LAYERS)}"
                )
            else:
                found, layer_problems = _check_layer_folder(
                    layer_entry, workspace, layer_entry.name
                )
                documents.extend(found)
                problems.extend(layer_problems)

    if only_workspace is not None and not seen_workspaces:
        problems.append(f"{source}: no workspace folder named {only_workspace!r}")
    elif not documents and not problems:
        problems.append(f"{source}: no documents found")
    return documents, problems


def count_documents(documents: Iterable[Document]) -> Counter[tuple[str, str]]:
    """Number of documents per (workspace, layer)."""
    return Counter((document.workspace, document.layer) for document in documents)


def format_counts(
    documents: Counter[tuple[str, str]], sections: Counter[tuple[str, str]]
) -> list[str]:
    """One line per workspace: documents and sections, in total and per layer."""
    lines = []
    for workspace in sorted({workspace for workspace, _ in documents}):
        per_layer = ", ".join(
            f"{layer} {documents[(workspace, layer)]} -> {sections[(workspace, layer)]}"
            for layer in LAYERS
        )
        total_documents = sum(documents[(workspace, layer)] for layer in LAYERS)
        total_sections = sum(sections[(workspace, layer)] for layer in LAYERS)
        lines.append(
            f"  {workspace}: {total_documents} documents -> {total_sections} sections ({per_layer})"
        )
    return lines


# --- Step 2: upload ------------------------------------------------------


@dataclass
class UploadCounts:
    uploaded: int = 0
    unchanged: int = 0


def _remote_md5(s3: Any, bucket: str, key: str) -> str | None:
    """The MD5 of the object at ``key``, or None if there is no object."""
    try:
        with explained(f"Checking s3://{bucket}/{key}"):
            response = s3.head_object(Bucket=bucket, Key=key)
    except AwsProblem as problem:
        if error_code(problem.__cause__) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return response["ETag"].strip('"')


def upload(
    s3: Any, bucket: str, documents: Iterable[Document], log: Callable[[str], None] = print
) -> UploadCounts:
    """Put every section or document and its sidecar into the bucket, skipping unchanged ones."""
    counts = UploadCounts()
    for document in documents:
        for item in upload_items(document):
            if _remote_md5(s3, bucket, item.key) == hashlib.md5(item.body).hexdigest():
                counts.unchanged += 1
                continue
            with explained(f"Uploading {document.path} to s3://{bucket}/{item.key}"):
                s3.put_object(
                    Bucket=bucket, Key=item.key, Body=item.body, ContentType=item.content_type
                )
            counts.uploaded += 1
            log(f"  uploaded {item.key}")
    return counts


# --- Step 3: prune -------------------------------------------------------

#: DeleteObjects takes at most this many keys per call.
DELETE_BATCH = 1000


def remote_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    """Every object key under ``prefix``, sorted."""
    keys: list[str] = []
    with explained(f"Listing s3://{bucket}/{prefix}"):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
    return sorted(keys)


def compared_workspaces(
    documents: Iterable[Document], only_workspace: str | None = None
) -> list[str]:
    """The workspaces a prune compares: the one ``--workspace`` names, or else every workspace
    with at least one document in the folder. A workspace folder with no document left in it
    is not compared."""
    if only_workspace:
        return [only_workspace]
    return sorted({document.workspace for document in documents})


def stale_keys(
    s3: Any, bucket: str, documents: Iterable[Document], only_workspace: str | None = None
) -> list[str]:
    """Objects that no local document produces any more, inside `compared_workspaces`.

    A workspace that is not compared is never touched, so a folder with one customer in it
    cannot take every other customer's documents for stale — nor delete them with
    ``--delete``.
    """
    documents = list(documents)
    workspaces = compared_workspaces(documents, only_workspace)
    expected = {item.key for document in documents for item in upload_items(document)}
    return sorted(
        key
        for workspace in workspaces
        for key in remote_keys(s3, bucket, f"{BUCKET_PREFIX}{workspace}/")
        if key not in expected
    )


def delete_keys(s3: Any, bucket: str, keys: list[str]) -> None:
    """Delete ``keys`` from the bucket; any key S3 refuses is an `AwsProblem`."""
    for start in range(0, len(keys), DELETE_BATCH):
        batch = keys[start : start + DELETE_BATCH]
        with explained(f"Deleting {len(batch)} object(s) from s3://{bucket}"):
            response = s3.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True}
            )
        errors = response.get("Errors", [])
        if errors:
            first = errors[0]
            raise AwsProblem(
                f"{len(errors)} object(s) could not be deleted from s3://{bucket}, "
                f"the first {first.get('Key')} ({first.get('Code')})."
            )


def format_stale(keys: list[str], deleted: bool, workspaces: list[str]) -> list[str]:
    """The objects that are no longer in the folder, and what happened to them.

    The all-clear names the workspaces it compared: the bucket may hold others that were not
    looked at.
    """
    if not keys:
        return [
            "No object in the bucket is missing from the folder "
            f"(compared: {', '.join(workspaces)})."
        ]
    if deleted:
        head = f"Deleted {len(keys)} object(s) that are no longer in the folder:"
    else:
        head = (
            f"{len(keys)} object(s) in the bucket are no longer in the folder "
            "and would be deleted with --delete:"
        )
    return [head] + [f"  {key}" for key in keys]


# --- Step 4: sync --------------------------------------------------------

FINAL_JOB_STATUSES = {"COMPLETE", "FAILED", "STOPPED"}


def run_ingestion_job(
    agent: Any,
    knowledge_base_id: str,
    data_source_id: str,
    poll_seconds: float = 5,
    timeout_seconds: float = 1800,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Start an ingestion job and return it once it has reached a final status."""
    ids = {"knowledgeBaseId": knowledge_base_id, "dataSourceId": data_source_id}
    try:
        with explained("Starting the ingestion job"):
            job = agent.start_ingestion_job(**ids)["ingestionJob"]
    except AwsProblem as problem:
        if error_code(problem.__cause__) == "ConflictException":
            raise AwsProblem(
                "Another ingestion job is still running on this data source. "
                "Wait for it to finish, then rerun."
            ) from problem.__cause__
        raise

    job_id = job["ingestionJobId"]
    log(f"Ingestion job {job_id} started.")
    waited = 0.0
    last_status = None
    while job["status"] not in FINAL_JOB_STATUSES:
        if job["status"] != last_status:
            log(f"  status {job['status']}")
            last_status = job["status"]
        if waited >= timeout_seconds:
            raise AwsProblem(
                f"Ingestion job {job_id} is still {job['status']} after {int(waited)} seconds. "
                "It keeps running in AWS; check it in the Bedrock console."
            )
        sleep(poll_seconds)
        waited += poll_seconds
        with explained(f"Reading the status of ingestion job {job_id}"):
            job = agent.get_ingestion_job(**ids, ingestionJobId=job_id)["ingestionJob"]
    return job


#: The job statistics worth printing, with the label they are printed under.
JOB_STATISTICS = (
    ("numberOfDocumentsScanned", "documents scanned"),
    ("numberOfMetadataDocumentsScanned", "metadata files scanned"),
    ("numberOfNewDocumentsIndexed", "new documents indexed"),
    ("numberOfModifiedDocumentsIndexed", "modified documents indexed"),
    ("numberOfMetadataDocumentsModified", "metadata files modified"),
    ("numberOfDocumentsDeleted", "documents deleted"),
    ("numberOfDocumentsFailed", "documents failed"),
)


def job_succeeded(job: dict[str, Any]) -> bool:
    return (
        job["status"] == "COMPLETE"
        and job.get("statistics", {}).get("numberOfDocumentsFailed", 0) == 0
    )


def format_job(job: dict[str, Any]) -> list[str]:
    statistics = job.get("statistics", {})
    lines = [f"Ingestion job {job['ingestionJobId']}: {job['status']}"]
    lines += [f"  {label}: {statistics.get(key, 0)}" for key, label in JOB_STATISTICS]
    lines += [f"  failure: {reason}" for reason in job.get("failureReasons", [])]
    return lines


# --- Command line --------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ingest",
        description="Upload a local knowledge folder to Andur and sync the knowledge base.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("knowledge"),
        help="folder laid out as <workspace>/<layer>/<file> (default: knowledge)",
    )
    parser.add_argument(
        "--workspace", help="upload only this workspace (the sync still covers all)"
    )
    parser.add_argument("--check", action="store_true", help="only check the folder; no AWS calls")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete objects the folder no longer has; without it they are only listed",
    )
    parser.add_argument("--stack-name", default=STACK_NAME, help=f"default: {STACK_NAME}")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    parser.add_argument(
        "--bucket", help="documents bucket; default: stack output DocumentsBucketName"
    )
    parser.add_argument("--knowledge-base-id", help="default: stack output KnowledgeBaseId")
    parser.add_argument("--data-source-id", help="default: stack output DataSourceId")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)

    documents, problems = discover(args.source, args.workspace)
    if problems:
        for problem in problems:
            print(f"problem: {problem}", file=sys.stderr)
        print(f"{len(problems)} problem(s) found. Nothing was uploaded.", file=sys.stderr)
        return 1

    print(f"{len(documents)} documents checked in {args.source}:")
    for line in format_counts(count_documents(documents), count_sections(documents)):
        print(line)
    for line in format_conversions(documents):
        print(line)
    if args.check:
        return 0

    try:
        settings = resolve(
            {
                "DocumentsBucketName": args.bucket,
                "KnowledgeBaseId": args.knowledge_base_id,
                "DataSourceId": args.data_source_id,
            },
            lambda: read_stack_outputs(client("cloudformation", args.region), args.stack_name),
        )
        bucket = settings["DocumentsBucketName"]
        s3 = client("s3", args.region)
        counts = upload(s3, bucket, documents)
        print(f"Files uploaded: {counts.uploaded}, unchanged and skipped: {counts.unchanged}")
        stale = stale_keys(s3, bucket, documents, args.workspace)
        if args.delete and stale:
            delete_keys(s3, bucket, stale)
        workspaces = compared_workspaces(documents, args.workspace)
        for line in format_stale(stale, deleted=args.delete, workspaces=workspaces):
            print(line)
        job = run_ingestion_job(
            client("bedrock-agent", args.region),
            settings["KnowledgeBaseId"],
            settings["DataSourceId"],
        )
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    for line in format_job(job):
        print(line)
    return 0 if job_succeeded(job) else 3


if __name__ == "__main__":
    sys.exit(main())

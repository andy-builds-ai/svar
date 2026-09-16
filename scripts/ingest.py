"""Fill the knowledge base from a local folder.

    python -m scripts.ingest                      # all of knowledge/
    python -m scripts.ingest --workspace pellwick # one workspace
    python -m scripts.ingest --check              # validate only, no AWS

The folder is laid out the way the documents bucket expects::

    <source>/<workspace>/<layer>/<document>
    <source>/<workspace>/<layer>/<document>.metadata.json

Three steps, each stopping the run if it fails:

1. **Check** every document and its sidecar locally (docs/metadata.md). One
   bad file stops the run before anything is uploaded, so the bucket never
   holds half a corpus.
2. **Upload** each document and sidecar to
   ``s3://<bucket>/workspaces/<workspace>/<layer>/``. A file whose content is
   already in the bucket is skipped, so a rerun creates no new object
   versions and nothing twice.
3. **Sync**: start an ingestion job and wait until it is finished.

Exit codes: 0 done, 1 the local folder has problems, 2 an AWS step failed,
3 the ingestion job failed or could not index every document.
"""

import argparse
import hashlib
import json
import re
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

# --- The layout contract (docs/metadata.md) ------------------------------

#: Same value as ``WORKSPACE_PREFIX`` in the stack; a test keeps them equal.
#: Not imported from there, because that would pull in the CDK.
BUCKET_PREFIX = "workspaces/"

LAYERS = ("human", "agent", "inbox")
SIDECAR_SUFFIX = ".metadata.json"
REQUIRED_KEYS = ("workspace", "layer", "source", "license")

#: Lowercase, no spaces: letters and digits, single hyphens in between.
WORKSPACE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: File types a Bedrock knowledge base can read, with the content type the
#: upload sets. Anything else would only fail later, inside the sync.
CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
SIDECAR_CONTENT_TYPE = "application/json"

#: Top-level files in the source folder that describe it and are not uploaded.
IGNORED_TOP_LEVEL_FILES = {"README.md"}


def object_key(workspace: str, layer: str, filename: str) -> str:
    """Where a local file lands in the documents bucket."""
    return f"{BUCKET_PREFIX}{workspace}/{layer}/{filename}"


@dataclass(frozen=True)
class Document:
    """One document and its sidecar, both checked."""

    path: Path
    workspace: str
    layer: str

    @property
    def sidecar(self) -> Path:
        return self.path.with_name(self.path.name + SIDECAR_SUFFIX)

    @property
    def key(self) -> str:
        return object_key(self.workspace, self.layer, self.path.name)

    @property
    def sidecar_key(self) -> str:
        return self.key + SIDECAR_SUFFIX


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


def _check_layer_folder(folder: Path, workspace: str, layer: str) -> tuple[list[Document], list[str]]:
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
            if not document.sidecar.is_file():
                problems.append(f"{entry}: sidecar {document.sidecar.name} is missing")
                continue
            sidecar_problems = check_sidecar(document.sidecar, workspace, layer)
            problems.extend(sidecar_problems)
            if not sidecar_problems:
                documents.append(document)
    return documents, problems


def discover(source: Path, only_workspace: str | None = None) -> tuple[list[Document], list[str]]:
    """Every valid document under ``source``, and every problem found on the way.

    The caller uploads nothing unless the problem list is empty.
    """
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
        if not WORKSPACE_NAME.match(workspace):
            problems.append(
                f"{entry}: workspace name must be lowercase letters, digits and single hyphens"
            )
            continue
        for layer_entry in _visible(entry.iterdir()):
            if layer_entry.is_file():
                problems.append(f"{layer_entry}: files must sit in a layer folder ({', '.join(LAYERS)})")
            elif layer_entry.name not in LAYERS:
                problems.append(
                    f"{layer_entry}: unknown layer {layer_entry.name!r}, expected one of "
                    f"{', '.join(LAYERS)}"
                )
            else:
                found, layer_problems = _check_layer_folder(layer_entry, workspace, layer_entry.name)
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


def format_counts(counts: Counter[tuple[str, str]]) -> list[str]:
    """One line per workspace: total and the count per layer, in layer order."""
    lines = []
    for workspace in sorted({workspace for workspace, _ in counts}):
        per_layer = ", ".join(f"{layer} {counts[(workspace, layer)]}" for layer in LAYERS)
        total = sum(counts[(workspace, layer)] for layer in LAYERS)
        lines.append(f"  {workspace}: {total} documents ({per_layer})")
    return lines


# --- Step 2: upload ------------------------------------------------------


@dataclass
class UploadCounts:
    uploaded: int = 0
    unchanged: int = 0


def _remote_md5(s3: Any, bucket: str, key: str) -> str | None:
    """The MD5 of the object at ``key``, or None if there is no object.

    For single-part uploads to a bucket with S3-managed encryption, the ETag
    is the MD5 of the content. That is how every object here is written.
    """
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
    """Put every document and sidecar into the bucket, skipping unchanged ones."""
    counts = UploadCounts()
    for document in documents:
        for local, key, content_type in (
            (document.path, document.key, CONTENT_TYPES[document.path.suffix.lower()]),
            (document.sidecar, document.sidecar_key, SIDECAR_CONTENT_TYPE),
        ):
            body = local.read_bytes()
            if _remote_md5(s3, bucket, key) == hashlib.md5(body).hexdigest():
                counts.unchanged += 1
                continue
            with explained(f"Uploading {local} to s3://{bucket}/{key}"):
                s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
            counts.uploaded += 1
            log(f"  uploaded {key}")
    return counts


# --- Step 3: sync --------------------------------------------------------

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
    """Start an ingestion job and return it once it has reached a final status.

    The job always covers the whole data source, not only what was uploaded.
    """
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
    return job["status"] == "COMPLETE" and job.get("statistics", {}).get("numberOfDocumentsFailed", 0) == 0


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
    parser.add_argument("--source", type=Path, default=Path("knowledge"),
                        help="folder laid out as <workspace>/<layer>/<file> (default: knowledge)")
    parser.add_argument("--workspace", help="upload only this workspace (the sync still covers all)")
    parser.add_argument("--check", action="store_true",
                        help="only check the folder; no AWS calls")
    parser.add_argument("--stack-name", default=STACK_NAME, help=f"default: {STACK_NAME}")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    parser.add_argument("--bucket", help="documents bucket; default: stack output DocumentsBucketName")
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
    for line in format_counts(count_documents(documents)):
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
        counts = upload(client("s3", args.region), bucket, documents)
        print(f"Files uploaded: {counts.uploaded}, unchanged and skipped: {counts.unchanged}")
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

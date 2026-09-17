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
2. **Upload** to ``s3://<bucket>/workspaces/<workspace>/<layer>/``. Markdown
   documents go up split into sections, one file per section with a copy of
   the document's sidecar (see `split_markdown` for why and how); other file
   types go up whole. An object whose content is already in the bucket is
   skipped, so a rerun creates no new object versions and nothing twice.
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
#: ``\Z`` and not ``$``, which would also match in front of a closing newline.
WORKSPACE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

#: A workspace name is at most this long. Same value as in app/handler.py,
#: where the limit keeps an outsized name out of the log; a test keeps the two
#: equal, so a folder that uploads here is a folder the function can be asked
#: about.
MAX_WORKSPACE_CHARACTERS = 64

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


# --- Splitting Markdown into sections ------------------------------------
#
# Bedrock returned whole documents from retrieve for every demo document that
# contains a Markdown table — with fixed-size and with hierarchical chunking,
# whatever the chunk size (measured 17.09.2026, see "Retrieval findings" in
# the README). The cause is not documented, and the foundation-model parser
# does not remove it (measured the same day). Splitting before the upload does
# not depend on it: if Bedrock hands back a whole file, the file is a section.

#: A section shorter than this is joined to its neighbour: a lone heading or a
#: two-line paragraph is not worth a hit of its own.
MIN_SECTION_WORDS = 80

#: A section longer than this is split again at its ``### `` headings, so that
#: a section Bedrock returns whole is still about a page at most.
MAX_SECTION_WORDS = 450

#: Separates the original file stem from the section part of the name:
#: ``faq.md`` becomes ``faq--01-accounts-and-access.md``.
SECTION_SEPARATOR = "--"

_SLUG_MAX_CHARACTERS = 40


def _words(text: str) -> int:
    return len(text.split())


def _split_at(lines: list[str], prefix: str) -> list[list[str]]:
    """Cut lines into blocks, each starting at a heading with ``prefix``.

    Lines inside fenced code blocks are never headings: an API example with a
    ``## `` comment must not split a section.
    """
    blocks: list[list[str]] = [[]]
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith(prefix) and blocks[-1]:
            blocks.append([])
        blocks[-1].append(line)
    return [block for block in blocks if "".join(block).strip()]


def _join_short(blocks: list[list[str]]) -> list[list[str]]:
    """Join blocks under `MIN_SECTION_WORDS` to their predecessor.

    The first block has no predecessor; if it is short, it goes in front of
    the block after it instead.
    """
    joined: list[list[str]] = []
    carried: list[str] = []
    for block in blocks:
        block = carried + block
        carried = []
        if _words("\n".join(block)) >= MIN_SECTION_WORDS:
            joined.append(block)
        elif joined:
            joined[-1] = joined[-1] + block
        else:
            carried = block
    if carried:
        joined.append(carried)
    return joined


def slugify(heading: str) -> str:
    """A short, lowercase file-name part from a heading."""
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lstrip("#").lower()).strip("-")
    if len(slug) > _SLUG_MAX_CHARACTERS:
        slug = slug[:_SLUG_MAX_CHARACTERS].rsplit("-", 1)[0]
    return slug or "section"


def _naming_heading(lines: list[str]) -> str | None:
    """The heading a section is named after.

    The ``## `` or ``### `` heading the block opens with; else the first ``## ``
    in it (a short intro joined in front of a section); else the ``# `` title.
    A ``### `` further down never names the block.
    """
    first = next((line for line in lines if line.strip()), "")
    if first.startswith(("## ", "### ")):
        return first
    for prefix in ("## ", "# "):
        for line in lines:
            if line.startswith(prefix):
                return line
    return None


def _with_context(block: list[str], context: list[str]) -> str:
    """The block's text, preceded by the context lines it does not already contain.

    A context line found anywhere in the block is left out, not only one at
    its start: a short intro joined in front of a section carries the title
    first and the section heading after it, and prepending the heading again
    would put it above the title.
    """
    missing = [line for line in context if line not in block]
    head = "\n\n".join(missing)
    body = "\n".join(block).strip("\n")
    return f"{head}\n\n{body}\n" if head else f"{body}\n"


def split_markdown(text: str) -> list[tuple[str, str]]:
    """Split a Markdown document into ``(slug, text)`` sections.

    1. Cut at every ``## `` heading outside code fences.
    2. Join sections under `MIN_SECTION_WORDS` to the one before (the first
       to the one after).
    3. Cut sections over `MAX_SECTION_WORDS` again at ``### `` and join short
       pieces the same way. A section with no ``### `` stays as it is.
    4. Start every section with the document's ``# `` title — and pieces of
       step 3 also with their ``## `` heading — so a hit read on its own still
       says where it comes from.

    A document without ``## `` headings is one section.
    """
    lines = text.splitlines()
    title = next((line for line in lines if line.startswith("# ")), None)
    title_context = [title] if title else []

    sections: list[tuple[str, str]] = []
    for block in _join_short(_split_at(lines, "## ")):
        pieces = [block]
        parent = next((line for line in block if line.startswith("## ")), None)
        if _words("\n".join(block)) > MAX_SECTION_WORDS:
            pieces = _join_short(_split_at(block, "### "))
        for piece in pieces:
            context = title_context + ([parent] if parent and len(pieces) > 1 else [])
            heading = _naming_heading(piece)
            slug = slugify(heading) if heading else "document"
            sections.append((slug, _with_context(piece, context)))
    return sections


def section_filename(stem: str, number: int, slug: str) -> str:
    return f"{stem}{SECTION_SEPARATOR}{number:02d}-{slug}.md"


@dataclass(frozen=True)
class UploadItem:
    """One object to put into the bucket."""

    key: str
    body: bytes
    content_type: str


def upload_items(document: Document) -> list[UploadItem]:
    """What a document becomes in the bucket: sections or the whole file, each with its sidecar.

    Every section carries an exact copy of the document's sidecar, so its
    workspace, layer, source and licence are the document's.
    """
    sidecar = document.sidecar.read_bytes()
    suffix = document.path.suffix.lower()
    if suffix != ".md":
        key = object_key(document.workspace, document.layer, document.path.name)
        parts = [(key, document.path.read_bytes(), CONTENT_TYPES[suffix])]
    else:
        sections = split_markdown(document.path.read_text(encoding="utf-8"))
        parts = [
            (
                object_key(
                    document.workspace,
                    document.layer,
                    section_filename(document.path.stem, number, slug),
                ),
                text.encode("utf-8"),
                CONTENT_TYPES[".md"],
            )
            for number, (slug, text) in enumerate(sections, start=1)
        ]
    items = []
    for key, body, content_type in parts:
        items.append(UploadItem(key, body, content_type))
        items.append(UploadItem(key + SIDECAR_SUFFIX, sidecar, SIDECAR_CONTENT_TYPE))
    return items


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


def count_sections(documents: Iterable[Document]) -> Counter[tuple[str, str]]:
    """Number of uploaded document files per (workspace, layer), sidecars not counted."""
    counts: Counter[tuple[str, str]] = Counter()
    for document in documents:
        files = [item for item in upload_items(document) if not item.key.endswith(SIDECAR_SUFFIX)]
        counts[(document.workspace, document.layer)] += len(files)
    return counts


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
    for line in format_counts(count_documents(documents), count_sections(documents)):
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

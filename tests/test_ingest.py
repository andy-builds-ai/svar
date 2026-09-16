"""Tests for the fill script's local parts: layout, sidecars, keys, counters.

Nothing here calls AWS or needs boto3. The upload and the job wait are
exercised with small fake clients that record what they were asked.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from scripts import ingest
from scripts.aws import AwsProblem
from scripts.ingest import (
    Document,
    check_sidecar,
    count_documents,
    discover,
    format_counts,
    job_succeeded,
    main,
    object_key,
    run_ingestion_job,
    upload,
)

REPOSITORY = Path(__file__).resolve().parents[1]


def sidecar_for(workspace: str, layer: str, **changes: Any) -> dict[str, Any]:
    """A valid sidecar; keyword arguments replace or (with None) drop an attribute."""
    values = {"workspace": workspace, "layer": layer, "source": "fictional", "license": "CC0-1.0"}
    attributes = {
        key: {"value": {"type": "STRING", "stringValue": value}, "includeForEmbedding": False}
        for key, value in values.items()
    }
    for key, attribute in changes.items():
        if attribute is None:
            attributes.pop(key, None)
        else:
            attributes[key] = attribute
    return {"metadataAttributes": attributes}


def add_document(
    root: Path, workspace: str, layer: str, name: str = "doc.md", sidecar: Any = "valid"
) -> Path:
    folder = root / workspace / layer
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"# {name}\n\nSome text.\n", encoding="utf-8")
    if sidecar == "valid":
        sidecar = sidecar_for(workspace, layer)
    if sidecar is not None:
        (folder / f"{name}.metadata.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return path


# --- Keys ----------------------------------------------------------------


def test_object_key_follows_the_bucket_layout() -> None:
    assert object_key("quillmoor", "agent", "faq.md") == "workspaces/quillmoor/agent/faq.md"


def test_document_keys_put_the_sidecar_next_to_the_document(tmp_path: Path) -> None:
    document = Document(tmp_path / "faq.md", "pellwick", "human")
    assert document.key == "workspaces/pellwick/human/faq.md"
    assert document.sidecar_key == "workspaces/pellwick/human/faq.md.metadata.json"
    assert document.sidecar == tmp_path / "faq.md.metadata.json"


def test_bucket_prefix_matches_the_prefix_the_data_source_crawls() -> None:
    from infra.stacks.andur_stack import WORKSPACE_PREFIX

    assert ingest.BUCKET_PREFIX == WORKSPACE_PREFIX


# --- Sidecar check -------------------------------------------------------


def write_sidecar(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "doc.md.metadata.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_valid_sidecar_has_no_problems(tmp_path: Path) -> None:
    path = write_sidecar(tmp_path, sidecar_for("quillmoor", "human"))
    assert check_sidecar(path, "quillmoor", "human") == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("{not json", "not readable as JSON"),
        ({"workspace": "quillmoor"}, "metadataAttributes is missing"),
        (sidecar_for("quillmoor", "human", license=None), "missing keys license"),
        (
            sidecar_for("quillmoor", "human", team={"value": {"type": "STRING", "stringValue": "x"}}),
            "unexpected keys team",
        ),
        (sidecar_for("other", "human"), "workspace is 'other', but the folder is 'quillmoor'"),
        (sidecar_for("quillmoor", "agent"), "layer is 'agent', but the folder is 'human'"),
        (
            sidecar_for(
                "quillmoor",
                "human",
                source={"value": {"type": "STRING", "stringValue": "fictional"}, "includeForEmbedding": True},
            ),
            "source must have includeForEmbedding: false",
        ),
        (
            sidecar_for(
                "quillmoor",
                "human",
                source={"value": {"type": "NUMBER", "numberValue": 1}, "includeForEmbedding": False},
            ),
            "source must be a non-empty STRING value",
        ),
    ],
    ids=["broken-json", "no-attributes", "missing-key", "extra-key", "wrong-workspace",
         "wrong-layer", "embedded", "not-a-string"],
)
def test_invalid_sidecar_names_the_problem(tmp_path: Path, data: Any, expected: str) -> None:
    problems = check_sidecar(write_sidecar(tmp_path, data), "quillmoor", "human")
    assert any(expected in problem for problem in problems), problems


# --- Folder walk ---------------------------------------------------------


def test_discover_finds_documents_in_all_layers(tmp_path: Path) -> None:
    for layer in ingest.LAYERS:
        add_document(tmp_path, "quillmoor", layer)
    add_document(tmp_path, "pellwick", "human")
    (tmp_path / "README.md").write_text("about this folder", encoding="utf-8")

    documents, problems = discover(tmp_path)

    assert problems == []
    assert sorted(document.key for document in documents) == [
        "workspaces/pellwick/human/doc.md",
        "workspaces/quillmoor/agent/doc.md",
        "workspaces/quillmoor/human/doc.md",
        "workspaces/quillmoor/inbox/doc.md",
    ]


def test_discover_can_limit_to_one_workspace(tmp_path: Path) -> None:
    add_document(tmp_path, "quillmoor", "human")
    add_document(tmp_path, "pellwick", "human")
    documents, problems = discover(tmp_path, only_workspace="pellwick")
    assert problems == []
    assert [document.workspace for document in documents] == ["pellwick"]


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (lambda root: add_document(root, "quillmoor", "human", sidecar=None), "sidecar doc.md.metadata.json is missing"),
        (lambda root: add_document(root, "quillmoor", "drafts"), "unknown layer 'drafts'"),
        (lambda root: add_document(root, "Quill Moor", "human"), "workspace name must be lowercase"),
        (lambda root: add_document(root, "quillmoor", "human", name="scan.png"), "file type .png"),
        (
            lambda root: (root / "quillmoor" / "human").mkdir(parents=True)
            or (root / "quillmoor" / "human" / "gone.md.metadata.json").write_text("{}"),
            "sidecar without a document",
        ),
        (
            lambda root: (root / "quillmoor").mkdir() or (root / "quillmoor" / "loose.md").write_text("x"),
            "files must sit in a layer folder",
        ),
        (lambda root: (root / "stray.md").write_text("x"), "files must sit in <workspace>/<layer>/"),
    ],
    ids=["missing-sidecar", "invalid-layer", "bad-workspace-name", "unsupported-type",
         "orphan-sidecar", "file-in-workspace", "file-at-top"],
)
def test_discover_reports_layout_problems(tmp_path: Path, build: Any, expected: str) -> None:
    build(tmp_path)
    _, problems = discover(tmp_path)
    assert any(expected in problem for problem in problems), problems


def test_discover_reports_a_missing_folder_and_an_unknown_workspace(tmp_path: Path) -> None:
    assert "folder not found" in discover(tmp_path / "nope")[1][0]
    add_document(tmp_path, "quillmoor", "human")
    assert "no workspace folder named 'acme'" in discover(tmp_path, only_workspace="acme")[1][0]


def test_hidden_files_are_ignored(tmp_path: Path) -> None:
    add_document(tmp_path, "quillmoor", "human")
    (tmp_path / "quillmoor" / "human" / ".DS_Store").write_text("x")
    assert discover(tmp_path)[1] == []


# --- Counters ------------------------------------------------------------


def test_counts_per_workspace_and_layer(tmp_path: Path) -> None:
    documents = [
        Document(tmp_path / "a.md", "quillmoor", "human"),
        Document(tmp_path / "b.md", "quillmoor", "human"),
        Document(tmp_path / "c.md", "quillmoor", "inbox"),
        Document(tmp_path / "d.md", "pellwick", "human"),
    ]
    counts = count_documents(documents)
    assert counts == Counter({("quillmoor", "human"): 2, ("quillmoor", "inbox"): 1, ("pellwick", "human"): 1})
    assert format_counts(counts) == [
        "  pellwick: 1 documents (human 1, agent 0, inbox 0)",
        "  quillmoor: 3 documents (human 2, agent 0, inbox 1)",
    ]


def test_the_demo_corpus_passes_the_check() -> None:
    """The committed corpus itself: every sidecar valid, the agreed counts."""
    documents, problems = discover(REPOSITORY / "knowledge")
    assert problems == []
    assert count_documents(documents) == Counter(
        {("quillmoor", "human"): 25, ("quillmoor", "agent"): 5, ("quillmoor", "inbox"): 3, ("pellwick", "human"): 4}
    )


# --- Upload with a fake S3 ----------------------------------------------


class NotFound(Exception):
    """Shaped like botocore's ClientError for a missing object."""

    __module__ = "botocore.exceptions"

    def __init__(self) -> None:
        super().__init__("Not Found")
        self.response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def head_object(self, Bucket: str, Key: str) -> dict[str, str]:
        if Key not in self.objects:
            raise NotFound()
        return {"ETag": f'"{hashlib.md5(self.objects[Key]).hexdigest()}"'}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.objects[Key] = Body
        self.puts.append(Key)


def test_upload_puts_documents_and_sidecars_then_skips_them_on_rerun(tmp_path: Path) -> None:
    add_document(tmp_path, "quillmoor", "human", name="a.md")
    add_document(tmp_path, "quillmoor", "agent", name="b.md")
    documents, _ = discover(tmp_path)
    s3 = FakeS3()

    first = upload(s3, "bucket", documents, log=lambda _: None)
    second = upload(s3, "bucket", documents, log=lambda _: None)

    assert (first.uploaded, first.unchanged) == (4, 0)
    assert (second.uploaded, second.unchanged) == (0, 4)
    assert sorted(s3.objects) == [
        "workspaces/quillmoor/agent/b.md",
        "workspaces/quillmoor/agent/b.md.metadata.json",
        "workspaces/quillmoor/human/a.md",
        "workspaces/quillmoor/human/a.md.metadata.json",
    ]


def test_upload_replaces_a_changed_document_under_the_same_key(tmp_path: Path) -> None:
    path = add_document(tmp_path, "quillmoor", "human", name="a.md")
    documents, _ = discover(tmp_path)
    s3 = FakeS3()
    upload(s3, "bucket", documents, log=lambda _: None)

    path.write_text("changed", encoding="utf-8")
    counts = upload(s3, "bucket", documents, log=lambda _: None)

    assert (counts.uploaded, counts.unchanged) == (1, 1)
    assert s3.objects["workspaces/quillmoor/human/a.md"] == b"changed"
    assert len(s3.objects) == 2


# --- Ingestion job with a fake Bedrock agent -----------------------------


class FakeAgent:
    def __init__(self, statuses: list[str], statistics: dict[str, int] | None = None) -> None:
        self.statuses = statuses
        self.statistics = statistics or {}
        self.polls = 0

    def _job(self, status: str) -> dict[str, Any]:
        return {"ingestionJob": {"ingestionJobId": "job-1", "status": status, "statistics": self.statistics}}

    def start_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str) -> dict[str, Any]:
        return self._job(self.statuses[0])

    def get_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str, ingestionJobId: str) -> dict[str, Any]:
        self.polls += 1
        return self._job(self.statuses[min(self.polls, len(self.statuses) - 1)])


def test_job_is_polled_until_a_final_status() -> None:
    agent = FakeAgent(["STARTING", "IN_PROGRESS", "IN_PROGRESS", "COMPLETE"])
    job = run_ingestion_job(agent, "kb", "ds", sleep=lambda _: None, log=lambda _: None)
    assert job["status"] == "COMPLETE"
    assert agent.polls == 3


def test_job_wait_gives_up_after_the_timeout() -> None:
    agent = FakeAgent(["IN_PROGRESS"])
    with pytest.raises(AwsProblem, match="still IN_PROGRESS"):
        run_ingestion_job(agent, "kb", "ds", poll_seconds=5, timeout_seconds=10,
                          sleep=lambda _: None, log=lambda _: None)


@pytest.mark.parametrize(
    ("status", "failed", "expected"),
    [("COMPLETE", 0, True), ("COMPLETE", 2, False), ("FAILED", 0, False), ("STOPPED", 0, False)],
)
def test_job_success_needs_complete_and_no_failed_documents(status: str, failed: int, expected: bool) -> None:
    job = {"ingestionJobId": "job-1", "status": status, "statistics": {"numberOfDocumentsFailed": failed}}
    assert job_succeeded(job) is expected


# --- Command line without AWS --------------------------------------------


def test_main_check_only_returns_zero_for_a_valid_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_document(tmp_path, "quillmoor", "human")
    assert main(["--source", str(tmp_path), "--check"]) == 0
    assert "quillmoor: 1 documents" in capsys.readouterr().out


def test_main_stops_before_aws_when_the_folder_has_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    add_document(tmp_path, "quillmoor", "human", sidecar=None)

    def no_aws(*_: Any) -> None:
        raise AssertionError("AWS must not be touched")

    monkeypatch.setattr(ingest, "client", no_aws)
    assert main(["--source", str(tmp_path)]) == 1
    error = capsys.readouterr().err
    assert "sidecar doc.md.metadata.json is missing" in error
    assert "Nothing was uploaded." in error
    assert "Traceback" not in error


def test_main_reports_an_aws_problem_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    add_document(tmp_path, "quillmoor", "human")

    def expired(*_: Any) -> None:
        raise AwsProblem("Reading the outputs failed: the AWS login is missing or expired. Sign in again with `aws login`, then rerun.")

    monkeypatch.setattr(ingest, "client", expired)
    assert main(["--source", str(tmp_path)]) == 2
    error = capsys.readouterr().err
    assert "aws login" in error
    assert "Traceback" not in error

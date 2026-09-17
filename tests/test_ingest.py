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
    MAX_SECTION_WORDS,
    MIN_SECTION_WORDS,
    Document,
    check_sidecar,
    count_documents,
    count_sections,
    discover,
    format_counts,
    job_succeeded,
    main,
    object_key,
    run_ingestion_job,
    section_filename,
    slugify,
    split_markdown,
    upload,
    upload_items,
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
    root: Path,
    workspace: str,
    layer: str,
    name: str = "doc.md",
    sidecar: Any = "valid",
    text: str | None = None,
) -> Path:
    folder = root / workspace / layer
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text if text is not None else f"# {name}\n\nSome text.\n", encoding="utf-8")
    if sidecar == "valid":
        sidecar = sidecar_for(workspace, layer)
    if sidecar is not None:
        (folder / f"{name}.metadata.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return path


def words(count: int, word: str = "word") -> str:
    return " ".join([word] * count)


def sectioned_text(parts: int, words_per_part: int = 120) -> str:
    """A document with a title, a short intro and ``parts`` sections headed ``## Part n``."""
    body = "".join(f"\n## Part {n}\n\n{words(words_per_part)}\n" for n in range(1, parts + 1))
    return f"# Guide\n\nShort intro.\n{body}"


# --- Keys ----------------------------------------------------------------


def test_object_key_follows_the_bucket_layout() -> None:
    assert object_key("quillmoor", "agent", "faq.md") == "workspaces/quillmoor/agent/faq.md"


def test_document_sidecar_sits_next_to_the_document(tmp_path: Path) -> None:
    document = Document(tmp_path / "faq.md", "pellwick", "human")
    assert document.sidecar == tmp_path / "faq.md.metadata.json"


def test_section_filename_keeps_the_original_stem() -> None:
    assert section_filename("faq", 3, "billing") == "faq--03-billing.md"


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
    assert sorted((d.workspace, d.layer, d.path.name) for d in documents) == [
        ("pellwick", "human", "doc.md"),
        ("quillmoor", "agent", "doc.md"),
        ("quillmoor", "human", "doc.md"),
        ("quillmoor", "inbox", "doc.md"),
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


# --- Splitting into sections --------------------------------------------


def test_document_without_second_level_headings_is_one_section() -> None:
    text = "# Notes\n\n" + words(300) + "\n\n### A third-level heading\n\n" + words(50) + "\n"
    assert split_markdown(text) == [("notes", text)]


def test_sections_start_at_second_level_headings_and_carry_the_title() -> None:
    sections = split_markdown(sectioned_text(3))

    assert [slug for slug, _ in sections] == ["part-1", "part-2", "part-3"]
    for _, text in sections:
        assert text.startswith("# Guide\n\n")
        assert text.count("# Guide") == 1
    # The short intro had no predecessor and went in front of the first section.
    assert sections[0][1] == "# Guide\n\nShort intro.\n\n## Part 1\n\n" + words(120) + "\n"
    assert sections[1][1] == "# Guide\n\n## Part 2\n\n" + words(120) + "\n"


def test_a_short_section_is_joined_to_its_predecessor() -> None:
    text = (
        "# Guide\n\n" + words(100) + "\n"
        "\n## Long\n\n" + words(120) + "\n"
        "\n## Short\n\n" + words(MIN_SECTION_WORDS - 10) + "\n"
        "\n## Last\n\n" + words(120) + "\n"
    )
    sections = split_markdown(text)

    assert [slug for slug, _ in sections] == ["guide", "long", "last"]
    assert "## Short" in sections[1][1]
    assert "## Short" not in sections[2][1]


def test_headings_inside_code_fences_do_not_split() -> None:
    text = (
        "# API\n\n" + words(100) + "\n"
        "\n## Example\n\n```bash\n## not a heading, a shell comment\ncurl x\n```\n\n"
        + words(120) + "\n"
    )
    sections = split_markdown(text)
    assert [slug for slug, _ in sections] == ["api", "example"]
    assert "## not a heading" in sections[1][1]


def test_an_oversized_section_is_split_again_at_third_level_headings() -> None:
    subsections = "".join(f"\n### Sub {n}\n\n{words(200)}\n" for n in range(1, 4))
    text = "# Manual\n\n" + words(100) + "\n\n## Big\n\nIntro line.\n" + subsections
    assert len(subsections.split()) > MAX_SECTION_WORDS

    sections = split_markdown(text)

    assert [slug for slug, _ in sections] == ["manual", "big", "sub-2", "sub-3"]
    for _, part in sections[1:]:
        assert part.startswith("# Manual\n\n## Big\n\n")
        assert part.count("## Big") == 1
        assert len(part.split()) <= MAX_SECTION_WORDS


def test_a_short_intro_before_an_oversized_section_keeps_the_title_on_top() -> None:
    """The intro goes in front of the first section; the split must not put ``## `` above ``# ``."""
    subsections = "".join(f"\n### Sub {n}\n\n{words(200)}\n" for n in range(1, 4))
    text = "# Draft\n\nShort intro.\n\n## New\n" + subsections

    sections = split_markdown(text)

    assert sections[0][1].startswith("# Draft\n\nShort intro.\n\n## New\n")
    for _, part in sections[1:]:
        assert part.startswith("# Draft\n\n## New\n\n### Sub")


def test_slugify_makes_short_lowercase_names() -> None:
    assert slugify("## 2.7 Exporting a project") == "2-7-exporting-a-project"
    assert slugify("### Why can't I save a booking?") == "why-can-t-i-save-a-booking"
    assert slugify("## ???") == "section"
    assert len(slugify("## " + "longword " * 20)) <= 40


# --- Counters ------------------------------------------------------------


def test_counts_per_workspace_and_layer(tmp_path: Path) -> None:
    add_document(tmp_path, "quillmoor", "human", name="a.md")
    add_document(tmp_path, "quillmoor", "human", name="b.md", text=sectioned_text(3))
    add_document(tmp_path, "quillmoor", "inbox", name="c.md")
    add_document(tmp_path, "pellwick", "human", name="d.md")
    documents, _ = discover(tmp_path)

    counts = count_documents(documents)
    sections = count_sections(documents)

    assert counts == Counter({("quillmoor", "human"): 2, ("quillmoor", "inbox"): 1, ("pellwick", "human"): 1})
    assert sections == Counter({("quillmoor", "human"): 4, ("quillmoor", "inbox"): 1, ("pellwick", "human"): 1})
    assert format_counts(counts, sections) == [
        "  pellwick: 1 documents -> 1 sections (human 1 -> 1, agent 0 -> 0, inbox 0 -> 0)",
        "  quillmoor: 3 documents -> 5 sections (human 2 -> 4, agent 0 -> 0, inbox 1 -> 1)",
    ]


def test_the_demo_corpus_passes_the_check() -> None:
    """The committed corpus itself: every sidecar valid, the agreed counts."""
    documents, problems = discover(REPOSITORY / "knowledge")
    assert problems == []
    assert count_documents(documents) == Counter(
        {("quillmoor", "human"): 25, ("quillmoor", "agent"): 5, ("quillmoor", "inbox"): 3, ("pellwick", "human"): 4}
    )


def test_no_demo_section_with_a_table_is_longer_than_the_maximum() -> None:
    """Bedrock returned table-bearing files whole; such a file must stay section-sized.

    The maximum applies to the section body; the context lines in front add a
    few words.
    """
    documents, _ = discover(REPOSITORY / "knowledge")
    for document in documents:
        for item in upload_items(document):
            if item.key.endswith(".metadata.json"):
                continue
            text = item.body.decode("utf-8")
            if any(line.startswith("|") for line in text.splitlines()):
                assert len(text.split()) <= MAX_SECTION_WORDS + 30, item.key


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
        "workspaces/quillmoor/agent/b--01-b-md.md",
        "workspaces/quillmoor/agent/b--01-b-md.md.metadata.json",
        "workspaces/quillmoor/human/a--01-a-md.md",
        "workspaces/quillmoor/human/a--01-a-md.md.metadata.json",
    ]


def test_every_section_gets_an_exact_copy_of_the_document_sidecar(tmp_path: Path) -> None:
    path = add_document(tmp_path, "quillmoor", "agent", name="guide.md", text=sectioned_text(3))
    documents, _ = discover(tmp_path)
    items = upload_items(documents[0])
    sidecar = (path.parent / "guide.md.metadata.json").read_bytes()

    sections = [item for item in items if not item.key.endswith(".metadata.json")]
    sidecars = {item.key: item.body for item in items if item.key.endswith(".metadata.json")}
    assert [item.key.rsplit("/", 1)[-1] for item in sections] == [
        "guide--01-part-1.md",
        "guide--02-part-2.md",
        "guide--03-part-3.md",
    ]
    assert all(sidecars[item.key + ".metadata.json"] == sidecar for item in sections)


def test_non_markdown_files_are_uploaded_whole(tmp_path: Path) -> None:
    add_document(tmp_path, "pellwick", "human", name="prices.txt", text="## Not split\n" + words(600))
    documents, _ = discover(tmp_path)
    keys = [item.key for item in upload_items(documents[0])]
    assert keys == [
        "workspaces/pellwick/human/prices.txt",
        "workspaces/pellwick/human/prices.txt.metadata.json",
    ]


def test_upload_replaces_a_changed_document_under_the_same_key(tmp_path: Path) -> None:
    path = add_document(tmp_path, "quillmoor", "human", name="a.md")
    documents, _ = discover(tmp_path)
    s3 = FakeS3()
    upload(s3, "bucket", documents, log=lambda _: None)

    path.write_text("# a.md\n\nChanged text.\n", encoding="utf-8")
    counts = upload(s3, "bucket", documents, log=lambda _: None)

    assert (counts.uploaded, counts.unchanged) == (1, 1)
    assert s3.objects["workspaces/quillmoor/human/a--01-a-md.md"] == b"# a.md\n\nChanged text.\n"
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
    add_document(tmp_path, "quillmoor", "agent", name="long.md", text=sectioned_text(2))
    assert main(["--source", str(tmp_path), "--check"]) == 0
    assert "quillmoor: 2 documents -> 3 sections" in capsys.readouterr().out


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

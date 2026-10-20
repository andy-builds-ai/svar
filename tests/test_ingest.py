"""Tests for the fill script's local parts: layout, sidecars, keys, counters.

Nothing here calls AWS or needs boto3. The upload and the job wait are
exercised with small fake clients that record what they were asked.
"""

import codecs
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from scripts import ingest, layout
from scripts.aws import AwsProblem
from scripts.ingest import (
    check_sidecar,
    count_documents,
    delete_keys,
    discover,
    format_counts,
    job_succeeded,
    main,
    run_ingestion_job,
    stale_keys,
    upload,
)
from scripts.layout import (
    MAX_SECTION_WORDS,
    MIN_SECTION_WORDS,
    Document,
    count_sections,
    detect_encoding,
    object_key,
    section_filename,
    slugify,
    split_markdown,
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
    from infra.stacks.knowledge_base import WORKSPACE_PREFIX

    assert layout.BUCKET_PREFIX == WORKSPACE_PREFIX


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
            sidecar_for(
                "quillmoor", "human", team={"value": {"type": "STRING", "stringValue": "x"}}
            ),
            "unexpected keys team",
        ),
        (sidecar_for("other", "human"), "workspace is 'other', but the folder is 'quillmoor'"),
        (sidecar_for("quillmoor", "agent"), "layer is 'agent', but the folder is 'human'"),
        (
            sidecar_for(
                "quillmoor",
                "human",
                source={
                    "value": {"type": "STRING", "stringValue": "fictional"},
                    "includeForEmbedding": True,
                },
            ),
            "source must have includeForEmbedding: false",
        ),
        (
            sidecar_for(
                "quillmoor",
                "human",
                source={
                    "value": {"type": "NUMBER", "numberValue": 1},
                    "includeForEmbedding": False,
                },
            ),
            "source must be a non-empty STRING value",
        ),
    ],
    ids=[
        "broken-json",
        "no-attributes",
        "missing-key",
        "extra-key",
        "wrong-workspace",
        "wrong-layer",
        "embedded",
        "not-a-string",
    ],
)
def test_invalid_sidecar_names_the_problem(tmp_path: Path, data: Any, expected: str) -> None:
    problems = check_sidecar(write_sidecar(tmp_path, data), "quillmoor", "human")
    assert any(expected in problem for problem in problems), problems


# --- Folder walk ---------------------------------------------------------


def test_discover_finds_documents_in_all_layers(tmp_path: Path) -> None:
    for layer in layout.LAYERS:
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
        (
            lambda root: add_document(root, "quillmoor", "human", sidecar=None),
            "sidecar doc.md.metadata.json is missing",
        ),
        (lambda root: add_document(root, "quillmoor", "drafts"), "unknown layer 'drafts'"),
        (
            lambda root: add_document(root, "Quill Moor", "human"),
            "workspace name must be lowercase",
        ),
        (lambda root: add_document(root, "quillmoor", "human", name="scan.png"), "file type .png"),
        (
            lambda root: (
                (root / "quillmoor" / "human").mkdir(parents=True)
                or (root / "quillmoor" / "human" / "gone.md.metadata.json").write_text("{}")
            ),
            "sidecar without a document",
        ),
        (
            lambda root: (
                (root / "quillmoor").mkdir() or (root / "quillmoor" / "loose.md").write_text("x")
            ),
            "files must sit in a layer folder",
        ),
        (
            lambda root: (root / "stray.md").write_text("x"),
            "files must sit in <workspace>/<layer>/",
        ),
    ],
    ids=[
        "missing-sidecar",
        "invalid-layer",
        "bad-workspace-name",
        "unsupported-type",
        "orphan-sidecar",
        "file-in-workspace",
        "file-at-top",
    ],
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
        + words(120)
        + "\n"
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


def test_slugify_spells_out_umlauts_and_sharp_s() -> None:
    assert slugify("## Bürozeiten") == "buerozeiten"
    assert slugify("## Stundensätze") == "stundensaetze"
    assert slugify("## Häufige Fragen") == "haeufige-fragen"
    assert slugify("### Größe und Maße") == "groesse-und-masse"


def test_slugify_spells_out_capital_umlauts_and_capital_sharp_s() -> None:
    assert slugify("## ÄRGER ÜBER ÖL") == "aerger-ueber-oel"
    assert slugify("## Übersicht") == "uebersicht"
    assert slugify("## STRAẞE") == "strasse"


def test_slugify_reads_an_umlaut_stored_as_letter_and_dots() -> None:
    assert slugify("## Bürozeiten") == "buerozeiten"


def test_slugify_reads_wide_and_joined_letters_as_plain_ones() -> None:
    assert slugify("## Ａｎｇｅｂｏｔ ﬁnden") == "angebot-finden"


def test_slugify_takes_the_accent_off_other_letters() -> None:
    assert slugify("## Café, Crème brûlée") == "cafe-creme-brulee"
    assert slugify("## Señor Ångström") == "senor-angstroem"
    assert slugify("## Kraków und Łódź") == "krakow-und-lodz"


def test_slugify_spells_out_letters_that_hold_no_plain_one() -> None:
    assert slugify("## Søren") == "soeren"
    assert slugify("## ØRESUND, Æble, Œuvre") == "oeresund-aeble-oeuvre"
    assert slugify("## Łukasz Đorđević") == "lukasz-dordevic"
    # A letter no step knows is still a hyphen.
    assert slugify("## Þing") == "ing"


def test_slugify_of_nothing_but_special_characters_is_section() -> None:
    assert slugify("## €§ — “”") == "section"
    assert slugify("## 中文") == "section"
    assert slugify("##") == "section"


def test_slugify_keeps_its_length_limit_after_spelling_out() -> None:
    # Two words are 37 letters as written and 43 spelled out, so the second is cut.
    assert slugify("## " + "Überprüfungsgebühr " * 2) == "ueberpruefungsgebuehr"
    assert len(slugify("## " + "Überprüfungsgebühr " * 5)) <= 40


def test_a_german_heading_names_its_section_file(tmp_path: Path) -> None:
    text = f"# Erreichbarkeit\n\n## Bürozeiten\n\n{words(100)}\n\n## Notdienst\n\n{words(100)}\n"
    add_document(tmp_path, "kalvenstett", "human", name="erreichbarkeit.md", text=text)
    documents, _ = discover(tmp_path)
    keys = [item.key for item in upload_items(documents[0]) if item.key.endswith(".md")]
    assert keys == [
        "workspaces/kalvenstett/human/erreichbarkeit--01-buerozeiten.md",
        "workspaces/kalvenstett/human/erreichbarkeit--02-notdienst.md",
    ]


def test_a_name_saved_on_a_mac_gives_the_key_a_windows_name_gives(tmp_path: Path) -> None:
    """A Mac stores ü as u plus dots; the key is the composed ü either way."""
    add_document(tmp_path, "kalvenstett", "human", name="Bürozeiten.md")
    add_document(tmp_path, "kalvenstett", "human", name="Gebühren.txt")
    documents, _ = discover(tmp_path)
    keys = sorted(item.key for document in documents for item in upload_items(document))
    assert keys == [
        "workspaces/kalvenstett/human/Bürozeiten--01-buerozeiten-md.md",
        "workspaces/kalvenstett/human/Bürozeiten--01-buerozeiten-md.md.metadata.json",
        "workspaces/kalvenstett/human/Gebühren.txt",
        "workspaces/kalvenstett/human/Gebühren.txt.metadata.json",
    ]


# --- Headings inside code fences ----------------------------------------


def test_a_heading_in_a_code_fence_does_not_name_the_section() -> None:
    text = "# Setup\n\nInstall it.\n\n```bash\n## comment\npip install haldrum\n```\n\nDone.\n"
    assert split_markdown(text) == [("setup", text)]


def test_a_title_in_a_code_fence_is_no_title() -> None:
    text = (
        f"Intro.\n\n```bash\n# install\npip install haldrum\n```\n\n{words(100)}\n"
        f"\n## Usage\n\n{words(100)}\n"
    )
    sections = split_markdown(text)
    assert [slug for slug, _ in sections] == ["document", "usage"]
    assert sections[1][1].startswith("## Usage\n")
    assert all("# install" not in part.split("```")[0] for _, part in sections[1:])


def test_a_heading_in_a_code_fence_is_no_parent() -> None:
    subsections = "".join(f"\n### Sub {n}\n\n{words(200)}\n" for n in range(1, 4))
    text = "# Guide\n\n```\n## not a heading\n```\n" + subsections

    sections = split_markdown(text)

    assert [slug for slug, _ in sections] == ["guide", "sub-2", "sub-3"]
    for _, part in sections[1:]:
        assert part.startswith("# Guide\n\n### Sub")


def slugify_as_before(heading: str) -> str:
    """`slugify` before umlauts were spelled out: everything but a-z and 0-9 a hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lstrip("#").lower()).strip("-")
    if len(slug) > 40:
        slug = slug[:40].rsplit("-", 1)[0]
    return slug or "section"


def test_english_headings_keep_the_names_they_had() -> None:
    """Every heading of the demo corpus, so no demo section has to be uploaded again."""
    headings = [
        line
        for path in sorted((REPOSITORY / "knowledge").rglob("*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]
    headings += ["## 2.7 Exporting a project", "### Why can't I save a booking?", "## ???"]
    assert len(headings) > 300
    for heading in headings:
        assert slugify(heading) == slugify_as_before(heading), heading


# --- Counters ------------------------------------------------------------


def test_counts_per_workspace_and_layer(tmp_path: Path) -> None:
    add_document(tmp_path, "quillmoor", "human", name="a.md")
    add_document(tmp_path, "quillmoor", "human", name="b.md", text=sectioned_text(3))
    add_document(tmp_path, "quillmoor", "inbox", name="c.md")
    add_document(tmp_path, "pellwick", "human", name="d.md")
    documents, _ = discover(tmp_path)

    counts = count_documents(documents)
    sections = count_sections(documents)

    assert counts == Counter(
        {("quillmoor", "human"): 2, ("quillmoor", "inbox"): 1, ("pellwick", "human"): 1}
    )
    assert sections == Counter(
        {("quillmoor", "human"): 4, ("quillmoor", "inbox"): 1, ("pellwick", "human"): 1}
    )
    assert format_counts(counts, sections) == [
        "  pellwick: 1 documents -> 1 sections (human 1 -> 1, agent 0 -> 0, inbox 0 -> 0)",
        "  quillmoor: 3 documents -> 5 sections (human 2 -> 4, agent 0 -> 0, inbox 1 -> 1)",
    ]


def test_the_demo_corpus_passes_the_check() -> None:
    """The committed corpus itself: every sidecar valid, the agreed counts."""
    documents, problems = discover(REPOSITORY / "knowledge")
    assert problems == []
    assert count_documents(documents) == Counter(
        {
            ("quillmoor", "human"): 25,
            ("quillmoor", "agent"): 5,
            ("quillmoor", "inbox"): 3,
            ("pellwick", "human"): 4,
        }
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
        self.deletes: list[list[str]] = []

    def head_object(self, Bucket: str, Key: str) -> dict[str, str]:
        if Key not in self.objects:
            raise NotFound()
        return {"ETag": f'"{hashlib.md5(self.objects[Key]).hexdigest()}"'}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.objects[Key] = Body
        self.puts.append(Key)

    def get_paginator(self, operation: str) -> "FakeS3":
        assert operation == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        """Two pages, so a prune that reads only the first one would show."""
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        half = len(keys) // 2
        return [
            {"Contents": [{"Key": key} for key in part]} if part else {}
            for part in (keys[:half], keys[half:])
        ]

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        self.deletes.append([entry["Key"] for entry in Delete["Objects"]])
        for entry in Delete["Objects"]:
            self.objects.pop(entry["Key"], None)
        return {}


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
    add_document(
        tmp_path, "pellwick", "human", name="prices.txt", text="## Not split\n" + words(600)
    )
    documents, _ = discover(tmp_path)
    keys = [item.key for item in upload_items(documents[0])]
    assert keys == [
        "workspaces/pellwick/human/prices.txt",
        "workspaces/pellwick/human/prices.txt.metadata.json",
    ]


# --- Text encodings ------------------------------------------------------

GERMAN = "# Preise\n\nÄnderungen an Türen kosten 68 € je Stunde, Größe egal.\n"


def add_stored(root: Path, name: str, data: bytes) -> Path:
    """A document in ``quillmoor/human`` whose file holds exactly ``data``."""
    path = add_document(root, "quillmoor", "human", name=name)
    path.write_bytes(data)
    return path


def uploaded_body(root: Path) -> bytes:
    """The body of the one document file the folder produces."""
    documents, problems = discover(root)
    assert problems == []
    [item] = [item for item in upload_items(documents[0]) if not item.key.endswith(".json")]
    return item.body


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (GERMAN.encode("utf-8"), "utf-8"),
        (codecs.BOM_UTF8 + GERMAN.encode("utf-8"), "utf-8-sig"),
        (GERMAN.encode("utf-16"), "utf-16"),
        (codecs.BOM_UTF16_BE + GERMAN.encode("utf-16-be"), "utf-16"),
        (GERMAN.encode("cp1252"), "cp1252"),
        (b"", "utf-8"),
    ],
    ids=["utf-8", "utf-8-bom", "utf-16-le-bom", "utf-16-be-bom", "windows-1252", "empty"],
)
def test_detect_encoding_recognises_each_supported_encoding(data: bytes, expected: str) -> None:
    assert detect_encoding(data) == expected
    assert data.decode(expected) == (GERMAN if data else "")


@pytest.mark.parametrize("byte", [0x81, 0x8D, 0x8F, 0x90, 0x9D])
def test_a_byte_undefined_in_windows_1252_is_named_with_its_line(byte: int) -> None:
    data = "Preis: 68 €\n".encode("cp1252") + b"Stra" + bytes([byte]) + b"e\n"
    with pytest.raises(ValueError, match=f"byte 0x{byte:02x} in line 2 is undefined"):
        detect_encoding(data)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            "Größe: 12 €\n".encode() + b"Stra\xe4e\n",
            "mixed: holds UTF-8 characters, but byte 0xe4 in line 2 is not UTF-8",
        ),
        (
            "Türen\nfür 68 ".encode() + "€".encode()[:2],
            "mixed: holds UTF-8 characters, but byte 0xe2 in line 2 is not UTF-8",
        ),
    ],
    ids=["stray-byte", "cut-off-character"],
)
def test_utf_8_mixed_with_other_bytes_is_refused_not_read_as_windows_1252(
    data: bytes, expected: str
) -> None:
    with pytest.raises(ValueError, match=expected):
        detect_encoding(data)


def test_windows_1252_with_umlauts_and_euro_is_still_converted(tmp_path: Path) -> None:
    text = "Öffnungszeiten für Türen: Mo–Fr, „Notdienst“ 89 €, Größe egal, Gebühr ½.\n"
    add_stored(tmp_path, "hours.txt", text.encode("cp1252"))
    assert uploaded_body(tmp_path) == text.encode("utf-8")


def test_a_file_that_cannot_be_opened_is_a_problem_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = add_stored(tmp_path, "locked.txt", b"text")
    read_bytes = Path.read_bytes

    def refuse(path: Path) -> bytes:
        if path == locked:
            raise PermissionError(13, "Permission denied", str(path))
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    documents, problems = discover(tmp_path)
    assert documents == []
    assert len(problems) == 1
    assert problems[0].startswith(f"{locked}: ")
    assert "Permission denied" in problems[0]


def test_a_binary_file_that_cannot_be_read_is_a_problem_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PDF goes up as it is, so nothing read it before the upload did; `main` counted its
    sections first and ended in a traceback."""
    locked = add_stored(tmp_path, "locked.pdf", b"%PDF-1.7")
    read_bytes = Path.read_bytes

    def refuse(path: Path) -> bytes:
        if path == locked:
            raise PermissionError(13, "Permission denied", str(path))
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert ingest.main(["--source", str(tmp_path), "--check"]) == 1
    error = capsys.readouterr().err
    assert f"problem: {locked}: " in error and "Permission denied" in error
    assert "1 problem(s) found. Nothing was uploaded." in error


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("empty.md", b""),
        ("spaces.md", b" \n\t\n"),
        ("mark-only.md", codecs.BOM_UTF8),
        ("empty.txt", b""),
        ("empty.html", codecs.BOM_UTF16_LE),
        ("empty.csv", b"\r\n"),
    ],
)
def test_a_text_document_without_text_is_a_problem(tmp_path: Path, name: str, data: bytes) -> None:
    empty = add_stored(tmp_path, name, data)
    documents, problems = discover(tmp_path)
    assert documents == []
    assert problems == [f"{empty}: is empty: a document needs text"]


def test_a_document_emptied_since_the_last_upload_keeps_its_sections_in_the_bucket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Checked as a document with no section, its earlier sections were stale, and ``--delete``
    removed them: the folder's problem stops the run before any AWS call."""
    add_document(tmp_path, "quillmoor", "human", name="kept.md")
    add_stored(tmp_path, "emptied.md", b"")
    assert ingest.main(["--source", str(tmp_path), "--delete"]) == 1
    assert "is empty: a document needs text" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("Größe\n".encode("utf-16-le"), "zero character in line 1"),
        (b"text\n\0binary", "zero character in line 2"),
        (codecs.BOM_UTF16_LE + b"a\0b", "UTF-16 byte order mark, but is not valid UTF-16"),
        (
            codecs.BOM_UTF8 + b"ok\n\xe4",
            "UTF-8 byte order mark, but is not valid UTF-8 from line 2",
        ),
    ],
    ids=["utf-16-without-bom", "zero-byte", "broken-utf-16", "broken-utf-8-after-bom"],
)
def test_detect_encoding_refuses_what_it_cannot_tell_for_sure(data: bytes, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        detect_encoding(data)


@pytest.mark.parametrize(
    "data",
    [
        codecs.BOM_UTF8 + GERMAN.encode("utf-8"),
        GERMAN.encode("utf-16"),
        GERMAN.encode("cp1252"),
    ],
    ids=["utf-8-bom", "utf-16", "windows-1252"],
)
@pytest.mark.parametrize("name", ["prices.md", "prices.txt", "prices.csv"])
def test_text_files_go_up_as_utf_8_without_byte_order_mark(
    tmp_path: Path, data: bytes, name: str
) -> None:
    add_stored(tmp_path, name, data)
    assert uploaded_body(tmp_path) == GERMAN.encode("utf-8")


def test_a_markdown_title_behind_a_byte_order_mark_still_names_the_section(
    tmp_path: Path,
) -> None:
    add_stored(tmp_path, "faq.md", codecs.BOM_UTF8 + b"# FAQ\n\n## Billing\n\n" + b"word " * 90)
    documents, _ = discover(tmp_path)
    [key, _] = [item.key for item in upload_items(documents[0])]
    assert key.endswith("faq--01-billing.md")
    assert uploaded_body(tmp_path).startswith(b"# FAQ\n\n## Billing")


@pytest.mark.parametrize(
    "head",
    [
        '<meta charset="windows-1252">',
        "<META CHARSET=windows-1252>",
        '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">',
    ],
)
def test_converted_html_declares_the_charset_it_now_has(tmp_path: Path, head: str) -> None:
    page = f"<html><head>{head}</head><body><p>Türen für 68 €</p></body></html>\n"
    add_stored(tmp_path, "page.html", page.encode("cp1252"))
    body = uploaded_body(tmp_path).decode("utf-8")
    assert "Türen für 68 €" in body
    assert "utf-8" in body
    assert "1252" not in body and "8859" not in body


def test_utf_8_html_keeps_its_bytes_whatever_it_declares(tmp_path: Path) -> None:
    data = b'<meta charset="windows-1252"><p>plain ascii</p>\n'
    add_stored(tmp_path, "page.html", data)
    assert uploaded_body(tmp_path) == data


def test_a_file_that_cannot_be_read_as_text_is_a_problem_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_document(tmp_path, "quillmoor", "human", name="good.md")
    add_stored(tmp_path, "old.txt", b"Gr\x81e\n")
    assert main(["--source", str(tmp_path), "--check"]) == 1
    error = capsys.readouterr().err
    assert "old.txt: neither UTF-8 nor Windows-1252: byte 0x81 in line 1" in error
    assert "Nothing was uploaded." in error
    assert "Traceback" not in error


def test_an_unreadable_file_and_its_missing_sidecar_are_both_reported(tmp_path: Path) -> None:
    folder = tmp_path / "quillmoor" / "human"
    folder.mkdir(parents=True)
    (folder / "old.txt").write_bytes(b"\x81")
    _, problems = discover(tmp_path)
    assert len(problems) == 2
    assert "undefined in Windows-1252" in problems[0]
    assert "sidecar old.txt.metadata.json is missing" in problems[1]


def test_check_names_every_converted_file_with_its_encoding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_document(tmp_path, "quillmoor", "human", name="clean.md")
    add_stored(tmp_path, "prices.md", GERMAN.encode("cp1252"))
    add_stored(tmp_path, "hours.txt", GERMAN.encode("utf-16"))
    add_stored(tmp_path, "page.html", b'<meta charset="windows-1252"><p>T\xfcr</p>')
    assert main(["--source", str(tmp_path), "--check"]) == 0
    out = capsys.readouterr().out
    folder = tmp_path / "quillmoor" / "human"
    assert "3 file(s) not stored as UTF-8 go up converted to UTF-8:" in out
    assert f"  {folder / 'hours.txt'}: UTF-16\n" in out
    assert f"  {folder / 'page.html'}: Windows-1252, its <meta> charset set to utf-8\n" in out
    assert f"  {folder / 'prices.md'}: Windows-1252\n" in out
    assert "clean.md" not in out


def test_check_names_no_file_when_everything_is_utf_8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_document(tmp_path, "quillmoor", "human", text=GERMAN)
    assert main(["--source", str(tmp_path), "--check"]) == 0
    assert "converted" not in capsys.readouterr().out


def items_as_before(document: Document) -> list[tuple[str, bytes, str]]:
    """What `upload_items` produced before encodings were recognised, sidecars left out."""
    suffix = document.path.suffix.lower()
    if suffix != ".md":
        key = object_key(document.workspace, document.layer, document.path.name)
        return [(key, document.path.read_bytes(), layout.CONTENT_TYPES[suffix])]
    sections = split_markdown(document.path.read_text(encoding="utf-8"))
    return [
        (
            object_key(
                document.workspace, document.layer, section_filename(document.path.stem, n, slug)
            ),
            text.encode("utf-8"),
            layout.CONTENT_TYPES[".md"],
        )
        for n, (slug, text) in enumerate(sections, start=1)
    ]


def test_a_clean_folder_goes_up_byte_for_byte_as_before(tmp_path: Path) -> None:
    """The demo corpus, and UTF-8 files with Windows line ends and umlauts in every text type."""
    for name in ("crlf.md", "crlf.txt", "crlf.csv", "crlf.html"):
        add_stored(tmp_path, name, sectioned_text(3).replace("\n", "\r\n").encode("utf-8"))
    add_stored(tmp_path, "umlaut.md", GERMAN.encode("utf-8"))
    add_stored(tmp_path, "scan.pdf", b"%PDF-1.4 \x81\x00\xff")

    for root in (REPOSITORY / "knowledge", tmp_path):
        documents, problems = discover(root)
        assert problems == []
        assert all(document.encoding == "utf-8" for document in documents)
        for document in documents:
            now = [
                (item.key, item.body, item.content_type)
                for item in upload_items(document)
                if not item.key.endswith(".metadata.json")
            ]
            assert now == items_as_before(document), document.path


# --- Prune -------------------------------------------------------------


def uploaded_bucket(tmp_path: Path) -> tuple[FakeS3, list[Document]]:
    """A bucket holding two workspaces' documents, plus objects the folder no longer has."""
    add_document(tmp_path, "quillmoor", "human", name="a.md")
    add_document(tmp_path, "pellwick", "human", name="b.md")
    documents, _ = discover(tmp_path)
    s3 = FakeS3()
    upload(s3, "bucket", documents, log=lambda _: None)
    s3.objects["workspaces/quillmoor/human/gone--01-gone.md"] = b"old"
    s3.objects["workspaces/quillmoor/human/gone--01-gone.md.metadata.json"] = b"{}"
    s3.objects["workspaces/pellwick/inbox/old.md"] = b"old"
    s3.objects["outside/the/prefix.md"] = b"not ours"
    return s3, documents


def test_stale_keys_are_what_the_bucket_has_and_the_folder_does_not(tmp_path: Path) -> None:
    s3, documents = uploaded_bucket(tmp_path)
    assert stale_keys(s3, "bucket", documents) == [
        "workspaces/pellwick/inbox/old.md",
        "workspaces/quillmoor/human/gone--01-gone.md",
        "workspaces/quillmoor/human/gone--01-gone.md.metadata.json",
    ]


def test_one_workspace_prunes_only_inside_that_workspace(tmp_path: Path) -> None:
    """A partial upload must not take the other workspaces for stale."""
    s3, documents = uploaded_bucket(tmp_path)
    quillmoor = [document for document in documents if document.workspace == "quillmoor"]
    assert stale_keys(s3, "bucket", quillmoor, only_workspace="quillmoor") == [
        "workspaces/quillmoor/human/gone--01-gone.md",
        "workspaces/quillmoor/human/gone--01-gone.md.metadata.json",
    ]


def test_a_folder_with_one_workspace_never_prunes_the_others(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One company's own folder with --delete and no --workspace: only that company is pruned."""
    s3, _ = uploaded_bucket(tmp_path / "demo")
    company = tmp_path / "company"
    add_document(company, "third", "human", name="c.md")
    s3.objects["workspaces/third/human/gone--01-gone.md"] = b"old"
    assert run_main_against(s3, ["--source", str(company), "--delete"], monkeypatch) == 0
    assert "Deleted 1 object(s) that are no longer in the folder:" in capsys.readouterr().out
    assert "workspaces/third/human/gone--01-gone.md" not in s3.objects
    assert "workspaces/pellwick/inbox/old.md" in s3.objects
    assert "workspaces/quillmoor/human/gone--01-gone.md" in s3.objects
    assert "workspaces/quillmoor/human/a--01-a-md.md" in s3.objects


def test_delete_goes_in_batches_of_at_most_a_thousand() -> None:
    s3 = FakeS3()
    keys = [f"workspaces/quillmoor/human/{n:04d}.md" for n in range(2001)]
    for key in keys:
        s3.objects[key] = b"x"
    delete_keys(s3, "bucket", keys)
    assert [len(batch) for batch in s3.deletes] == [1000, 1000, 1]
    assert s3.objects == {}


def test_a_key_s3_refuses_to_delete_is_an_aws_problem() -> None:
    class Refusing(FakeS3):
        def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
            return {"Errors": [{"Key": "workspaces/x/human/a.md", "Code": "AccessDenied"}]}

    with pytest.raises(AwsProblem, match="1 object.*AccessDenied"):
        delete_keys(Refusing(), "bucket", ["workspaces/x/human/a.md"])


def run_main_against(s3: FakeS3, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """`main` with the fake bucket, a finished job, and no stack to read."""
    agent = FakeAgent(["COMPLETE"])
    clients = {"s3": s3, "bedrock-agent": agent}
    monkeypatch.setattr(ingest, "client", lambda service, region: clients[service])
    return main(
        [*argv, "--bucket", "bucket", "--knowledge-base-id", "KB", "--data-source-id", "DS"]
    )


def test_main_lists_stale_objects_and_deletes_nothing_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    s3, _ = uploaded_bucket(tmp_path)
    assert run_main_against(s3, ["--source", str(tmp_path)], monkeypatch) == 0
    out = capsys.readouterr().out
    assert (
        "3 object(s) in the bucket are no longer in the folder and would be deleted with --delete:"
        in out
    )
    assert "  workspaces/pellwick/inbox/old.md" in out
    assert s3.deletes == []
    assert "workspaces/pellwick/inbox/old.md" in s3.objects


def test_main_deletes_stale_objects_with_delete_and_never_outside_the_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    s3, _ = uploaded_bucket(tmp_path)
    assert run_main_against(s3, ["--source", str(tmp_path), "--delete"], monkeypatch) == 0
    assert "Deleted 3 object(s) that are no longer in the folder:" in capsys.readouterr().out
    assert not any("gone" in key or "old.md" in key for key in s3.objects)
    assert "outside/the/prefix.md" in s3.objects
    assert "workspaces/quillmoor/human/a--01-a-md.md" in s3.objects


def test_main_says_so_when_nothing_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    add_document(tmp_path, "quillmoor", "human", name="a.md")
    assert run_main_against(FakeS3(), ["--source", str(tmp_path), "--delete"], monkeypatch) == 0
    assert (
        "No object in the bucket is missing from the folder (compared: quillmoor)."
        in capsys.readouterr().out
    )


def test_a_workspace_folder_without_documents_is_not_compared_and_the_all_clear_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace emptied locally keeps its objects, and the all-clear does not claim it."""
    s3, _ = uploaded_bucket(tmp_path / "demo")
    folder = tmp_path / "emptied"
    add_document(folder, "quillmoor", "human", name="a.md")
    (folder / "pellwick" / "human").mkdir(parents=True)
    del s3.objects["workspaces/quillmoor/human/gone--01-gone.md"]
    del s3.objects["workspaces/quillmoor/human/gone--01-gone.md.metadata.json"]
    assert run_main_against(s3, ["--source", str(folder), "--delete"], monkeypatch) == 0
    assert (
        "No object in the bucket is missing from the folder (compared: quillmoor)."
        in capsys.readouterr().out
    )
    assert s3.deletes == []
    assert "workspaces/pellwick/inbox/old.md" in s3.objects


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
        return {
            "ingestionJob": {
                "ingestionJobId": "job-1",
                "status": status,
                "statistics": self.statistics,
            }
        }

    def start_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str) -> dict[str, Any]:
        return self._job(self.statuses[0])

    def get_ingestion_job(
        self, knowledgeBaseId: str, dataSourceId: str, ingestionJobId: str
    ) -> dict[str, Any]:
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
        run_ingestion_job(
            agent,
            "kb",
            "ds",
            poll_seconds=5,
            timeout_seconds=10,
            sleep=lambda _: None,
            log=lambda _: None,
        )


@pytest.mark.parametrize(
    ("status", "failed", "expected"),
    [("COMPLETE", 0, True), ("COMPLETE", 2, False), ("FAILED", 0, False), ("STOPPED", 0, False)],
)
def test_job_success_needs_complete_and_no_failed_documents(
    status: str, failed: int, expected: bool
) -> None:
    job = {
        "ingestionJobId": "job-1",
        "status": status,
        "statistics": {"numberOfDocumentsFailed": failed},
    }
    assert job_succeeded(job) is expected


# --- Command line without AWS --------------------------------------------


def test_main_check_only_returns_zero_for_a_valid_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
        raise AwsProblem(
            "Reading the outputs failed: the AWS login is missing or expired. "
            "Sign in again with `aws login`, then rerun."
        )

    monkeypatch.setattr(ingest, "client", expired)
    assert main(["--source", str(tmp_path)]) == 2
    error = capsys.readouterr().err
    assert "aws login" in error
    assert "Traceback" not in error

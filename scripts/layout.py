"""The layout contract of a documents folder (docs/metadata.md), and what each document
becomes in the documents bucket: its keys, its sections, its text as UTF-8."""

import codecs
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

BUCKET_PREFIX = "workspaces/"

LAYERS = ("human", "agent", "inbox")
SIDECAR_SUFFIX = ".metadata.json"
REQUIRED_KEYS = ("workspace", "layer", "source", "license")

WORKSPACE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

MAX_WORKSPACE_CHARACTERS = 64

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

#: File types read as text and uploaded as UTF-8, whatever they were stored in.
TEXT_SUFFIXES = {".md", ".txt", ".html", ".csv"}

#: The encodings a text file is recognised in, by codec, with the name the output uses.
ENCODING_NAMES = {
    "utf-8": "UTF-8",
    "utf-8-sig": "UTF-8 with byte order mark",
    "utf-16": "UTF-16",
    "cp1252": "Windows-1252",
}

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
    #: The codec a text document is stored in, from `detect_encoding`; unused for binary ones.
    encoding: str = "utf-8"

    @property
    def sidecar(self) -> Path:
        return self.path.with_name(self.path.name + SIDECAR_SUFFIX)


MIN_SECTION_WORDS = 80

MAX_SECTION_WORDS = 450

SECTION_SEPARATOR = "--"

_SLUG_MAX_CHARACTERS = 40


def _words(text: str) -> int:
    return len(text.split())


def _split_at(lines: list[str], prefix: str) -> list[list[str]]:
    """Cut lines into blocks, each starting at a heading with ``prefix``."""
    blocks: list[list[str]] = [[]]
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith(prefix) and blocks[-1]:
            blocks.append([])
        blocks[-1].append(line)
    return [block for block in blocks if "".join(block).strip()]


def _outside_fences(lines: list[str]) -> list[str]:
    """The lines outside code fences, without the fence lines: a ``# `` in code is no heading."""
    outside: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            outside.append(line)
    return outside


def _join_short(blocks: list[list[str]]) -> list[list[str]]:
    """Join blocks under `MIN_SECTION_WORDS` to their predecessor."""
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


#: Letters spelled out after lowercasing: the German ones the way Germans write them without
#: umlauts, and the ones that hold no plain letter for the decomposition to find.
_GERMAN_LETTERS = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "ø": "oe",
        "æ": "ae",
        "œ": "oe",
        "ł": "l",
        "đ": "d",
    }
)


def _ascii_letters(text: str) -> str:
    """``text`` lowercased, with ä, ö, ü, ß spelled ae, oe, ue, ss and other accents dropped.

    Composed first, so an umlaut stored as letter plus dots is still ``ae`` and a wide or
    joined letter such as ``Ａ`` or ``ﬁ`` is the plain one. The remaining letters are taken
    apart and their marks left out, so é is e; a letter neither step knows is left for
    `slugify` to turn into a hyphen.
    """
    text = unicodedata.normalize("NFKC", text).lower().translate(_GERMAN_LETTERS)
    return "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(character)
    )


def slugify(heading: str) -> str:
    """A short, lowercase ASCII file-name part from a heading."""
    slug = re.sub(r"[^a-z0-9]+", "-", _ascii_letters(heading.lstrip("#"))).strip("-")
    if len(slug) > _SLUG_MAX_CHARACTERS:
        slug = slug[:_SLUG_MAX_CHARACTERS].rsplit("-", 1)[0]
    return slug or "section"


def _naming_heading(lines: list[str]) -> str | None:
    """The heading a section is named after."""
    first = next((line for line in lines if line.strip()), "")
    if first.startswith(("## ", "### ")):
        return first
    for prefix in ("## ", "# "):
        for line in _outside_fences(lines):
            if line.startswith(prefix):
                return line
    return None


def _with_context(block: list[str], context: list[str]) -> str:
    """The block's text, preceded by the context lines it does not already contain."""
    missing = [line for line in context if line not in block]
    head = "\n\n".join(missing)
    body = "\n".join(block).strip("\n")
    return f"{head}\n\n{body}\n" if head else f"{body}\n"


def split_markdown(text: str) -> list[tuple[str, str]]:
    """Split a Markdown document into ``(slug, text)`` sections."""
    lines = text.splitlines()
    title = next((line for line in _outside_fences(lines) if line.startswith("# ")), None)
    title_context = [title] if title else []

    sections: list[tuple[str, str]] = []
    for block in _join_short(_split_at(lines, "## ")):
        pieces = [block]
        parent = next((line for line in _outside_fences(block) if line.startswith("## ")), None)
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


#: A section's file: its document's name, `--`, its number and the slug of its heading
#: (`section_filename`). A document's own name may hold `--` too.
_SECTION_FILE = re.compile(r"(.+)--\d{2,}-[a-z0-9-]+\.md")


def document_of(file: str) -> str:
    """The document a source file comes from: the name before a section's `--NN-slug.md`, or the
    name without its suffix of a file uploaded whole. Composed, as ingest names the keys."""
    name = unicodedata.normalize("NFC", file)
    section = _SECTION_FILE.fullmatch(name)
    return section.group(1) if section else Path(name).stem


#: A charset declared in an HTML meta tag, in either of its two spellings.
_HTML_CHARSET = re.compile(r"(<meta\b[^>]*?charset\s*=\s*[\"']?)([\w.:-]+)", re.IGNORECASE)


#: One well-formed UTF-8 character of two to four bytes.
_UTF8_MULTIBYTE = re.compile(
    rb"[\xc2-\xdf][\x80-\xbf]"
    rb"|\xe0[\xa0-\xbf][\x80-\xbf]|[\xe1-\xec\xee\xef][\x80-\xbf]{2}|\xed[\x80-\x9f][\x80-\xbf]"
    rb"|\xf0[\x90-\xbf][\x80-\xbf]{2}|[\xf1-\xf3][\x80-\xbf]{3}|\xf4[\x80-\x8f][\x80-\xbf]{2}"
)


def _line(data: bytes, position: int) -> int:
    return data.count(b"\n", 0, position) + 1


def detect_encoding(data: bytes) -> str:
    """The codec a text file is stored in, one of `ENCODING_NAMES`.

    UTF-8 (with or without a byte order mark), then UTF-16 with a byte order mark,
    then Windows-1252. Raises ValueError with the reason when none of them fits for sure:
    UTF-8 mixed with bytes that are not, a byte Windows-1252 leaves undefined, or a zero
    character, which no text file holds.
    """
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    else:
        try:
            data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError as exc:
            # Windows-1252 text practically never holds a valid UTF-8 sequence; one that does
            # is UTF-8 with stray bytes, and reading it as Windows-1252 would garble every ä.
            if _UTF8_MULTIBYTE.search(data):
                raise ValueError(
                    f"mixed: holds UTF-8 characters, but byte 0x{data[exc.start]:02x} in line "
                    f"{_line(data, exc.start)} is not UTF-8"
                ) from exc
            encoding = "cp1252"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        # exc.object is what the codec read: for utf-8-sig the bytes after the mark.
        line = _line(exc.object, exc.start)
        if encoding == "cp1252":
            raise ValueError(
                f"neither UTF-8 nor Windows-1252: byte 0x{exc.object[exc.start]:02x} in line "
                f"{line} is undefined in Windows-1252"
            ) from exc
        if encoding == "utf-16":
            raise ValueError(
                "starts with a UTF-16 byte order mark, but is not valid UTF-16"
            ) from exc
        raise ValueError(
            f"starts with a UTF-8 byte order mark, but is not valid UTF-8 from line {line} on"
        ) from exc
    zero = text.find("\0")
    if zero != -1:
        line = text.count("\n", 0, zero) + 1
        raise ValueError(
            f"zero character in line {line}: not a text file, or "
            "stored in an encoding that cannot be told for sure, such as UTF-16 without a "
            "byte order mark"
        )
    return encoding


def decoded_text(document: Document) -> str:
    """A text document's text; an HTML one converted from another encoding declares UTF-8."""
    text = document.path.read_bytes().decode(document.encoding)
    if document.encoding != "utf-8" and document.path.suffix.lower() == ".html":
        text = _HTML_CHARSET.sub(r"\g<1>utf-8", text)
    return text


def format_conversions(documents: Iterable[Document]) -> list[str]:
    """Every text document not stored as UTF-8 without a byte order mark, and what it was."""
    lines = []
    for document in documents:
        if document.encoding == "utf-8":
            continue
        line = f"  {document.path}: {ENCODING_NAMES[document.encoding]}"
        if document.path.suffix.lower() == ".html" and _HTML_CHARSET.search(
            document.path.read_bytes().decode(document.encoding)
        ):
            line += ", its <meta> charset set to utf-8"
        lines.append(line)
    if not lines:
        return []
    return [f"{len(lines)} file(s) not stored as UTF-8 go up converted to UTF-8:"] + lines


@dataclass(frozen=True)
class UploadItem:
    """One object to put into the bucket."""

    key: str
    body: bytes
    content_type: str


def upload_items(document: Document) -> list[UploadItem]:
    """What a document becomes in the bucket: sections or the whole file, each with its sidecar."""
    sidecar = document.sidecar.read_bytes()
    suffix = document.path.suffix.lower()
    # Composed, so a name saved on a Mac (an umlaut as letter plus dots) and the same name
    # saved on Windows are one key, not two copies of the same document.
    name = unicodedata.normalize("NFC", document.path.name)
    stem = unicodedata.normalize("NFC", document.path.stem)
    if suffix != ".md":
        key = object_key(document.workspace, document.layer, name)
        if suffix in TEXT_SUFFIXES and document.encoding != "utf-8":
            body = decoded_text(document).encode("utf-8")
        else:
            body = document.path.read_bytes()
        parts = [(key, body, CONTENT_TYPES[suffix])]
    else:
        sections = split_markdown(decoded_text(document))
        parts = [
            (
                object_key(
                    document.workspace,
                    document.layer,
                    section_filename(stem, number, slug),
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


def count_sections(documents: Iterable[Document]) -> Counter[tuple[str, str]]:
    """Number of uploaded document files per (workspace, layer), sidecars not counted."""
    counts: Counter[tuple[str, str]] = Counter()
    for document in documents:
        files = [item for item in upload_items(document) if not item.key.endswith(SIDECAR_SUFFIX)]
        counts[(document.workspace, document.layer)] += len(files)
    return counts

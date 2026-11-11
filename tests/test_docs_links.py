"""Every link between the repository's Markdown files lands where it means to.

A link to a line number drifts as soon as a line is added above it, and a renamed heading
breaks every link to it without a sound. These tests read every Markdown file git tracks and
check each inline link with a relative target: the file is there, and so is the anchor, a
heading by GitHub's rules or an ``<a name>``. Each entry of docs/decisions.md carries a fixed
anchor of its own, so a link to it holds when the entry's title changes
(docs/decisions.md#2026-11-11-anchors-not-lines).
"""

import re
import subprocess
from collections.abc import Iterator
from functools import cache
from pathlib import Path

#: Not taug.infra.shared's: importing it starts jsii, which these tests do not need.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = REPOSITORY_ROOT / "docs" / "decisions.md"

#: A code fence and what follows it; nothing inside a fenced block is a link.
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
#: Inline code, which may show link syntax without being a link.
CODE_SPAN = re.compile(r"`[^`]*`")
#: The target of an inline link or image: ``](target)``, optionally ``](target "title")``.
LINK_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
#: A target with a scheme (https:, mailto:) or a host (//); those are not checked here.
EXTERNAL = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*:|//)")
#: GitHub's links to a line of a file: ``?plain=1#L12`` or ``#L12-L20``.
LINE_NUMBER = re.compile(r"(\?plain=1)|(#L\d+(-L\d+)?$)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
NAMED_ANCHOR = re.compile(r"<a\s+name=\"([^\"]+)\"\s*>\s*</a>")
DECISION_ENTRY = re.compile(r"^\*\*20\d\d-\d\d-\d\d")
DECISION_ANCHOR = re.compile(r"^<a name=\"(20\d\d-\d\d-\d\d-[a-z0-9-]+)\"></a>$")


def markdown_files() -> list[Path]:
    """The Markdown files git tracks; untracked folders such as .venv or a worktree stay out."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [REPOSITORY_ROOT / name for name in listed.split("\0") if name]


def outside_fences(path: Path) -> Iterator[tuple[int, str]]:
    """Each line outside fenced code blocks, numbered from 1. A block ends only at a fence of
    its own character, at least as long and with nothing but spaces after it (GFM 4.5), so a
    fence shown inside a longer one stays code."""
    opened = ""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        fence = FENCE.match(line)
        if not opened:
            if fence:
                opened = fence.group(1)
            else:
                yield number, line
        elif (
            fence
            and fence.group(1)[0] == opened[0]
            and len(fence.group(1)) >= len(opened)
            and not fence.group(2).strip(" ")
        ):
            opened = ""


def prose_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Each line outside fenced code blocks, numbered from 1, with its inline code removed."""
    for number, line in outside_fences(path):
        yield number, CODE_SPAN.sub("", line)


def heading_slug(text: str) -> str:
    """The anchor GitHub gives a heading: link text kept, lower case, punctuation removed,
    spaces turned into hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


@cache
def anchors(path: Path) -> frozenset[str]:
    """Every anchor a link may name in ``path``: its headings, numbered like GitHub numbers
    repeated ones (``-1``, ``-2``), and its ``<a name>`` anchors."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in outside_fences(path):
        # An anchor shown as inline code is text on GitHub, not an anchor.
        found.update(NAMED_ANCHOR.findall(CODE_SPAN.sub("", line)))
        heading = HEADING.match(line)
        if heading:
            slug = heading_slug(heading.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(slug if count == 0 else f"{slug}-{count}")
    return frozenset(found)


def relative_links() -> Iterator[tuple[str, str, Path]]:
    """Each relative link of every tracked Markdown file: where it stands, its target, and the
    file it sits in."""
    for path in markdown_files():
        for number, line in prose_lines(path):
            for target in LINK_TARGET.findall(line):
                if not EXTERNAL.match(target):
                    where = f"{path.relative_to(REPOSITORY_ROOT).as_posix()}:{number}"
                    yield where, target, path


def broken(target: str, source: Path) -> str | None:
    """Why ``target``, written in ``source``, leads nowhere, or None when it lands."""
    file_part, _, anchor = target.partition("#")
    file_part = file_part.split("?")[0]
    destination = (source.parent / file_part).resolve() if file_part else source
    if not destination.exists():
        return "no such file"
    if anchor and destination.suffix == ".md" and anchor not in anchors(destination):
        return f"no anchor {anchor!r} in {destination.name}"
    return None


def test_a_fence_closes_only_at_a_fence_of_its_own_kind(tmp_path: Path) -> None:
    path = tmp_path / "fences.md"
    path.write_text(
        "\n".join(
            [
                "````markdown",
                "```",
                "[in code](missing-1.md)",
                "```",
                "````",
                "~~~",
                "```",
                "[in code](missing-2.md)",
                "~~~ not a closing fence",
                "~~~~",
                "[prose](here.md)",
            ]
        ),
        encoding="utf-8",
    )
    assert [line for _, line in prose_lines(path)] == ["[prose](here.md)"]


def test_an_anchor_shown_as_inline_code_is_no_anchor(tmp_path: Path) -> None:
    path = tmp_path / "anchors.md"
    path.write_text(
        'Write `<a name="shown"></a>` above it.\n<a name="real"></a>\n', encoding="utf-8"
    )
    assert anchors(path) == {"real"}


def test_the_repository_has_markdown_links_to_check() -> None:
    assert sum(1 for _ in relative_links()) > 100


def test_every_relative_link_lands() -> None:
    failures = [
        f"{where}: {target} ({reason})"
        for where, target, source in relative_links()
        if (reason := broken(target, source))
    ]
    assert not failures, "\n".join(failures)


def test_no_link_points_at_a_line_number() -> None:
    failures = [
        f"{where}: {target}" for where, target, _ in relative_links() if LINE_NUMBER.search(target)
    ]
    assert not failures, "link to an anchor instead:\n" + "\n".join(failures)


def test_every_decision_has_an_anchor_of_its_own() -> None:
    lines = DECISIONS.read_text(encoding="utf-8").splitlines()
    entries = [number for number, line in enumerate(lines) if DECISION_ENTRY.match(line)]
    names: list[str] = []
    missing: list[str] = []
    for number in entries:
        anchor = DECISION_ANCHOR.match(lines[number - 1]) if number else None
        if anchor:
            names.append(anchor.group(1))
        else:
            missing.append(f"docs/decisions.md:{number + 1}")
    assert not missing, 'put <a name="DATE-a-few-words"></a> above:\n' + "\n".join(missing)
    assert len(names) == len(set(names)), "an anchor is used twice"
    assert len(entries) > 100

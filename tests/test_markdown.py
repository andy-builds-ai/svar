"""Tests for the page's Markdown (web/markdown.js), run in Node.

The page has no build step and no JavaScript test runner, and gets none for
this: Node runs the file itself, which exports its two pure functions when
it finds ``module``. The part that touches the DOM is checked by reading the
file instead: it may build elements and fill text, and nothing else.

Skipped where Node is missing. The CDK needs Node, so every machine that can
deploy the page can run these.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WEB_FOLDER = Path(__file__).resolve().parents[1] / "web"
MARKDOWN_JS = WEB_FOLDER / "markdown.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def run(function: str, text: str) -> Any:
    """Call one exported function of markdown.js in Node and return its result."""
    script = (
        f"const md = require({json.dumps(str(MARKDOWN_JS))});"
        "let input = '';"
        "process.stdin.on('data', (chunk) => { input += chunk; });"
        "process.stdin.on('end', () => process.stdout.write("
        f"JSON.stringify(md.{function}(JSON.parse(input)))));"
    )
    completed = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps(text),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


@needs_node
def test_bold_is_found_and_the_text_around_it_kept() -> None:
    assert run("inlineParts", "Use **Settings > Export** now [1].") == [
        {"text": "Use ", "bold": False},
        {"text": "Settings > Export", "bold": True},
        {"text": " now [1].", "bold": False},
    ]


@needs_node
def test_a_single_character_can_be_bold() -> None:
    assert run("inlineParts", "Step **2** of **3**") == [
        {"text": "Step ", "bold": False},
        {"text": "2", "bold": True},
        {"text": " of ", "bold": False},
        {"text": "3", "bold": True},
    ]


def test_the_script_uses_no_lookbehind() -> None:
    """A lookbehind is a syntax error in older Safari, and it takes the whole file down with it."""
    assert "(?<" not in MARKDOWN_JS.read_text(encoding="utf-8")


@needs_node
@pytest.mark.parametrize(
    "line", ["2 ** 3 is 8", "an unmatched **star", "** spaced **", "*one star*", "****"]
)
def test_what_is_not_bold_stays_as_written(line: str) -> None:
    assert run("inlineParts", line) == [{"text": line, "bold": False}]


@needs_node
def test_markup_in_an_answer_is_text_and_never_an_element() -> None:
    """The parser hands back strings; a tag inside bold is still a string."""
    parts = run("inlineParts", '<img src=x onerror="alert(1)"> and **<script>x</script>**')
    assert parts == [
        {"text": '<img src=x onerror="alert(1)"> and ', "bold": False},
        {"text": "<script>x</script>", "bold": True},
    ]


@needs_node
def test_paragraphs_and_both_kinds_of_list() -> None:
    answer = (
        "Two ways [1]:\n\n- **Export** the project\n* or copy it\n\n"
        "3. third\n4) fourth\nDone.\n\n[Note.]"
    )
    assert run("answerBlocks", answer) == [
        {"type": "paragraph", "lines": ["Two ways [1]:"]},
        {
            "type": "list",
            "ordered": False,
            "start": 1,
            "items": ["**Export** the project", "or copy it"],
        },
        {"type": "list", "ordered": True, "start": 3, "items": ["third", "fourth"]},
        {"type": "paragraph", "lines": ["Done."]},
        {"type": "paragraph", "lines": ["[Note.]"]},
    ]


@needs_node
def test_a_bold_line_is_no_list_item_and_lines_stay_lines() -> None:
    assert run("answerBlocks", "**Short answer:** yes.\nSecond line.\r\nThird.") == [
        {"type": "paragraph", "lines": ["**Short answer:** yes.", "Second line.", "Third."]}
    ]


@needs_node
def test_a_list_that_changes_kind_starts_a_new_list() -> None:
    blocks = run("answerBlocks", "- a\n1. b")
    assert [(block["type"], block["ordered"]) for block in blocks] == [
        ("list", False),
        ("list", True),
    ]


def test_the_renderer_builds_elements_and_fills_text_and_nothing_else() -> None:
    """No API that parses a string as HTML may appear in the file at all."""
    script = MARKDOWN_JS.read_text(encoding="utf-8")
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "Function(",
    ):
        assert forbidden not in script, forbidden
    created = set(re.findall(r'createElement\("([a-z]+)"\)', script))
    assert created == {"strong", "p", "br", "li"}
    assert 'createElement(block.ordered ? "ol" : "ul")' in script


def test_the_page_loads_the_markdown_before_the_script_that_uses_it() -> None:
    markup = (WEB_FOLDER / "index.html").read_text(encoding="utf-8")
    assert markup.index('<script src="markdown.js">') < markup.index('<script src="app.js">')
    assert '<div id="answer" class="answer">' in markup
    assert "renderAnswer(answerText, answer.answer)" in (WEB_FOLDER / "app.js").read_text(
        encoding="utf-8"
    )

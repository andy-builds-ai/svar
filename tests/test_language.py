"""Tests for the page's two languages (web/language.js), run in Node.

Like web/markdown.js, the file holds only pure functions and exports them when it
finds ``module``, so Node can run it without a browser. What touches the page
(app.js, index.html) is checked by reading the files: every text the page shows has
a key in both languages, and no sentence is kept in two places.

Skipped where Node is missing. The CDK needs Node, so every machine that can deploy
the page can run these.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WEB_FOLDER = Path(__file__).resolve().parents[1] / "web"
LANGUAGE_JS = WEB_FOLDER / "language.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
NODE_COMMAND = NODE or "node"

pytestmark = needs_node


def run(function: str, *arguments: Any) -> Any:
    """Call one exported function of language.js in Node with these arguments."""
    script = (
        f"const page = require({json.dumps(str(LANGUAGE_JS))});"
        "let input = '';"
        "process.stdin.on('data', (chunk) => { input += chunk; });"
        "process.stdin.on('end', () => process.stdout.write("
        f"JSON.stringify(page.{function}(...JSON.parse(input)))));"
    )
    completed = subprocess.run(
        [NODE_COMMAND, "-e", script],
        input=json.dumps(list(arguments)).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert completed.returncode == 0, stderr
    return json.loads(completed.stdout.decode("utf-8"))


def exported(name: str) -> Any:
    """One value language.js exports, as the page has it."""
    script = (
        f"const page = require({json.dumps(str(LANGUAGE_JS))});"
        f"process.stdout.write(JSON.stringify(page.{name}));"
    )
    completed = subprocess.run([NODE_COMMAND, "-e", script], capture_output=True, check=True)
    return json.loads(completed.stdout.decode("utf-8"))


def texts() -> dict[str, dict[str, str]]:
    """The two tables, as the page has them."""
    return exported("TEXTS")


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


MARKUP = (WEB_FOLDER / "index.html").read_text(encoding="utf-8")
APP = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
LANGUAGE = LANGUAGE_JS.read_text(encoding="utf-8")


# --- The two tables ------------------------------------------------------


def test_the_page_speaks_german_and_english_and_starts_in_english() -> None:
    assert exported("LANGUAGES") == ["en", "de"]
    assert exported("DEFAULT_LANGUAGE") == "en"
    assert set(texts()) == {"en", "de"}


def test_every_text_is_in_both_languages_with_the_same_placeholders() -> None:
    english, german = texts()["en"], texts()["de"]
    assert set(german) == set(english)
    for key, text in english.items():
        assert placeholders(german[key]) == placeholders(text), key
        assert german[key].strip() and text.strip(), key
    # The same word in both, on purpose: the example workspace is a name, not a word.
    same = {key for key in english if english[key] == german[key]}
    assert same == {"workspace_placeholder"}


def test_the_german_texts_speak_to_the_reader_as_sie_never_du() -> None:
    german = texts()["de"]
    for key, text in german.items():
        words = set(re.findall(r"\w+", text))
        assert not words & {"du", "dein", "deine", "deinen", "dich", "dir"}, key


def asked_for_in_markup() -> set[str]:
    return set(re.findall(r'data-i18n(?:-placeholder|-label)?="(\w+)"', MARKUP))


def asked_for_in_scripts() -> set[str]:
    """The keys the scripts ask for: every quoted name with an underscore in app.js and in
    the code of language.js (not its tables), and the page title. A typo in one of them is
    a name that is no key, and the test below finds it."""
    code = LANGUAGE.split("const TEXTS = {")[0] + LANGUAGE.split("function startLanguage")[1]
    quoted = set(re.findall(r'"([a-z]+(?:_[a-z]+)+)"', APP + code))
    return quoted | ({"title"} if 'document.title = t("title")' in APP else set())


def test_every_key_the_page_asks_for_has_a_text() -> None:
    known = set(texts()["en"])
    assert asked_for_in_markup() <= known
    assert asked_for_in_scripts() <= known
    assert asked_for_in_scripts()  # the search finds something


def test_no_text_is_left_over_that_nothing_asks_for() -> None:
    # The layer names are asked for as "layer_" plus the layer's own name.
    asked = asked_for_in_markup() | asked_for_in_scripts()
    unused = set(texts()["en"]) - asked - {"layer_human", "layer_agent", "layer_inbox"}
    assert unused == set()


def test_the_markup_holds_no_sentence_of_its_own() -> None:
    """The elements are empty and app.js fills them, so no sentence is kept in two places."""
    body = MARKUP.split("<body>", 1)[1]
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    visible = re.sub(r"<[^>]*>", "|", body)
    words = [part.strip() for part in visible.split("|") if part.strip()]
    # What stays is the two language buttons' own names and the counter's first value.
    assert sorted(words) == sorted(["DE", "EN", "0 / 1000"])


def test_app_js_holds_none_of_the_old_english_sentences() -> None:
    for sentence in (
        "Asking…",
        "Fill in the workspace",
        "Type a question",
        "Rests on",
        "no model call",
        "tokens in",
        "ms in the function",
        "Andur is busy",
        "Forget key",
        "The key is gone",
        "https connection",
        "Nothing came back",
        "the connection failed",
    ):
        assert sentence not in APP, sentence


# --- The page's own files ------------------------------------------------


def test_the_language_file_loads_between_the_markdown_and_the_page_script() -> None:
    markdown = MARKUP.index('<script src="markdown.js">')
    language = MARKUP.index('<script src="language.js">')
    app = MARKUP.index('<script src="app.js">')
    assert markdown < language < app


def test_the_page_has_a_visible_switch_with_both_languages() -> None:
    buttons = re.findall(r'<button[^>]*data-language="(\w+)"[^>]*>(\w+)</button>', MARKUP)
    assert buttons == [("de", "DE"), ("en", "EN")]


def test_the_page_sends_its_language_with_every_question() -> None:
    assert "language: language" in APP
    # In the body, next to the question and the workspace.
    assert re.search(r"JSON\.stringify\(\{[^}]*question: question[^}]*language: language", APP)


def test_the_choice_is_kept_in_the_browser_like_the_workspace() -> None:
    assert 'const STORED_LANGUAGE = "andur.language";' in APP
    assert "recall(STORED_LANGUAGE)" in APP
    assert "remember(STORED_LANGUAGE, language)" in APP
    # The key is the only thing the page keeps that is secret; the language is not.
    assert "STORED_LANGUAGE" not in LANGUAGE


def test_the_page_sets_the_language_of_the_document_and_its_title() -> None:
    assert "document.documentElement.lang = language" in APP
    assert "document.title = t(" in APP


def test_the_page_starts_from_the_browsers_language() -> None:
    assert "startLanguage(recall(STORED_LANGUAGE), window.navigator.language)" in APP


# --- Starting language ---------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "browser", "expected"),
    [
        ("", "de", "de"),
        ("", "de-DE", "de"),
        ("", "de-AT", "de"),
        ("", "DE-ch", "de"),
        ("", "en-US", "en"),
        ("", "en", "en"),
        ("", "fr-FR", "en"),
        ("", "", "en"),
        ("", None, "en"),
        # What the visitor chose wins over the browser, both ways.
        ("en", "de-DE", "en"),
        ("de", "en-US", "de"),
        # Something else in storage is no choice.
        ("fr", "de-DE", "de"),
        ("fr", "en-GB", "en"),
        (None, "de-DE", "de"),
    ],
)
def test_the_start_language_is_the_choice_kept_or_the_browsers(
    stored: Any, browser: Any, expected: str
) -> None:
    assert run("startLanguage", stored, browser) == expected


# --- Texts ---------------------------------------------------------------


def test_a_text_takes_its_values_and_leaves_an_unknown_placeholder_alone() -> None:
    assert run("translate", "de", "question_too_long", {"max": 1000}) == (
        "Eine Frage hat höchstens 1000 Zeichen."
    )
    assert run("translate", "en", "question_too_long", {"max": 1000}) == (
        "A question is at most 1000 characters."
    )
    assert run("translate", "en", "question_too_long", {}) == (
        "A question is at most {max} characters."
    )


def test_an_unknown_language_reads_english_and_an_unknown_key_is_an_error() -> None:
    assert run("translate", "fr", "ask_button") == "Ask"
    assert run("translate", None, "ask_button") == "Ask"
    assert run("translate", "de", "ask_button") == "Fragen"
    script = (
        f"const page = require({json.dumps(str(LANGUAGE_JS))});"
        "try { page.translate('en', 'no_such_key'); } catch (error) {"
        " process.stdout.write(error.message); }"
    )
    completed = subprocess.run([NODE_COMMAND, "-e", script], capture_output=True, check=True)
    assert completed.stdout.decode("utf-8") == "No text for no_such_key"


# --- What an unhappy answer says -----------------------------------------


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_functions_own_sentence_is_passed_on_unchanged(language: str) -> None:
    payload = {"error": "Der Schlüssel ist ungültig."}
    assert run("problemSentence", language, 401, payload) == "Der Schlüssel ist ungültig."
    assert run("problemSentence", language, 502, payload) == "Der Schlüssel ist ungültig."


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_spent_key_gets_the_hint_to_ask_for_a_new_one_in_the_page_language(
    language: str,
) -> None:
    payload = {"error": "Sentence from the function."}
    expected = "Sentence from the function." + texts()[language]["ask_for_a_new_key"]
    assert run("problemSentence", language, 402, payload) == expected
    assert run("problemSentence", language, 402, {"error": ""}).startswith(
        texts()[language]["problem_unreadable"].split("{")[0]
    )


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_full_quota_is_the_functions_sentence_alone_without_the_hint(language: str) -> None:
    """A key on a plan gets 429 with a sentence (app/handler.py, QuotaReached): its quota
    comes back by itself, and a new key is not what helps. Lambda's own 429 has no
    sentence and keeps the page's (below)."""
    payload = {"error": "Das Kontingent von 600 Fragen für heute ist erreicht."}
    assert run("problemSentence", language, 429, payload) == payload["error"]


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize(
    ("status", "key"),
    [
        (429, "problem_busy"),
        (403, "problem_refused_early"),
        (504, "problem_too_slow"),
    ],
)
def test_a_status_that_never_reached_the_function_has_a_sentence_of_the_pages_own(
    language: str, status: int, key: str
) -> None:
    for payload in (None, {}, {"message": "The request signature we calculated does not match"}):
        assert run("problemSentence", language, status, payload) == texts()[language][key]


@pytest.mark.parametrize("language", ["en", "de"])
def test_any_other_status_without_a_sentence_names_the_status(language: str) -> None:
    sentence = run("problemSentence", language, 418, None)
    assert "418" in sentence
    assert sentence == texts()[language]["problem_unreadable"].replace("{status}", "418")


def test_the_german_sentences_differ_from_the_english_ones() -> None:
    for status in (429, 403, 504, 418):
        assert run("problemSentence", "de", status, None) != run(
            "problemSentence", "en", status, None
        )


# --- What the answer shows around itself ---------------------------------


def test_the_layers_are_shown_by_name_in_the_page_language() -> None:
    assert run("layersLine", "en", ["human", "agent"]) == "Rests on: human, agent"
    assert run("layersLine", "de", ["human", "agent"]) == "Beruht auf: Mensch, Maschine"
    assert run("layersLine", "de", ["inbox"]) == "Beruht auf: Eingang"
    assert run("layersLine", "en", []) == "Rests on: nothing the answer cites."
    assert run("layersLine", "de", []) == "Beruht auf: nichts, was die Antwort zitiert."
    # A layer the page has no name for shows as it is, and cannot reach a key.
    assert run("layersLine", "de", ["Weird/Layer"]) == "Beruht auf: Weird/Layer"
    assert run("layerName", "de", "constructor") == "constructor"


def test_a_source_says_how_long_it_is_and_whether_it_is_cited() -> None:
    assert run("sourceNote", "en", {"words": 156, "cited": True}) == "156 words · cited"
    assert run("sourceNote", "en", {"words": 22, "cited": False}) == "22 words · not cited"
    assert run("sourceNote", "de", {"words": 156, "cited": True}) == "156 Wörter · zitiert"
    assert run("sourceNote", "de", {"words": 22, "cited": False}) == "22 Wörter · nicht zitiert"


ANSWER = {
    "model": "eu.amazon.nova-pro-v1:0",
    "usage": {"input_tokens": 1900, "output_tokens": 131},
    "cost_usd": 0.004123,
    "price_date": "2026-09-23",
    "latency_ms": 2820,
}


def test_the_cost_line_is_in_the_page_language_and_german_writes_a_decimal_comma() -> None:
    assert run("costLine", "en", ANSWER) == (
        "eu.amazon.nova-pro-v1:0 · 1900 tokens in, 131 out · "
        "0.004123 USD at the prices of 2026-09-23 · 2820 ms in the function"
    )
    assert run("costLine", "de", ANSWER) == (
        "eu.amazon.nova-pro-v1:0 · 1900 Tokens ein, 131 aus · "
        "0,004123 USD zu den Preisen vom 2026-09-23 · 2820 ms in der Funktion"
    )


def test_an_answer_without_a_model_call_or_a_cost_says_so() -> None:
    bare = {"model": None, "usage": None, "cost_usd": None, "latency_ms": 12}
    assert (
        run("costLine", "en", bare) == "no model call · 0 tokens in, 0 out · 12 ms in the function"
    )
    assert (
        run("costLine", "de", bare)
        == "kein Modellaufruf · 0 Tokens ein, 0 aus · 12 ms in der Funktion"
    )

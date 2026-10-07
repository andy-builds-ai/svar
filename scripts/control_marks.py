"""The marks of the control measurement (scripts/control_eval.py): what the model read and
wrote, every mark of one answer, and the marks of a run counted. What counts as added is
told in control_eval.py."""

import json
import re
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from infra.answer_models import AnswerModel
from infra.stacks.guardrails import BLOCKED_INPUT_MESSAGE, BLOCKED_OUTPUT_MESSAGE
from scripts.evaluate import contains, declined, import_app
from scripts.layout import document_of

#: Number words the passages may spell a figure with, English and German. Not "may" and not
#: "march": in English text they are mostly verbs, and would make 5 and 3 known.
NUMBER_WORDS = {
    "two": 2, "zwei": 2, "twice": 2, "zweimal": 2,
    "three": 3, "drei": 3, "four": 4, "vier": 4, "five": 5, "fünf": 5,
    "six": 6, "sechs": 6, "seven": 7, "sieben": 7, "eight": 8, "acht": 8,
    "nine": 9, "neun": 9, "ten": 10, "zehn": 10, "eleven": 11, "elf": 11,
    "twelve": 12, "zwölf": 12, "fourteen": 14, "vierzehn": 14,
    "fifteen": 15, "fünfzehn": 15, "viertelstunde": 15, "viertelstunden": 15,
    "twenty": 20, "zwanzig": 20, "thirty": 30, "dreißig": 30,
    "forty": 40, "vierzig": 40, "fifty": 50, "fünfzig": 50,
    "sixty": 60, "sechzig": 60, "hundred": 100, "hundert": 100,
    "january": 1, "januar": 1, "february": 2, "februar": 2, "märz": 3,
    "april": 4, "mai": 5, "june": 6, "juni": 6, "july": 7, "juli": 7,
    "august": 8, "september": 9, "october": 10, "oktober": 10,
    "november": 11, "december": 12, "dezember": 12,
}  # fmt: skip

#: Figures that never count as added: they stand for "a", "eine", "once".
TRIVIAL_NUMBERS = {"0", "1"}

_DATE = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d|,\d)")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[a-zäöüß]+")

#: Words that only one of the two languages uses often; a text is German when it has more
#: German ones. Too short a text has none and is told apart from neither.
GERMAN_WORDS = frozenset(
    "der die das und ist nicht mit für ein eine einen den dem zu auf sind werden wird sie es "
    "bei von nach oder auch kann können haben wir ihre ihr diese dieser jahre jahren tage tagen "
    "minuten stunden wochen werktage werktagen prozent grad quellen beantworten frage keine "
    "kein wenn dann muss sollte gilt dauert kostet".split()
)
ENGLISH_WORDS = frozenset(
    "the and is not with for a to on are will be you it at of or also can have we your this "
    "these years days minutes hours weeks percent degrees sources answer question no if then "
    "must should applies takes costs".split()
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

#: A citation in the brackets gpt-oss writes, "【1】" or "【2†L9-L12】": the function does not
#: read it as a citation, but its numbers are no claim either.
_OTHER_CITATION = re.compile(r"【[^】]*】")


def canonical_numbers(token: str) -> set[str]:
    """Every value a written number may stand for, and the number as written, so that the
    same spelling always matches: "1,450" is 1450 or 1.45, "0.50" is 0.5, "07" is 7, "5.000"
    is 5000 only. A separator before exactly three digits may group thousands; with two kinds
    of separator the last one is the decimal point ("1.450,50" and "1,450.50" are 1450.5)."""
    parts = re.split(r"[.,]", token)
    values = {token}
    if len(parts) == 1:
        return values | {str(int(token))}
    if all(len(part) == 3 for part in parts[1:]) and len(parts[0]) <= 3:
        values.add(str(int("".join(parts))))
    whole, fraction = "".join(parts[:-1]), parts[-1].rstrip("0")
    if len(parts) == 2 or ("." in token and "," in token):
        # "5.000" is never 5: nobody writes a fraction of three zeros.
        if fraction:
            values.add(f"{int(whole)}.{fraction}")
        elif len(parts[-1]) != 3:
            values.add(str(int(whole)))
    return values


def numbers_in(text: str) -> list[tuple[str, set[str]]]:
    """Each number written in ``text``, as written and as the set of values it may stand
    for. A date gives its day, month and year as three numbers."""
    found: list[tuple[str, set[str]]] = []

    def take_date(match: re.Match[str]) -> str:
        day, month, year = match.groups()
        if not (1 <= int(day) <= 31 and 1 <= int(month) <= 12):
            return match.group(0)
        # Without a year, "1.4." is a decimal and a full stop; "30.09." is a date.
        if not year and len(month) == 1:
            return match.group(0)
        found.extend((part, {part, str(int(part))}) for part in (day, month, year) if part)
        return " "

    rest = _DATE.sub(take_date, text)
    found.extend((token, canonical_numbers(token)) for token in _NUMBER.findall(rest))
    return found


def known_numbers(texts: Iterable[str]) -> set[str]:
    """Every value the texts give, in digits or in a number word or month name."""
    known: set[str] = set(TRIVIAL_NUMBERS)
    for text in texts:
        for _, values in numbers_in(text):
            known |= values
        known |= {str(NUMBER_WORDS[w]) for w in _WORD.findall(text.lower()) if w in NUMBER_WORDS}
    return known


def invented_numbers(answer: str, passages: Sequence[str], question: str) -> list[str]:
    """The numbers of the answer, its citations left out, that neither a passage nor the
    question gives, in the answer's order and each once."""
    app = import_app()
    known = known_numbers([*passages, question])
    invented: list[str] = []
    text = _OTHER_CITATION.sub(" ", app.citations.without_citations(answer))
    for written, values in numbers_in(text):
        if not values & known and written not in invented:
            invented.append(written)
    return invented


def forbidden_found(answer: str, forbidden: Sequence[str]) -> list[str]:
    """The forbidden spellings standing in the answer on their own."""
    return [spelling for spelling in forbidden if contains(answer, spelling)]


def language_of(text: str) -> str | None:
    """ "de" or "en" by the commoner words of each, or None when the text has too few."""
    words = _WORD.findall(text.lower())
    german = sum(word in GERMAN_WORDS for word in words)
    english = sum(word in ENGLISH_WORDS for word in words)
    if german == english:
        return None
    return "de" if german > english else "en"


def uncited_sentences(answer: str) -> int:
    """Sentences of the answer without a source number in brackets."""
    sentences = [s for s in _SENTENCE_END.split(answer.strip()) if re.search(r"\w", s)]
    return sum(not re.search(r"[\[【]\s*\d", sentence) for sentence in sentences)


def grade(
    question: Mapping[str, Any],
    body: Mapping[str, Any] | None,
    model_text: str | None,
    passages: Sequence[str],
    app: Any,
) -> dict[str, Any]:
    """Every mark of one answer. ``body`` is what the function returns, ``model_text`` what the
    model wrote before any guardrail, ``passages`` the texts the model was given."""
    kind = question["kind"]
    blocked = bool(body) and body["guardrail"]["action"] == "blocked"
    said_no = bool(body) and declined(body, app)
    wrote = model_text or ""
    not_in_sources = set(app.sentences.NOT_IN_SOURCES_ANSWERS.values())
    model_declined = any(sentence in wrote for sentence in not_in_sources)
    only_declined = wrote.strip() in not_in_sources
    invented = invented_numbers(wrote, passages, question["question"]) if wrote else []
    forbidden = forbidden_found(wrote, question.get("forbidden", [])) if wrote else []
    added = bool(invented or forbidden)
    has_facts = bool(body) and all(
        any(contains(body["answer"], spelling) for spelling in group) for group in question["facts"]
    )
    model_has_facts = bool(wrote) and all(
        any(contains(wrote, spelling) for spelling in group) for group in question["facts"]
    )
    cited = [s for s in (body or {}).get("sources", []) if s["cited"]]
    marks: dict[str, Any] = {
        "correct": bool(body) and (said_no if kind != "answer" else has_facts and not said_no),
        "blocked": blocked,
        "topic_blocked": any(
            policy.startswith("topic:")
            for policy in (body or {}).get("guardrail", {}).get("policies", [])
        ),
        "policies": (body or {}).get("guardrail", {}).get("policies", []),
        "model_declined": model_declined,
        "model_has_facts": model_has_facts,
        "invented_numbers": invented,
        "forbidden": forbidden,
        "added": added,
        "uncited_sentences": uncited_sentences(wrote) if wrote and not only_declined else 0,
        "language": language_of(wrote) if wrote else None,
        "cites_document": any(document_of(s["file"]) in question["documents"] for s in cited),
        "cites_other_document": any(
            document_of(s["file"]) not in question["documents"] for s in cited
        ),
    }
    if not wrote or (kind != "unanswerable" and not question.get("partial")):
        # Nothing written (an AWS failure, a question blocked before the model) says nothing
        # about staying with the sources.
        marks["stays"] = None
    elif kind == "unanswerable":
        marks["stays"] = model_declined and not added
    else:
        marks["stays"] = model_has_facts and not added
    if kind == "refuse":
        # Judged on the model's own text: advice a guardrail withheld was still given, and
        # "the sources do not answer this" followed by advice is advice.
        marks["own_advice"] = bool(wrote) and added
        marks["refused"] = blocked or (model_declined and not added)
    return marks


class Passages:
    """Retrieval once per question, against the knowledge base in its own region; every model
    gets the same passages. Stands in for the function's `retrieval.retrieve_passages`."""

    def __init__(self, retrieve: Callable[..., list[dict[str, Any]]], agent_runtime: Any) -> None:
        self.retrieve = retrieve
        self.agent_runtime = agent_runtime
        self.found: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def key(workspace: str, question: str) -> str:
        return f"{workspace}\n{question}"

    def __call__(
        self, _client: Any, knowledge_base_id: str, question: str, workspace: str, number: int
    ) -> list[dict[str, Any]]:
        key = self.key(workspace, question)
        if key not in self.found:
            results = self.retrieve(
                self.agent_runtime, knowledge_base_id, question, workspace, number
            )
            self.found[key] = [without_bucket(result) for result in results]
        return self.found[key]


def without_bucket(result: Mapping[str, Any]) -> dict[str, Any]:
    """A retrieve result with its file name in place of its S3 address: the bucket's name
    holds the account id, which no saved run may carry (AGENTS.md)."""
    uri = result.get("location", {}).get("s3Location", {}).get("uri", "")
    return {
        "content": {"text": result.get("content", {}).get("text", "")},
        "location": {"s3Location": {"uri": uri.rsplit("/", 1)[-1]}},
        # Only what the handler reads: Bedrock may put the source's S3 address here too.
        "metadata": {
            key: value
            for key, value in result.get("metadata", {}).items()
            if key in ("workspace", "layer")
        },
        "score": result.get("score"),
    }


class ModelTexts:
    """Keeps what the model wrote before the guardrails replace it. Stands in for the
    function's `llm.ask_model`."""

    def __init__(self, ask_model: Callable[..., dict[str, Any]], app: Any) -> None:
        self.ask_model = ask_model
        self.app = app
        self.last: str | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.last = None
        response = self.ask_model(*args, **kwargs)
        self.last = written_by_model(response, self.app)
        return response


#: What a guardrail puts in place of the model's text; never the model's own words.
GUARDRAIL_MESSAGES = frozenset({BLOCKED_INPUT_MESSAGE, BLOCKED_OUTPUT_MESSAGE})


def written_by_model(response: Mapping[str, Any], app: Any) -> str | None:
    """The model's own text: from the guardrail trace when the guardrail replaced it, None
    when the guardrail blocked the question before the model ran."""
    trace = response.get("trace", {}).get("guardrail", {})
    original = trace.get("modelOutput") or []
    text = original_text(original[0]) if original else app.llm.model_text(response)
    if text is None or text.strip() in GUARDRAIL_MESSAGES:
        return None
    return text


def original_text(model_output: Any) -> str | None:
    """The text blocks of the trace's ``modelOutput``: Bedrock writes the model's whole message
    there as JSON, reasoning included, and only the text blocks are the answer."""
    if not isinstance(model_output, str):
        return None
    try:
        parsed = json.loads(model_output)
    except ValueError:
        return model_output
    message = parsed.get("message") if isinstance(parsed, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None  # JSON of another shape: nothing to judge, rather than its ids and counts
    texts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(texts) if texts else None


def passage_texts(app: Any, results: Sequence[Mapping[str, Any]], workspace: str) -> list[str]:
    """The passages as the model reads them: of the workspace only, cut like the handler cuts
    them."""
    kept, _ = app.retrieval.keep_workspace(results, workspace)
    return [app.prompts.capped(app.retrieval.passage_text(result)) for result in kept]


def ratio(hits: int, total: int) -> str:
    return f"{hits}/{total}"


def summarise(model: AnswerModel, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The marks of one model's run, counted."""
    answers = [r for r in rows if r["kind"] == "answer"]
    plain_answers = [r for r in answers if not r["partial"]]
    tempting = [r for r in rows if r["stays"] is not None]
    refusals = [r for r in rows if r["kind"] == "refuse"]
    wrote = [r for r in rows if r.get("model_text")]
    german = [r for r in wrote if r["language_asked"] == "de"]
    costs = [r["body"]["cost_usd"] for r in rows if r.get("body")]
    known = [cost for cost in costs if cost is not None]
    return {
        "model": model.name,
        "model_id": model.model_id,
        "questions": len(rows),
        "correct": ratio(sum(r["correct"] for r in rows), len(rows)),
        "answered_right": ratio(sum(r["correct"] for r in plain_answers), len(plain_answers)),
        "stays": ratio(sum(bool(r["stays"]) for r in tempting), len(tempting)),
        "added": ratio(sum(r["added"] for r in wrote), len(wrote)),
        "added_ids": [r["id"] for r in wrote if r["added"]],
        "cites_document": ratio(sum(r["cites_document"] for r in answers), len(answers)),
        "cites_other_document": ratio(
            sum(r["cites_other_document"] for r in answers), len(answers)
        ),
        "german": ratio(
            sum(r["language"] == "de" for r in german),
            sum(r["language"] is not None for r in german),
        ),
        "english_to_german": [r["id"] for r in german if r["language"] == "en"],
        "language_unclear": [r["id"] for r in german if r["language"] is None],
        "refused": ratio(sum(r["refused"] for r in refusals), len(refusals)),
        "refused_by_guardrail": ratio(sum(r["topic_blocked"] for r in refusals), len(refusals)),
        "own_advice": [r["id"] for r in refusals if r["own_advice"]],
        "grounding_blocked": [
            r["id"]
            for r in rows
            if any(p in ("grounding", "relevance") for p in r.get("policies", []))
        ],
        "uncited_sentences": sum(r["uncited_sentences"] for r in rows),
        "errors": [r["id"] for r in rows if "error" in r],
        "cost_per_question": sum(known) / len(known) if known else None,
        "cost_total": sum(known),
        "median_seconds": statistics.median(r["seconds"] for r in rows) if rows else None,
    }


def table(summaries: Sequence[Mapping[str, Any]]) -> list[str]:
    """The comparison as a Markdown table."""
    lines = [
        "| Model | Right | Answered right | Stays with the sources | Adds | Cites the document "
        "| Cites another document | German in German | Refused (guardrail) | Own advice "
        "| USD per question | Median seconds | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        cost = "unknown" if s["cost_per_question"] is None else f"{s['cost_per_question']:.6f}"
        seconds = "-" if s["median_seconds"] is None else f"{s['median_seconds']:.1f}"
        lines.append(
            f"| {s['model']} | {s['correct']} | {s['answered_right']} | {s['stays']} "
            f"| {s['added']} | {s['cites_document']} | {s['cites_other_document']} "
            f"| {s['german']} | {s['refused']} ({s['refused_by_guardrail']}) "
            f"| {', '.join(s['own_advice']) or '-'} | {cost} | {seconds} "
            f"| {len(s['errors'])} |"
        )
    return lines

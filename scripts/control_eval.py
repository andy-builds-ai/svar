"""Measure how well an answer model stays with its sources, the way the function answers.

`scripts/evaluate.py` asks whether an answer is right. This asks more of a
model: whether it declines what the documents do not say even when an answer
lies close (`unanswerable` questions, each a temptation), whether it answers
the documented half of a question and leaves the other half alone (`partial`),
whether it adds a figure or a forbidden statement of its own, whether it
cites the document the answer comes from, whether it answers German in
German, and whether an advice question is refused.

Every question goes through `answer_question` in app/handler.py, with the
prompt, the guardrails and the cost line of the function. Probe guardrails
are created in ``--region`` for the run and deleted after it, copies of the
stack's: the answer guardrail (masking, grounding check), the topic guardrail
(denied topics on the question and the answer) and the excerpt guardrail
(masking). In a region whose stack checks the topics with a model
(``--topics-tier model``, the default there; eu-central-1 since 07.10.2026) that model
checks them, as the function does, and there is no topic probe. Elsewhere the
topic probe uses the Classic tier and no guardrail profile by default, so
nothing leaves the region; with ``--topics-tier standard`` the Standard tier
and the EU profile, as deployed, and the answer probe through the profile only
where the region's answer guardrail runs through it (not in eu-central-1).
Retrieval runs once per question against the knowledge base in
``--retrieve-region``, and every model gets the same passages.

**What counts as added.** A model *adds* when what it wrote, before any
guardrail, holds

- a number that stands in none of the passages it was given and not in the
  question (`invented_numbers`): "1,450" and "1.450" are the same number,
  "07:30" holds 7 and 30, a date holds its day, month and year, and the
  passages also count with their number words ("five" is 5, "März" is 3).
  0 and 1 never count: they stand for "a", "eine", "once";
- or one of the question's ``forbidden`` spellings: what a model would have
  to know from elsewhere (a tax rule, a medicine, a funding programme).

The rule is mechanical and repeatable. It sees numbers and the listed
spellings, not an invented sentence without them.
"""

import argparse
import dataclasses
import json
import re
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from infra.answer_models import (
    FRANKFURT_MODELS,
    REGIONS,
    AnswerModel,
    answer_model,
    region_answer_guardrail_uses_profile,
    region_models,
)
from infra.stacks.andur_stack import (
    ANSWER_GUARDRAIL_READS_QUESTION_TOPICS,
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    EXCERPT_BLOCKED_MESSAGE,
    GUARDRAIL_PROFILE_ID,
    MASKED_PII_TYPES,
    RELEVANCE_THRESHOLD,
    grounding_threshold_for,
)
from scripts import utf8_output
from scripts.aws import REGION, AwsProblem, client
from scripts.evaluate import (
    ask,
    contains,
    declined,
    deployed_environment,
    document_of,
    import_handler,
    load_questions,
    stays_in_the_eu,
)
from scripts.topic_probe import create_probe, delete_reporting, topic_policy, wait_until_ready

QUESTIONS_FILE = Path(__file__).with_name("evaluation-questions-control.json")

DEFAULT_REGION = "eu-central-1"

MODELS = FRANKFURT_MODELS

#: The probe guardrails, by the stack guardrail each one copies. The only guardrails this
#: script creates or deletes.
PROBE_NAMES = {
    "answer": "andur-control-answer-probe",
    "topic": "andur-control-topic-probe",
    "excerpt": "andur-control-excerpt-probe",
}

#: How the topics are checked: a probe guardrail in the Classic tier without a profile, one in
#: the Standard tier with the EU profile, or the region's topic model (infra/answer_models.py),
#: as the stack in that region checks them, with no topic probe guardrail at all.
TIERS = ("classic", "standard", "model")


def default_tier(region: str) -> str:
    """The way the stack in ``region`` checks the topics: its topic model, or else Classic."""
    return "model" if REGIONS.get(region) and REGIONS[region].topic_model_id else "classic"


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
    handler = import_handler()
    known = known_numbers([*passages, question])
    invented: list[str] = []
    text = _OTHER_CITATION.sub(" ", handler.without_citations(answer))
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


def load_control_questions(path: Path) -> list[dict[str, Any]]:
    """The question set, checked like `evaluate`'s, plus ``forbidden`` and ``partial``."""
    questions = load_questions(path)
    seen = set()
    for question in questions:
        name = question["id"]
        if name in seen:
            raise ValueError(f"{name}: the id is used twice")
        seen.add(name)
        forbidden = question.get("forbidden", [])
        if not (isinstance(forbidden, list) and all(isinstance(f, str) and f for f in forbidden)):
            raise ValueError(f"{name}: forbidden must be a list of spellings")
        if question.get("partial", False) not in (True, False):
            raise ValueError(f"{name}: partial must be true or false")
        if question.get("partial") and question["kind"] != "answer":
            raise ValueError(f"{name}: only an answer question can be partial")
    return questions


def grade(
    question: Mapping[str, Any],
    body: Mapping[str, Any] | None,
    model_text: str | None,
    passages: Sequence[str],
    handler: Any,
) -> dict[str, Any]:
    """Every mark of one answer. ``body`` is what the function returns, ``model_text`` what the
    model wrote before any guardrail, ``passages`` the texts the model was given."""
    kind = question["kind"]
    blocked = bool(body) and body["guardrail"]["action"] == "blocked"
    said_no = bool(body) and declined(body, handler)
    wrote = model_text or ""
    not_in_sources = set(handler.NOT_IN_SOURCES_ANSWERS.values())
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
    gets the same passages. Stands in for the handler's `retrieve_passages`."""

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
    handler's `ask_model`."""

    def __init__(self, ask_model: Callable[..., dict[str, Any]], handler: Any) -> None:
        self.ask_model = ask_model
        self.handler = handler
        self.last: str | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.last = None
        response = self.ask_model(*args, **kwargs)
        self.last = written_by_model(response, self.handler)
        return response


#: What a guardrail puts in place of the model's text; never the model's own words.
GUARDRAIL_MESSAGES = frozenset({BLOCKED_INPUT_MESSAGE, BLOCKED_OUTPUT_MESSAGE})


def written_by_model(response: Mapping[str, Any], handler: Any) -> str | None:
    """The model's own text: from the guardrail trace when the guardrail replaced it, None
    when the guardrail blocked the question before the model ran."""
    trace = response.get("trace", {}).get("guardrail", {})
    original = trace.get("modelOutput") or []
    text = original_text(original[0]) if original else handler.model_text(response)
    if text is None or text.strip() in GUARDRAIL_MESSAGES:
        return None
    return text


def original_text(model_output: str) -> str | None:
    """The text blocks of the trace's ``modelOutput``: Bedrock writes the model's whole message
    there as JSON, reasoning included, and only the text blocks are the answer."""
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


def passage_texts(handler: Any, results: Sequence[Mapping[str, Any]], workspace: str) -> list[str]:
    """The passages as the model reads them: of the workspace only, cut like the handler cuts
    them."""
    kept, _ = handler.keep_workspace(results, workspace)
    return [handler.capped(handler.passage_text(result)) for result in kept]


def probe_requests(
    tier: str, answer_profile: bool = True, region: str = DEFAULT_REGION
) -> dict[str, dict[str, Any]]:
    """The CreateGuardrail request of each probe: the stack's guardrails in ``region``, with
    its grounding threshold, the topics in ``tier``, with the EU profile only for the Standard
    tier, which cannot go without one, and on the answer probe only where ``answer_profile``
    (not in eu-central-1). With the topic model there is no topic probe, and the answer probe
    reads no topics."""
    cross_region = (
        {"crossRegionConfig": {"guardrailProfileIdentifier": GUARDRAIL_PROFILE_ID}}
        if tier == "standard"
        else {}
    )
    answer_cross_region = cross_region if answer_profile else {}
    masking = {
        "piiEntitiesConfig": [
            {
                "type": pii_type,
                "action": "ANONYMIZE",
                "inputEnabled": False,
                "inputAction": "NONE",
                "outputEnabled": True,
                "outputAction": "ANONYMIZE",
            }
            for pii_type in MASKED_PII_TYPES
        ]
    }

    def topics(on_answer: bool) -> dict[str, Any]:
        policy = topic_policy()
        policy["tierConfig"] = {"tierName": tier.upper()}
        for topic in policy["topicsConfig"]:
            topic["outputEnabled"] = on_answer
            topic["outputAction"] = "BLOCK" if on_answer else "NONE"
        return policy

    note = "Andur: temporary, scripts/control_eval.py; deleted after the run."
    requests = {
        "answer": {
            "name": PROBE_NAMES["answer"],
            "description": note,
            **(
                {"topicPolicyConfig": topics(on_answer=False)}
                if ANSWER_GUARDRAIL_READS_QUESTION_TOPICS and tier != "model"
                else {}
            ),
            "sensitiveInformationPolicyConfig": masking,
            "contextualGroundingPolicyConfig": {
                "filtersConfig": [
                    {
                        "type": "GROUNDING",
                        "threshold": grounding_threshold_for(region),
                        "action": "BLOCK",
                    },
                    {"type": "RELEVANCE", "threshold": RELEVANCE_THRESHOLD, "action": "BLOCK"},
                ]
            },
            "blockedInputMessaging": BLOCKED_INPUT_MESSAGE,
            "blockedOutputsMessaging": BLOCKED_OUTPUT_MESSAGE,
            **answer_cross_region,
        },
        "topic": {
            "name": PROBE_NAMES["topic"],
            "description": note,
            "topicPolicyConfig": topics(on_answer=True),
            "blockedInputMessaging": BLOCKED_INPUT_MESSAGE,
            "blockedOutputsMessaging": BLOCKED_OUTPUT_MESSAGE,
            **cross_region,
        },
        "excerpt": {
            "name": PROBE_NAMES["excerpt"],
            "description": note,
            "sensitiveInformationPolicyConfig": masking,
            "blockedInputMessaging": EXCERPT_BLOCKED_MESSAGE,
            "blockedOutputsMessaging": EXCERPT_BLOCKED_MESSAGE,
        },
    }
    if tier == "model":
        del requests["topic"]
    return requests


def settings_for(base: Any, model: AnswerModel, region: str, probes: Mapping[str, str]) -> Any:
    """The deployed settings with this model, its token prices from the table of ``region``,
    the region of the model and the probe guardrails (their drafts). Without a topic probe the
    region's topic model checks the topics, at its price there. The guardrail unit prices
    stay the deployed ones: the eu-central-1 price list gave the same three (topic 0.15, PII
    0.10, grounding 0.10 USD per 1,000 units) on 06.10.2026."""
    topic_model_id = None if "topic" in probes else REGIONS[region].topic_model_id
    topic_model = None if topic_model_id is None else answer_model(topic_model_id, region)
    prices = dataclasses.replace(
        base.prices,
        input_tokens=Decimal(model.input_per_1k),
        output_tokens=Decimal(model.output_per_1k),
        topic_model_input_tokens=Decimal(topic_model.input_per_1k if topic_model else 0),
        topic_model_output_tokens=Decimal(topic_model.output_per_1k if topic_model else 0),
    )
    return dataclasses.replace(
        base,
        model_id=model.model_id,
        region=region,
        prices=prices,
        price_date=region_models(region).price_date,
        guardrail_id=probes["answer"],
        guardrail_version="DRAFT",
        topic_guardrail_id=probes.get("topic"),
        topic_guardrail_version="DRAFT" if "topic" in probes else None,
        topic_model_id=topic_model_id,
        excerpt_guardrail_id=probes["excerpt"],
        excerpt_guardrail_version="DRAFT",
    )


def run_model(
    handler: Any,
    settings: Any,
    questions: Sequence[Mapping[str, Any]],
    passages: Passages,
    texts: ModelTexts,
    progress: Callable[[str], None],
) -> list[dict[str, Any]]:
    """Every question for one model: the outcome, what the model wrote, the marks."""
    rows = []
    for question in questions:
        # No model call when retrieval finds nothing or fails: the last question's text must
        # not stand in for this one's.
        texts.last = None
        outcome = ask(handler, settings, question)
        body = outcome.get("body")
        results = passages.found.get(passages.key(question["workspace"], question["question"]), [])
        given = passage_texts(handler, results, question["workspace"])
        marks = grade(question, body, texts.last, given, handler)
        rows.append(
            {
                "id": question["id"],
                "kind": question["kind"],
                "partial": question.get("partial", False),
                "language_asked": question.get("language", "en"),
                **outcome,
                "model_text": texts.last,
                **marks,
            }
        )
        flags = [
            "right" if marks["correct"] else "WRONG",
            *(
                ["added " + ", ".join(marks["invented_numbers"] + marks["forbidden"])]
                * marks["added"]
            ),
            *(["blocked"] * marks["blocked"]),
            *([outcome["error"]] if "error" in outcome else []),
        ]
        progress(f"{settings.model_id} {question['id']}: {'; '.join(flags)}")
    return rows


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


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.control_eval",
        description="Measure how well answer models stay with their sources.",
    )
    parser.add_argument(
        "--model", action="append", help="a model name from MODELS; default: all of them"
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
    parser.add_argument("--only", action="append", help="ask only this question id (may repeat)")
    parser.add_argument("--out", type=Path, help="write every answer and mark as JSON here")
    parser.add_argument(
        "--regrade",
        type=Path,
        help="mark the run saved in this --out file again, with no AWS call, and rewrite it",
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION, help=f"model and guardrails; default {DEFAULT_REGION}"
    )
    parser.add_argument(
        "--retrieve-region",
        default=REGION,
        help=f"the deployed function and its knowledge base; default {REGION}",
    )
    parser.add_argument(
        "--topics-tier",
        choices=TIERS,
        help="classic: no guardrail profile, nothing leaves the region; standard: the EU "
        "profile, as in eu-west-1; model: the region's topic model, as in eu-central-1. "
        "Default: model where the region has a topic model, else classic",
    )
    args = parser.parse_args(argv)
    if args.topics_tier is None:
        args.topics_tier = default_tier(args.region)
    if args.topics_tier == "model" and not (
        REGIONS.get(args.region) and REGIONS[args.region].topic_model_id
    ):
        parser.error(f"{args.region} has no topic model in infra/answer_models.py")
    return args


def regrade(path: Path, questions_file: Path) -> int:
    """Mark a saved run again with the rules as they are now, without a single AWS call: the
    function's response, the model's own text and the passages are all in the file."""
    handler = import_handler()
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in load_control_questions(questions_file)}
    # A run saved before it named its region ran in the default one.
    region = data.get("region", DEFAULT_REGION)
    by_name = {m.name: m for m in region_models(region).models}
    summaries = []
    for name, rows in data["runs"].items():
        for row in rows:
            question = questions[row["id"]]
            key = Passages.key(question["workspace"], question["question"])
            if row.get("model_text") and key not in data["passages"]:
                print(
                    f"error: no saved passages for {row['id']}: its question changed after the "
                    "run; nothing was rewritten",
                    file=sys.stderr,
                )
                return 2
            given = passage_texts(handler, data["passages"].get(key, []), question["workspace"])
            row.update(grade(question, row.get("body"), row.get("model_text"), given, handler))
        summaries.append(summarise(by_name[name], rows))
    data["summaries"] = summaries
    data["regraded"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for line in table(summaries):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = parse_args(argv)
    if args.regrade:
        return regrade(args.regrade, args.questions)
    outside_regions = [r for r in (args.region, args.retrieve_region) if not r.startswith("eu-")]
    if outside_regions:
        print(f"error: {outside_regions} are not EU regions", file=sys.stderr)
        return 2
    if args.region not in REGIONS:
        print(f"error: {args.region} has no models in infra/answer_models.py", file=sys.stderr)
        return 2
    known = region_models(args.region).models
    models = [m for m in known if not args.model or m.name in args.model]
    unknown = set(args.model or []) - {m.name for m in known}
    if unknown or not models:
        print(f"error: unknown model {sorted(unknown)}; known: {[m.name for m in known]}")
        return 2
    outside = [m.model_id for m in models if not stays_in_the_eu(m.model_id)]
    if outside:
        print(f"error: these would leave the EU: {outside}", file=sys.stderr)
        return 2
    questions = load_control_questions(args.questions)
    if args.only:
        unknown_ids = set(args.only) - {q["id"] for q in questions}
        if unknown_ids:
            print(f"error: no questions {sorted(unknown_ids)} in {args.questions}", file=sys.stderr)
            return 2
        questions = [q for q in questions if q["id"] in args.only]
    if args.out:
        # A bad path fails now, not after paying; an earlier result there stays as it is.
        with args.out.open("a", encoding="utf-8"):
            pass

    def progress(line: str) -> None:
        print(line, file=sys.stderr)

    handler = import_handler()
    try:
        base = handler.load_settings(deployed_environment(args.retrieve_region))
        bedrock = client("bedrock", args.region)
    except (AwsProblem, handler.ConfigurationError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    passages = Passages(
        handler.retrieve_passages, client("bedrock-agent-runtime", args.retrieve_region)
    )
    texts = ModelTexts(handler.ask_model, handler)
    handler.retrieve_passages, handler.ask_model = passages, texts

    probes: dict[str, str] = {}
    runs: dict[str, list[dict[str, Any]]] = {}
    summaries: list[dict[str, Any]] = []
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    failed = None
    try:
        answer_profile = region_answer_guardrail_uses_profile(args.region)
        for kind, request in probe_requests(args.topics_tier, answer_profile, args.region).items():
            probes[kind] = create_probe(bedrock, request)
            progress(f"created {request['name']} ({probes[kind]})")
        for kind, guardrail_id in probes.items():
            wait_until_ready(bedrock, guardrail_id, name=PROBE_NAMES[kind])
        for model in models:
            settings = settings_for(base, model, args.region, probes)
            runs[model.name] = run_model(handler, settings, questions, passages, texts, progress)
            summaries.append(summarise(model, runs[model.name]))
            progress(f"{model.name}: {summaries[-1]['cost_total']:.4f} USD")
            if args.out:
                write(args.out, args, started, summaries, runs, passages)
    except AwsProblem as problem:
        failed = problem
    finally:
        # A list, not a generator: a failed delete must not skip the others.
        deleted = all(
            [
                delete_reporting(bedrock, guardrail_id, progress, PROBE_NAMES[kind])
                for kind, guardrail_id in probes.items()
            ]
        )
    if args.out:
        write(args.out, args, started, summaries, runs, passages)
    print(f"Topic tier of the probe guardrails: {args.topics_tier}.\n")
    for line in table(summaries):
        print(line)
    total = sum(s["cost_total"] for s in summaries)
    print(f"\nModel and guardrail cost of the run: {total:.4f} USD (retrieval not counted).")
    if failed is not None:
        print(f"error: {failed}", file=sys.stderr)
        return 2
    return 0 if deleted else 2


def write(
    path: Path,
    args: argparse.Namespace,
    started: str,
    summaries: Sequence[Mapping[str, Any]],
    runs: Mapping[str, Any],
    passages: Passages,
) -> None:
    path.write_text(
        json.dumps(
            {
                "started": started,
                "region": args.region,
                "retrieve_region": args.retrieve_region,
                "topics_tier": args.topics_tier,
                "price_date": region_models(args.region).price_date,
                "summaries": summaries,
                "runs": runs,
                "passages": passages.found,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())

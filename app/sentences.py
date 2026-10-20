"""Every sentence the answer function says itself, in each language it speaks."""

from collections.abc import Mapping
from typing import TypeVar

T = TypeVar("T")

#: The two languages Svar speaks: the one the visitor's page is set to. A request
#: names it in the body field "language"; a missing or unknown one means English.
LANGUAGES = ("en", "de")

DEFAULT_LANGUAGE = "en"

#: Every sentence the handler says itself, in each language. A text with a
#: ``{placeholder}`` carries the same placeholders in both.
NO_SOURCES_ANSWERS = {
    "en": "No sources in this workspace match the question.",
    "de": "Keine Quelle in diesem Arbeitsbereich passt zur Frage.",
}

#: What the prompt tells the model to answer when the sources answer no part of the question.
#: The handler shows it also when the grounding or relevance check withheld it
#: (`blocked_answer`).
NOT_IN_SOURCES_ANSWERS = {
    "en": "The sources do not answer this question.",
    "de": "Die Quellen beantworten diese Frage nicht.",
}

#: Said after the error of a question that was given back to the key's budget.
REFUNDED_NOTES = {
    "en": " This question was not counted against your key.",
    "de": " Diese Frage wurde Ihrem Schlüssel nicht angerechnet.",
}

#: The only thing a caller learns about a broken configuration.
NOT_CONFIGURED_MESSAGES = {
    "en": "The service is not configured.",
    "de": "Der Dienst ist nicht eingerichtet.",
}

UNEXPECTED_FAILURE_MESSAGES = {
    "en": "The function failed unexpectedly; the details are in its log.",
    "de": "Die Funktion ist unerwartet ausgefallen; die Einzelheiten stehen in ihrem Protokoll.",
}

QUESTIONS_SPENT_MESSAGES = {
    "en": "This key's {limit} questions are used up; a new key is needed.",
    "de": "Die {limit} Fragen dieses Schlüssels sind aufgebraucht; es braucht einen neuen.",
}
WINDOW_CLOSED_MESSAGES = {
    "en": "This key's time window has closed; a new key is needed.",
    "de": "Der Zeitraum dieses Schlüssels ist abgelaufen; es braucht einen neuen Schlüssel.",
}
BUDGET_SPENT_MESSAGES = {
    "en": "This key's budget is used up; a new key is needed.",
    "de": "Das Budget dieses Schlüssels ist aufgebraucht; es braucht einen neuen Schlüssel.",
}

#: What a key on a plan reads when its quota is reached (app/plans.py).
MONTH_QUOTA_MESSAGES = {
    "en": (
        "This key's quota of {limit} questions for this month is reached. Please get in touch "
        "with your contact person."
    ),
    "de": (
        "Das Kontingent von {limit} Fragen für diesen Monat ist erreicht. Bitte melden Sie sich "
        "bei Ihrem Ansprechpartner."
    ),
}
DAY_QUOTA_MESSAGES = {
    "en": (
        "This key's quota of {limit} questions for today is reached. Please get in touch with "
        "your contact person."
    ),
    "de": (
        "Das Kontingent von {limit} Fragen für heute ist erreicht. Bitte melden Sie sich bei "
        "Ihrem Ansprechpartner."
    ),
}

#: What the guardrail's own message says, in the handler's words. The stack sets
#: the English ones on the guardrail (BLOCKED_INPUT_MESSAGE, BLOCKED_OUTPUT_MESSAGE
#: in infra/stacks/guardrails.py) and a test keeps the two in step; they are what
#: a caller reads when the handler cannot tell what the guardrail did.
QUESTION_REFUSED_ANSWERS = {
    "en": (
        "Svar answers questions about this company's documents. It does not give legal, tax "
        "or medical advice, and it does not answer questions about people's private lives or "
        "public figures."
    ),
    "de": (
        "Svar beantwortet Fragen zu den Dokumenten dieses Betriebs. Svar gibt keine Rechts-, "
        "Steuer- oder medizinische Beratung und beantwortet keine Fragen zum Privatleben von "
        "Personen oder zu Personen des öffentlichen Lebens."
    ),
}
ANSWER_WITHHELD_ANSWERS = {
    "en": (
        "Svar withheld this answer: the documents do not carry it, or it strays into advice "
        "Svar does not give."
    ),
    "de": (
        "Svar hat diese Antwort zurückgehalten: Die Dokumente enthalten sie nicht, oder sie "
        "geht in eine Beratung, die Svar nicht gibt."
    ),
}


def in_language(texts: Mapping[str, T], language: str) -> T:
    """The entry for this language; English when it is none of `LANGUAGES`."""
    return texts[language if language in texts else DEFAULT_LANGUAGE]

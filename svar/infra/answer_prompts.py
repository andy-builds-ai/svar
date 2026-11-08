"""Which wording of the system prompt each answer model answers with.

The wordings live in `svar/app/prompts.py` (only app/ goes into the function, and a model id
never goes into app/); here are their names, and which model answers with which. The wording
belongs to the model, not to the region, so a model is found by its name in Taug's tables
(`taug/infra/answer_models.py`), where its id and prices live. A test holds the names here, the
wordings in app/ and the tables together.

"each-part": the sentence for "not in the sources" only if the sources answer no part of the
question (07.10.2026, measured with Qwen3 235B). "sentence-first": the sentence whenever the
sources do not answer the question (06.10.2026, measured with Nova Pro). Only these two models
were measured with theirs (docs/decisions.md).
"""

from taug.infra.answer_models import AnswerModel

PROMPT_EACH_PART = "each-part"
PROMPT_SENTENCE_FIRST = "sentence-first"
DEFAULT_PROMPT = PROMPT_EACH_PART

#: The models that answer with another wording than `DEFAULT_PROMPT`, by their name.
PROMPTS_BY_MODEL_NAME = {"Nova Pro": PROMPT_SENTENCE_FIRST}


def prompt_of(model: AnswerModel) -> str:
    """The name of the wording ``model`` answers with."""
    return PROMPTS_BY_MODEL_NAME.get(model.name, DEFAULT_PROMPT)

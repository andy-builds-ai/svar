"""The answer models Andur may be given, and what their tokens cost.

The stack and `scripts/evaluate.py` both read this table, so a model never
travels without its price.
"""

from typing import NamedTuple


class AnswerModel(NamedTuple):
    name: str
    model_id: str
    #: USD per 1,000 tokens, eu-west-1, standard tier, read on `PRICE_DATE`.
    input_per_1k: str
    output_per_1k: str


#: AWS Price List API (the source of aws.amazon.com/bedrock/pricing), eu-west-1.
#: An EU profile is billed at the "regional" price, not the global one.
PRICE_DATE = "2026-09-28"

ANSWER_MODELS = (
    AnswerModel("Nemotron 3 Super", "nvidia.nemotron-super-3-120b", "0.00018", "0.00078"),
    AnswerModel("Nova Pro", "eu.amazon.nova-pro-v1:0", "0.00092", "0.00368"),
    AnswerModel("Nova 2 Lite", "eu.amazon.nova-2-lite-v1:0", "0.000374", "0.003157"),
    AnswerModel(
        "Claude Haiku 4.5", "eu.anthropic.claude-haiku-4-5-20251001-v1:0", "0.0011", "0.0055"
    ),
    AnswerModel("Claude Sonnet 4.6", "eu.anthropic.claude-sonnet-4-6", "0.0033", "0.0165"),
)


def answer_model(model_id: str) -> AnswerModel:
    """The table row of a model id; a model without a price is refused."""
    for model in ANSWER_MODELS:
        if model.model_id == model_id:
            return model
    raise KeyError(f"{model_id} has no price in infra/answer_models.py; add it there first.")

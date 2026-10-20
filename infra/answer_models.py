"""Where Andur runs, the models it may be given there, and what their tokens cost.

The stack, `scripts/evaluate.py` and the probes read a region's table through `REGIONS`, so a
model never travels without its price, and never without the price of its region.
"""

from typing import NamedTuple

#: The names of the system prompt's wordings, which live in `app/prompts.py` (only app/ goes into
#: the function, and a model id never goes into app/). A test holds both lists together.
#: "each-part": the sentence for "not in the sources" only if the sources answer no part of the
#: question (07.10.2026, measured with Qwen3 235B). "sentence-first": the sentence whenever the
#: sources do not answer the question (06.10.2026, measured with Nova Pro).
PROMPT_EACH_PART = "each-part"
PROMPT_SENTENCE_FIRST = "sentence-first"
DEFAULT_PROMPT = PROMPT_EACH_PART


class AnswerModel(NamedTuple):
    name: str
    model_id: str
    #: USD per 1,000 tokens, standard tier: in `ANSWER_MODELS` for eu-west-1, read on
    #: `PRICE_DATE`; in `FRANKFURT_MODELS` for eu-central-1, read on `FRANKFURT_PRICE_DATE`.
    input_per_1k: str
    output_per_1k: str
    #: The wording of the system prompt this model answers with: it belongs to the model, not to
    #: the region. Only Qwen3 235B and Nova Pro were measured with theirs (docs/decisions.md).
    prompt: str = DEFAULT_PROMPT


#: AWS Price List API (the source of aws.amazon.com/bedrock/pricing), eu-west-1.
#: An EU profile is billed at the "regional" price, not the global one.
PRICE_DATE = "2026-09-28"

ANSWER_MODELS = (
    AnswerModel("Nemotron 3 Super", "nvidia.nemotron-super-3-120b", "0.00018", "0.00078"),
    AnswerModel("Nova Pro", "eu.amazon.nova-pro-v1:0", "0.00092", "0.00368", PROMPT_SENTENCE_FIRST),
    AnswerModel("Nova 2 Lite", "eu.amazon.nova-2-lite-v1:0", "0.000374", "0.003157"),
    AnswerModel(
        "Claude Haiku 4.5", "eu.anthropic.claude-haiku-4-5-20251001-v1:0", "0.0011", "0.0055"
    ),
    AnswerModel("Claude Sonnet 4.6", "eu.anthropic.claude-sonnet-4-6", "0.0033", "0.0165"),
)


#: The candidates for a move to eu-central-1 (Frankfurt), priced there: the models that run in
#: Frankfurt itself, and two behind the EU profile for comparison. AWS Price List API,
#: eu-central-1, standard tier (effective 2026-10-01), read on `FRANKFURT_PRICE_DATE`; an EU
#: profile is billed at the price of the region that calls it.
FRANKFURT_PRICE_DATE = "2026-10-06"

FRANKFURT_MODELS = (
    AnswerModel("gpt-oss-120b", "openai.gpt-oss-120b-1:0", "0.0002", "0.00079"),
    AnswerModel("gpt-oss-20b", "openai.gpt-oss-20b-1:0", "0.00009", "0.0004"),
    AnswerModel("Qwen3 235B A22B 2507", "qwen.qwen3-235b-a22b-2507-v1:0", "0.00029", "0.00116"),
    AnswerModel("Qwen3 32B", "qwen.qwen3-32b-v1:0", "0.0002", "0.00079"),
    AnswerModel("MiniMax M2.5", "minimax.minimax-m2.5", "0.00036", "0.00144"),
    AnswerModel("MiniMax M2.1", "minimax.minimax-m2.1", "0.00036", "0.00144"),
    AnswerModel("Nemotron 3 Super", "nvidia.nemotron-super-3-120b", "0.00018", "0.00078"),
    AnswerModel("GLM 4.7 Flash", "zai.glm-4.7-flash", "0.00008", "0.00048"),
    AnswerModel("Devstral 2 123B", "mistral.devstral-2-123b", "0.00048", "0.0024"),
    AnswerModel("Nova Pro", "eu.amazon.nova-pro-v1:0", "0.00105", "0.0042", PROMPT_SENTENCE_FIRST),
    AnswerModel("Claude Sonnet 4.6", "eu.anthropic.claude-sonnet-4-6", "0.0033", "0.0165"),
)


class RegionModels(NamedTuple):
    """What a stack in one region answers with, and the prices it counts with there."""

    price_date: str
    models: tuple[AnswerModel, ...]
    answer_model_id: str
    #: None: the topic guardrail checks the denied topics in the Standard tier, through the EU
    #: guardrail profile, which carries the checks to six EU regions. A model id: that model
    #: checks them in the region itself, and there is no topic guardrail.
    topic_model_id: str | None
    #: Whether the answer guardrail (masking, grounding) runs through the EU profile as well.
    answer_guardrail_profile: bool
    #: True: CloudFront is the one address of page and questions, at its edge locations
    #: worldwide. False: the answer function's URL in the region is that address and serves the
    #: page itself (app/page.py); nothing of a request passes through another region.
    behind_cloudfront: bool = True
    #: The grounding threshold of the answer guardrail, measured for the region's answer
    #: model; None: `GROUNDING_THRESHOLD` of the stack (0.7).
    grounding_threshold: float | None = None


def answer_guardrail_uses_profile(
    answer_guardrail_profile: bool, topic_model_id: str | None
) -> bool:
    """Whether the answer guardrail runs through the EU profile: only where the topic guardrail
    does too. A stack whose topics a model checks keeps every guardrail in its region, whatever
    `answer_guardrail_profile` says. The stack and the probes ask this, so they agree."""
    return answer_guardrail_profile and topic_model_id is None


def region_answer_guardrail_uses_profile(region: str) -> bool:
    """`answer_guardrail_uses_profile` for the stack of a region of `REGIONS`."""
    models = region_models(region)
    return answer_guardrail_uses_profile(models.answer_guardrail_profile, models.topic_model_id)


#: The regions a stack may be deployed to. `cdk deploy -c region=<region>` and each script's
#: `--region` pick one; without them it is `DEFAULT_REGION`, but for `control_eval`,
#: `tool_probe` and `rehearsal`, which default to eu-central-1 on their own.
REGIONS = {
    "eu-west-1": RegionModels(PRICE_DATE, ANSWER_MODELS, "eu.amazon.nova-pro-v1:0", None, True),
    # Everything stays in Frankfurt. The Standard topic tier, which reads German, goes only with
    # the EU profile (AWS refused it without one on 06.10.2026), so gpt-oss-120b checks the
    # topics in the region; gpt-oss-20b let advice questions through (docs/decisions.md).
    # CloudFront cannot be kept to Germany, so the function's URL is the address. At 0.8 the
    # grounding check withheld 75 of 104 made-up answers and none of 59 right ones to German
    # documents, Qwen's own among them in three runs; at 0.7, 59 (docs/decisions.md).
    "eu-central-1": RegionModels(
        FRANKFURT_PRICE_DATE,
        FRANKFURT_MODELS,
        "qwen.qwen3-235b-a22b-2507-v1:0",
        "openai.gpt-oss-120b-1:0",
        False,
        behind_cloudfront=False,
        grounding_threshold=0.8,
    ),
}

DEFAULT_REGION = "eu-west-1"


def region_models(region: str) -> RegionModels:
    """The table of a region; a region without one is refused."""
    if region not in REGIONS:
        raise KeyError(
            f"{region} has no models in infra/answer_models.py; Andur runs in {', '.join(REGIONS)}."
        )
    return REGIONS[region]


def answer_model(model_id: str, region: str = DEFAULT_REGION) -> AnswerModel:
    """The table row of a model id in a region; a model without a price there is refused."""
    for model in region_models(region).models:
        if model.model_id == model_id:
            return model
    raise KeyError(
        f"{model_id} has no price for {region} in infra/answer_models.py; add it there first."
    )

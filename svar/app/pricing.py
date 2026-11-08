"""What one answer cost: tokens and guardrail units, each times its price per thousand."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import config

COST_DECIMAL_PLACES = 6

PRICED_UNITS = {
    "topicPolicyUnits": "topic_units",
    "sensitiveInformationPolicyUnits": "sensitive_information_units",
    "contextualGroundingPolicyUnits": "grounding_units",
}


def add_units(
    units: Mapping[str, int] | None,
    extra_units: int | None,
    kind: str = "sensitiveInformationPolicyUnits",
) -> dict[str, int] | None:
    """The answer's guardrail units plus another call's units of one kind (by default the
    excerpt guardrail's PII units); None when either is unknown."""
    if units is None or extra_units is None:
        return None
    total = dict(units)
    total[kind] += extra_units
    return total


def model_tokens_unknown(
    response: Mapping[str, Any], usage: Mapping[str, int], question_refused: bool
) -> bool:
    """Whether the model wrote an answer whose tokens the response does not give: one the
    guardrail withheld after the model call. Bedrock then reports 0 tokens, in the response and
    in CloudWatch (96 calls on 07.10.2026), and its user guide says the model call is charged
    all the same ("How charges are calculated for Amazon Bedrock Guardrails", read
    07.10.2026). A question the guardrail refused before the model was asked has no model
    tokens, and its cost is known."""
    no_tokens = not (usage.get("input_tokens") or usage.get("output_tokens"))
    intervened = response.get("stopReason") == "guardrail_intervened"
    return intervened and not question_refused and no_tokens


def answer_cost(
    prices: config.Prices,
    usage: Mapping[str, int],
    units: Mapping[str, int] | None,
    topic_tokens: Mapping[str, int] | None = None,
) -> float | None:
    """What one answer cost in USD: model tokens, guardrail units and the topic model's tokens,
    each times its price per thousand."""
    if units is None:
        return None
    topic_tokens = topic_tokens or {"input_tokens": 0, "output_tokens": 0}
    total = (
        Decimal(usage["input_tokens"]) * prices.input_tokens
        + Decimal(usage["output_tokens"]) * prices.output_tokens
        + Decimal(topic_tokens["input_tokens"]) * prices.topic_model_input_tokens
        + Decimal(topic_tokens["output_tokens"]) * prices.topic_model_output_tokens
        + sum(
            (
                Decimal(units[name]) * getattr(prices, field_name)
                for name, field_name in PRICED_UNITS.items()
            ),
            Decimal(0),
        )
    ) / 1000
    return float(round(total, COST_DECIMAL_PLACES))

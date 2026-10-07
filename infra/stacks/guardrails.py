"""The three guardrails: on every answer, on the sources' excerpts, and on the denied
topics."""

import hashlib
import json
from typing import TYPE_CHECKING, Any

from aws_cdk import (
    Token,
    aws_bedrock as bedrock,
)

from app.denied_topics import DENIED_TOPICS
from infra.answer_models import region_models

if TYPE_CHECKING:
    from infra.stacks.andur_stack import AndurStack


GUARDRAIL_NAME = "andur-answer-guardrail"

#: The Classic tier reads denied topics in English, French and Spanish only; it let
#: German tax, legal and medical questions through. The Standard tier reads German
#: and needs a guardrail profile. Both tiers cost the same per text unit.
TOPICS_TIER = "STANDARD"

#: The EU guardrail profile. Called from an EU region, it routes the guardrail's
#: checks to these regions and no others (Bedrock user guide, "Supported Regions
#: for cross-Region guardrail inference").
GUARDRAIL_PROFILE_ID = "eu.guardrail.v1:0"
GUARDRAIL_PROFILE_REGIONS = (
    "eu-central-1",
    "eu-west-1",
    "eu-west-3",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
)

MASKED_PII_TYPES = ("NAME", "EMAIL", "PHONE", "ADDRESS")

GROUNDING_THRESHOLD = 0.7
RELEVANCE_THRESHOLD = 0.7


def grounding_threshold_for(region: str) -> float:
    """The grounding threshold of the answer guardrail in ``region``: its own in `REGIONS`,
    measured for its answer model, else `GROUNDING_THRESHOLD`. The stack and the probes that
    copy its answer guardrail ask this, so they agree."""
    own = region_models(region).grounding_threshold
    return GROUNDING_THRESHOLD if own is None else own


BLOCKED_INPUT_MESSAGE = (
    "Svar answers questions about this company's documents. It does not give legal, tax or "
    "medical advice, and it does not answer questions about people's private lives or public "
    "figures."
)
BLOCKED_OUTPUT_MESSAGE = (
    "Svar withheld this answer: the documents do not carry it, or it strays into advice "
    "Svar does not give."
)

EXCERPT_GUARDRAIL_NAME = "andur-excerpt-guardrail"

#: The guardrail that checks the question and the answer for `DENIED_TOPICS`, each after its
#: frame and the answer with its citations taken out: on the answer as the model writes it,
#: `Private lives` withheld short harmless answers whose citations read [1][4] or [1][2][4],
#: and without them none (04.10.2026, docs/decisions.md).
TOPIC_GUARDRAIL_NAME = "andur-topic-guardrail"

#: Whether the answer guardrail on the model call reads the denied topics in the question, as
#: it did until the function checked the question with the topic guardrail (06.10.2026). True
#: was for the one deploy that brought in that check: in the seconds CloudFormation gives the
#: old code the new environment, the old code would have called an answer guardrail that no
#: longer reads the question's topics, without the check it did not know yet. With it True the
#: question was checked twice, as it is read alone and after `QUESTION_FRAME`. False for the
#: second deploy, once the first was through (06.10.2026): old and new code both check the
#: question with the topic guardrail. The answer guardrail never reads the answer's topics;
#: the topic guardrail does (05.10.2026).
ANSWER_GUARDRAIL_READS_QUESTION_TOPICS = False

#: Required by Bedrock, never shown: masking replaces text, it does not block.
EXCERPT_BLOCKED_MESSAGE = "This excerpt was withheld."


def masking_policy() -> bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty:
    """The masking both guardrails share: `MASKED_PII_TYPES` in the output, never in the input."""
    return bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
        pii_entities_config=[
            bedrock.CfnGuardrail.PiiEntityConfigProperty(
                type=pii_type,
                action="ANONYMIZE",
                input_enabled=False,
                input_action="NONE",
                output_enabled=True,
                output_action="ANONYMIZE",
            )
            for pii_type in MASKED_PII_TYPES
        ]
    )


def guardrail_with_version(
    stack: "AndurStack", construct_id: str, label: str, properties: dict[str, Any]
) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
    """A guardrail from ``properties``, and a numbered version that follows every one of
    them."""
    resolved = json.dumps(stack.resolve(properties), sort_keys=True)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    guardrail = bedrock.CfnGuardrail(stack, construct_id, **properties)
    version = bedrock.CfnGuardrailVersion(
        stack,
        f"{construct_id}Version",
        guardrail_identifier=guardrail.attr_guardrail_id,
        description=f"Andur {label} guardrail, settings {digest}",
    )
    return guardrail, version


def guardrail_profile_arn(stack: "AndurStack", region: str) -> str:
    """The EU guardrail profile in ``region``.

    A stack whose region is set to one outside the profile's regions would send
    its checks across a border; it does not synthesise. Without a set region
    (no ``env``) the region is unknown at synth time and cannot be checked;
    infra/app.py always sets it.
    """
    if not Token.is_unresolved(stack.region) and stack.region not in GUARDRAIL_PROFILE_REGIONS:
        raise ValueError(
            f"{stack.region} is not a source region of {GUARDRAIL_PROFILE_ID}; "
            f"Andur's guardrail stays in {', '.join(GUARDRAIL_PROFILE_REGIONS)}."
        )
    return stack.format_arn(
        service="bedrock",
        region=region,
        resource="guardrail-profile",
        resource_name=GUARDRAIL_PROFILE_ID,
    )


def cross_region_config(stack: "AndurStack", profile: bool) -> dict[str, Any]:
    """The EU guardrail profile for a guardrail, or nothing: masking and the grounding check
    run in the region without a profile."""
    if not profile:
        return {}
    return {
        "cross_region_config": bedrock.CfnGuardrail.GuardrailCrossRegionConfigProperty(
            guardrail_profile_arn=guardrail_profile_arn(stack, stack.region)
        )
    }


def topic_policy(on_answer: bool) -> bedrock.CfnGuardrail.TopicPolicyConfigProperty:
    """`DENIED_TOPICS` in the Standard tier, read in the question and, if ``on_answer``,
    in the answer too."""
    return bedrock.CfnGuardrail.TopicPolicyConfigProperty(
        topics_config=[
            bedrock.CfnGuardrail.TopicConfigProperty(
                name=name,
                definition=definition,
                examples=examples,
                type="DENY",
                input_enabled=True,
                input_action="BLOCK",
                output_enabled=on_answer,
                output_action="BLOCK" if on_answer else "NONE",
            )
            for name, definition, examples in DENIED_TOPICS
        ],
        topics_tier_config=bedrock.CfnGuardrail.TopicsTierConfigProperty(tier_name=TOPICS_TIER),
    )


def create_answer_guardrail(
    stack: "AndurStack",
) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
    """The guardrail on every answer, and the numbered version the function uses. Its
    topics read the question only while `ANSWER_GUARDRAIL_READS_QUESTION_TOPICS`, and never
    the answer; the topic guardrail reads both."""
    topics = (
        {"topic_policy_config": topic_policy(on_answer=False)}
        if ANSWER_GUARDRAIL_READS_QUESTION_TOPICS and stack.answer_guardrail_profile
        else {}
    )
    return guardrail_with_version(
        stack,
        "AnswerGuardrail",
        "answer",
        {
            "name": GUARDRAIL_NAME,
            "description": (
                "Andur: refuses advice topics, masks personal details, withholds "
                "ungrounded answers."
                if ANSWER_GUARDRAIL_READS_QUESTION_TOPICS
                else "Andur: masks personal details, withholds ungrounded answers."
            ),
            "blocked_input_messaging": BLOCKED_INPUT_MESSAGE,
            "blocked_outputs_messaging": BLOCKED_OUTPUT_MESSAGE,
            **topics,
            **cross_region_config(stack, stack.answer_guardrail_profile),
            "sensitive_information_policy_config": masking_policy(),
            "contextual_grounding_policy_config": bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(  # noqa: E501
                filters_config=[
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                        type="GROUNDING",
                        threshold=grounding_threshold_for(stack.price_region),
                        action="BLOCK",
                        enabled=True,
                    ),
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                        type="RELEVANCE",
                        threshold=RELEVANCE_THRESHOLD,
                        action="BLOCK",
                        enabled=True,
                    ),
                ]
            ),
        },
    )


def create_excerpt_guardrail(
    stack: "AndurStack",
) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
    """The guardrail that masks the sources' excerpts, and its numbered version."""
    return guardrail_with_version(
        stack,
        "ExcerptGuardrail",
        "excerpt",
        {
            "name": EXCERPT_GUARDRAIL_NAME,
            "description": "Andur: masks personal details in the excerpts of the sources.",
            "blocked_input_messaging": EXCERPT_BLOCKED_MESSAGE,
            "blocked_outputs_messaging": EXCERPT_BLOCKED_MESSAGE,
            "sensitive_information_policy_config": masking_policy(),
        },
    )


def create_topic_guardrail(
    stack: "AndurStack",
) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
    """The guardrail that checks the question and the answer, without its citations, for
    the denied topics, and its numbered version. The function applies it as INPUT to the
    question and as OUTPUT to the answer, each after its frame; its topics are set on both
    sides, exactly like the guardrail `python -m scripts.topic_probe` measures."""
    return guardrail_with_version(
        stack,
        "TopicGuardrail",
        "topic",
        {
            "name": TOPIC_GUARDRAIL_NAME,
            # Changed with the flag, so the first deploy makes no new version of it.
            "description": (
                "Andur: withholds answers that stray into a denied topic."
                if ANSWER_GUARDRAIL_READS_QUESTION_TOPICS
                else "Andur: refuses questions and withholds answers on a denied topic."
            ),
            "blocked_input_messaging": BLOCKED_INPUT_MESSAGE,
            "blocked_outputs_messaging": BLOCKED_OUTPUT_MESSAGE,
            "topic_policy_config": topic_policy(on_answer=True),
            **cross_region_config(stack, True),
        },
    )

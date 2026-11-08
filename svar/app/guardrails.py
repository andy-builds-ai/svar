"""What the guardrails did: read from the answer's trace, and the masking of the excerpts."""

from collections.abc import Mapping
from typing import Any

import aws
import config
import log
import pricing


def guardrail_assessments(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every assessment in the guardrail trace: the one of the input, then each one of the
    output."""
    trace = response.get("trace", {}).get("guardrail", {})
    found = list(trace.get("inputAssessment", {}).values())
    for assessments in trace.get("outputAssessments", {}).values():
        found.extend(assessments)
    return found


def read_guardrail(response: Mapping[str, Any]) -> dict[str, Any]:
    """What the guardrail did, read from its trace, and never a matched text."""
    blocked: list[str] = []
    masked: list[str] = []
    scores: dict[str, float | None] = {"grounding_score": None, "relevance_score": None}
    units: dict[str, int] | None = None
    input_assessments = list(
        response.get("trace", {}).get("guardrail", {}).get("inputAssessment", {}).values()
    )
    question_refused = any(
        topic.get("action") == "BLOCKED"
        for assessment in input_assessments
        for topic in assessment.get("topicPolicy", {}).get("topics", [])
    )
    for assessment in guardrail_assessments(response):
        for topic in assessment.get("topicPolicy", {}).get("topics", []):
            if topic.get("action") == "BLOCKED":
                blocked.append(f"topic:{topic.get('name')}")
        for entity in assessment.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            if entity.get("action") == "ANONYMIZED":
                masked.append(f"pii:{entity.get('type')}")
            elif entity.get("action") == "BLOCKED":
                blocked.append(f"pii:{entity.get('type')}")
        for check in assessment.get("contextualGroundingPolicy", {}).get("filters", []):
            kind = str(check.get("type", "")).lower()
            if f"{kind}_score" in scores and isinstance(check.get("score"), (int, float)):
                scores[f"{kind}_score"] = round(check["score"], 4)
            if check.get("action") == "BLOCKED":
                blocked.append(kind)
        usage = assessment.get("invocationMetrics", {}).get("usage")
        if usage is not None:
            units = units or {name: 0 for name in pricing.PRICED_UNITS}
            for name in pricing.PRICED_UNITS:
                units[name] += int(usage.get(name, 0))

    if blocked:
        action = "blocked"
    elif masked:
        action = "masked"
    elif response.get("stopReason") == "guardrail_intervened":
        action = "intervened"
    else:
        action = "none"
    policies = list(dict.fromkeys(blocked + masked))
    return {
        "action": action,
        "policies": policies,
        **scores,
        "units": units,
        "question_refused": question_refused,
    }


def mask_excerpts(
    guardrail_client: Any, settings: config.Settings, sources: list[dict[str, Any]]
) -> dict[str, Any]:
    """Mask the personal details in the sources' excerpts, in place; return what was masked and
    its units."""
    if not sources:
        return {"policies": [], "units": 0}
    try:
        response = guardrail_client.apply_guardrail(
            guardrailIdentifier=settings.excerpt_guardrail_id,
            guardrailVersion=settings.excerpt_guardrail_version,
            source="OUTPUT",
            content=[{"text": {"text": "\n".join(source["excerpt"] for source in sources)}}],
        )
    except aws.botocore_errors() as exc:
        log.log_event("excerpt-guard-failed", detail=aws.error_detail(exc))
        for source in sources:
            source["excerpt"] = None
        return {"policies": [], "units": None}

    policies = list(
        dict.fromkeys(
            f"pii:{entity.get('type')}"
            for assessment in response.get("assessments", [])
            for entity in assessment.get("sensitiveInformationPolicy", {}).get("piiEntities", [])
            if entity.get("action") == "ANONYMIZED"
        )
    )
    units = response.get("usage", {}).get("sensitiveInformationPolicyUnits")
    counted = {"policies": policies, "units": None if units is None else int(units)}
    action = response.get("action")
    if action == "NONE":
        return counted
    outputs = response.get("outputs", [])
    masked = (
        outputs[0].get("text", "").split("\n")
        if action == "GUARDRAIL_INTERVENED" and len(outputs) == 1
        else []
    )
    if len(masked) != len(sources):
        log.log_event(
            "excerpt-guard-unusable",
            action=action,
            outputs=len(outputs),
            lines=len(masked),
            excerpts=len(sources),
        )
        for source in sources:
            source["excerpt"] = None
        return counted
    for source, text in zip(sources, masked, strict=True):
        source["excerpt"] = text
    return counted

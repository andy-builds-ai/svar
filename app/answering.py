"""One question, start to finish: search, topic checks, the model, the response body."""

from collections.abc import Mapping
from typing import Any

import citations
import clients
import config
import guardrails
import llm
import log
import pricing
import prompts
import retrieval
import sentences
import topic_check


def refused_for_topics(
    settings: config.Settings, checked: Mapping[str, Any], record: log.RequestRecord
) -> dict[str, Any]:
    """The response to a question the topic check refused: the handler's sentence, no
    sources, no model call; it costs the check's topic units, or the topic model's tokens."""
    units = (
        None
        if checked["units"] is None
        else {**{name: 0 for name in pricing.PRICED_UNITS}, "topicPolicyUnits": checked["units"]}
    )
    usage = {"input_tokens": 0, "output_tokens": 0}
    record.topic_tokens = topic_check.topic_tokens(checked)
    record.guardrail = {
        "action": "blocked",
        "policies": checked["topics"],
        "grounding_score": None,
        "relevance_score": None,
        "units": units,
    }
    return {
        "answer": sentences.in_language(sentences.QUESTION_REFUSED_ANSWERS, record.language),
        "sources": [],
        "layers": [],
        "model": None,
        "stop_reason": None,
        "usage": usage,
        "guardrail": {"action": "blocked", "policies": checked["topics"]},
        "cost_usd": pricing.answer_cost(settings.prices, usage, units, record.topic_tokens),
        "price_date": settings.price_date,
    }


def answer_question(
    settings: config.Settings, workspace: str, question: str, record: log.RequestRecord
) -> dict[str, Any]:
    """Retrieve, ask the model, and build the response body — without latency."""
    retrieved = retrieval.retrieve_passages(
        clients.client("bedrock-agent-runtime", settings.region),
        settings.knowledge_base_id,
        question,
        workspace,
        settings.number_of_results,
    )
    results, record.dropped = retrieval.keep_workspace(retrieved, workspace)
    record.hits = len(results)
    if not results:
        # No model call and no guardrail call: nothing that is billed per answer.
        record.guardrail = {
            "action": "none",
            "policies": [],
            "grounding_score": None,
            "relevance_score": None,
            "units": {name: 0 for name in pricing.PRICED_UNITS},
        }
        return {
            "answer": sentences.in_language(sentences.NO_SOURCES_ANSWERS, record.language),
            "sources": [],
            "layers": [],
            "model": None,
            "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "guardrail": {"action": "none", "policies": []},
            "cost_usd": 0.0,
            "price_date": settings.price_date,
        }

    asked = topic_check.check_question_topics(
        topic_check.topic_client(settings), settings, question
    )
    if asked["topics"]:
        return refused_for_topics(settings, asked, record)

    response = llm.ask_model(
        clients.client("bedrock-runtime", settings.region),
        settings,
        prompts.build_content(question, results),
        record,
    )
    record.model_answered = True
    guardrail = guardrails.read_guardrail(response)
    guardrail["units"] = pricing.add_units(guardrail["units"], asked["units"], "topicPolicyUnits")
    replaced = llm.blocked_answer(response, guardrail, record.language)
    answer = replaced or llm.extract_answer(response, guardrail["action"], record.language)
    # Whatever of the model's text the reader gets is checked; only the handler's own sentence
    # is not, also when the trace says BLOCKED on an answer Bedrock did not stop.
    checked: Mapping[str, Any] = {}
    if replaced is None:
        checked = topic_check.check_answer_topics(
            topic_check.topic_client(settings), settings, llm.model_text(response) or ""
        )
        guardrail["units"] = pricing.add_units(
            guardrail["units"], checked["units"], "topicPolicyUnits"
        )
        if checked["topics"]:
            answer = sentences.in_language(sentences.ANSWER_WITHHELD_ANSWERS, record.language)
            guardrail["action"] = "blocked"
            guardrail["policies"] = list(dict.fromkeys(checked["topics"] + guardrail["policies"]))
    usage = llm.read_usage(response)
    # An answer withheld for a denied topic loses its sources too: their excerpts can say what
    # the answer was withheld for, and masking takes out names and addresses, not the rest.
    withheld_for_topic = guardrail["action"] == "blocked" and any(
        policy.startswith("topic:") for policy in guardrail["policies"]
    )
    if guardrail["question_refused"] or withheld_for_topic:
        sources: list[dict[str, Any]] = []
        excerpts = {"policies": [], "units": 0}
    else:
        sources = [
            retrieval.to_source(number, result) for number, result in enumerate(results, start=1)
        ]
        excerpts = guardrails.mask_excerpts(
            clients.client("guardrail", settings.region), settings, sources
        )
    guardrail["units"] = pricing.add_units(guardrail["units"], excerpts["units"])
    record.guardrail = guardrail
    record.excerpt_policies = excerpts["policies"]
    record.topic_tokens = topic_check.topic_tokens(asked, checked)
    cited = citations.cited_numbers(answer, len(sources))
    for source in sources:
        source["cited"] = source["number"] in cited
    return {
        "answer": answer,
        "sources": sources,
        "layers": citations.cited_layers(sources),
        "model": settings.model_id,
        "stop_reason": response.get("stopReason"),
        "usage": usage,
        "guardrail": {"action": guardrail["action"], "policies": guardrail["policies"]},
        "cost_usd": None
        if pricing.model_tokens_unknown(response, usage, guardrail["question_refused"])
        else pricing.answer_cost(settings.prices, usage, guardrail["units"], record.topic_tokens),
        "price_date": settings.price_date,
    }

"""The denied topics checked by a model, for a region where no guardrail that reads German
checks them without carrying the text to other regions.

The model reads the topics of `DENIED_TOPICS` and names the one a text strays into, or none, in
one line of JSON. eu-central-1 checks with gpt-oss-120b (`REGIONS` in infra/answer_models.py).
gpt-oss-20b went round in circles in its reasoning on harmless texts, and each way out measured
let advice questions through; gpt-oss-120b refused a question on cancelling a contract as legal
action until the sentence on the company's own contracts (docs/decisions.md, 06. and
07.10.2026). Measure every change with `python -m scripts.topic_probe --region <region>`.
"""

import re
from collections.abc import Mapping
from typing import Any

from denied_topics import DENIED_TOPICS

TOPIC_NAMES = tuple(name for name, _, _ in DENIED_TOPICS)

GATE_PROMPT = """You are a topic gate for a company document assistant. The assistant must not
deal with the denied topics below. Read the text and decide whether it strays into one of them.
The text may be in German or English. Questions about the company's own prices, invoices,
guarantees, contracts, services, opening hours and products are NOT denied topics, and neither
is who owns or runs the company or whom to contact there; asking for the name of the
owner, a manager or a contact person is NOT about a private life. Questions on what the company's
own contracts, terms and documents say, such as how to cancel a contract with the company, its
notice period or its guarantee, are NOT legal action either: the company answers them, not a
court. The topics are worded as questions; a text that answers such a question, or gives such
advice or such details unasked, strays into the same topic.

Denied topics:
{topics}

Reply with one line of JSON and nothing else: {{"topic": "<exact topic name>"}} or {{"topic": "none"}}."""  # noqa: E501

#: Room for the model's reasoning and the line of JSON.
MAX_VERDICT_TOKENS = 512

VERDICT_TEMPERATURE = 0

NO_TOPIC = "none"

#: The calls of one check that run one after the other: the first call, then either one call
#: in place of a first that failed or gave no verdict, or the `CONFIRMING_CALLS`, which run at
#: once. app/aws.py counts them in the time budget. Every call runs at the model's own
#: reasoning effort: at "low" gpt-oss-20b let "Which football club does the prime minister
#: support?" through in two of three runs (06.10.2026). gpt-oss-120b never ran out of its
#: tokens in 1,330 checks (07.10.2026).
ROUNDS_PER_CHECK = 2

#: The calls that ask again, at once, when the first call names a topic: the topic stands
#: unless every one of them names none; one that fails or gives no verdict keeps it. A single
#: call of gpt-oss-120b refused "Wie heißt der Inhaber von Kalvenstett Haustechnik?" in 4 of
#: 333 checks with the prompt here, 7 of 153 without its sentence on names, and let none of 650
#: texts to refuse pass (07.10.2026, docs/decisions.md). With two, a text to refuse passes only
#: when the first call lets it pass, or when both of these do. 0 lets the first topic stand.
CONFIRMING_CALLS = 2

_VERDICT = re.compile(r'"topic"\s*:\s*"([^"]*)"')


def gate_prompt() -> str:
    """The system prompt: the gate's rules and every topic with its definition and examples."""
    topics = "\n".join(
        f"- {name}: {definition} Examples: {'; '.join(examples)}"
        for name, definition, examples in DENIED_TOPICS
    )
    return GATE_PROMPT.format(topics=topics)


def ask(runtime: Any, model_id: str, text: str, effort: str | None = None) -> dict[str, Any]:
    """One Converse call that asks the model for its verdict on ``text``, at its own reasoning
    effort or at ``effort``; Bedrock's response."""
    effort_field = (
        {} if effort is None else {"additionalModelRequestFields": {"reasoning_effort": effort}}
    )
    return runtime.converse(
        modelId=model_id,
        system=[{"text": gate_prompt()}],
        messages=[{"role": "user", "content": [{"text": f"Text:\n{text}"}]}],
        inferenceConfig={"maxTokens": MAX_VERDICT_TOKENS, "temperature": VERDICT_TEMPERATURE},
        **effort_field,
    )


def reply_text(response: Mapping[str, Any]) -> str:
    """The text of the reply, without the model's reasoning; empty when there is none."""
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    return next((block["text"] for block in blocks if "text" in block), "")


def verdict(reply: str) -> str | None:
    """The topic the reply names, `NO_TOPIC`, or None when it names neither: an empty reply,
    one without the JSON, a topic that is none of `TOPIC_NAMES`, or two different verdicts, so
    that a "none" written first never outweighs a topic written after it."""
    named = {match.strip().casefold() for match in _VERDICT.findall(reply)}
    if len(named) != 1:
        return None
    (name,) = named
    if name == NO_TOPIC:
        return NO_TOPIC
    return next((topic for topic in TOPIC_NAMES if topic.casefold() == name), None)

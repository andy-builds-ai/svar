"""The answer model: one Converse call through the answer guardrail, and reading its response."""

import json
from collections.abc import Mapping
from typing import Any

import aws
import clocks
import config
import errors
import log
import prompts
import sentences

#: Answers are a few sentences; this only stops a runaway answer.
MAX_ANSWER_TOKENS = 1024

#: The least random setting; AWS does not promise identical answers for it.
ANSWER_TEMPERATURE = 0

#: Notes added to an answer the model did not finish on its own.
STOP_NOTES = {
    "en": {
        "max_tokens": f"[Answer cut off: the limit of {MAX_ANSWER_TOKENS} tokens was reached.]",
        "content_filtered": "[Answer stopped: the model's content filter intervened.]",
        "guardrail_intervened": "[Answer stopped: a guardrail intervened.]",
    },
    "de": {
        "max_tokens": (
            f"[Antwort abgebrochen: Das Limit von {MAX_ANSWER_TOKENS} Tokens ist erreicht.]"
        ),
        "content_filtered": "[Antwort gestoppt: Der Inhaltsfilter des Modells hat eingegriffen.]",
        "guardrail_intervened": "[Antwort gestoppt: Ein Guardrail hat eingegriffen.]",
    },
}

GUARDRAIL_NOTES = {
    "en": {
        "blocked": None,
        "masked": "[Personal details in this answer are masked.]",
    },
    "de": {
        "blocked": None,
        "masked": "[Persönliche Angaben in dieser Antwort sind unkenntlich gemacht.]",
    },
}


#: Converse errors whose AWS description says the request was not run: denied
#: over a quota, a service unable to handle it, a model not ready to serve.
#: Not InternalServerException — processing had begun (docs/decisions.md).
MODEL_REJECTION_CODES = frozenset(
    {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException"}
)


def ask_model(
    bedrock_runtime: Any,
    settings: config.Settings,
    content: list[dict[str, Any]],
    record: log.RequestRecord | None = None,
) -> dict[str, Any]:
    """Send the system rules and the message to the model through the guardrail; return
    Bedrock's raw response. A call AWS turns away is made once more while there is time."""
    record = record if record is not None else log.RequestRecord()
    for attempt in (1, 2):
        try:
            return converse(bedrock_runtime, settings, content, record.language)
        except aws.botocore_errors() as exc:
            rejected = aws.error_code(exc) in MODEL_REJECTION_CODES
            record.model_rejections += rejected
            if attempt == 1 and rejected:
                elapsed = clocks.clock() - record.started
                retry = elapsed <= aws.latest_model_retry_start(settings.page_folder is None)
                log.log_event(
                    "converse-rejected",
                    model=settings.model_id,
                    detail=aws.error_detail(exc),
                    elapsed_ms=round(elapsed * 1000),
                    retry=retry,
                )
                if retry:
                    clocks.pause(aws.MODEL_RETRY_PAUSE_SECONDS)
                    continue
            log.log_event("converse-failed", model=settings.model_id, detail=aws.error_detail(exc))
            cause = german_cause = aws.error_cause(exc)
            if aws.error_code(exc) in {"AccessDeniedException", "AccessDenied"}:
                cause += " — the model or the guardrail may not be open to this function"
                german_cause += (
                    " — das Modell oder der Guardrail ist für diese Funktion womöglich nicht "
                    "freigegeben"
                )
            raise errors.LLMError(
                f"The model {settings.model_id} could not answer: {cause}.",
                f"Das Modell {settings.model_id} konnte nicht antworten: {german_cause}.",
            ) from exc
    raise AssertionError("unreachable: the second attempt returns or raises")


def converse(
    bedrock_runtime: Any,
    settings: config.Settings,
    content: list[dict[str, Any]],
    language: str = sentences.DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    """One model call with the system rules, the message and the guardrail."""
    return bedrock_runtime.converse(
        modelId=settings.model_id,
        system=[{"text": prompts.system_prompt(language)}],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": MAX_ANSWER_TOKENS, "temperature": ANSWER_TEMPERATURE},
        guardrailConfig={
            "guardrailIdentifier": settings.guardrail_id,
            "guardrailVersion": settings.guardrail_version,
            "trace": "enabled",
        },
    )


def model_text(response: Mapping[str, Any]) -> str | None:
    """The first content block that carries text, not blindly the first one."""
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    return next((block["text"] for block in blocks if "text" in block), None)


def extract_answer(
    response: Mapping[str, Any],
    guardrail_action: str = "none",
    language: str = sentences.DEFAULT_LANGUAGE,
) -> str:
    """The model's text, with the note, in the request's language, when the model did not
    finish on its own."""
    text = model_text(response)
    stop_reason = response.get("stopReason", "unknown")
    if text is None or not text.strip():
        raise errors.LLMError(
            f"The model returned no text (stopReason {stop_reason}).",
            f"Das Modell hat keinen Text geliefert (stopReason {stop_reason}).",
        )
    guardrail_notes = sentences.in_language(GUARDRAIL_NOTES, language)
    if stop_reason == "guardrail_intervened" and guardrail_action in guardrail_notes:
        note = guardrail_notes[guardrail_action]
    else:
        note = sentences.in_language(STOP_NOTES, language).get(stop_reason)
    return f"{text}\n\n{note}" if note else text


def written_text(response: Mapping[str, Any]) -> str | None:
    """What the model wrote, before the guardrail replaced or masked it: the trace's
    ``modelOutput`` when it has one, else the response's text. Bedrock writes the whole
    message there as JSON, and only its text blocks are the answer; JSON of another shape
    gives None. The same reading as `original_text` in scripts/control_eval.py."""
    original = response.get("trace", {}).get("guardrail", {}).get("modelOutput") or []
    if not original:
        return model_text(response)
    if not isinstance(original[0], str):
        return None
    try:
        parsed = json.loads(original[0])
    except ValueError:
        return original[0]
    message = parsed.get("message") if isinstance(parsed, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    texts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(texts) if texts else None


#: The only checks that may withhold the model's "not in the sources" sentence and still let
#: the reader have it: they judge an answer against the sources, and that sentence claims
#: nothing the sources could carry. Whether it is right about them, they cannot judge.
SOURCE_CHECKS = frozenset({"grounding", "relevance"})


def blocked_answer(
    response: Mapping[str, Any], guardrail: Mapping[str, Any], language: str
) -> str | None:
    """The handler's own sentence for a question or an answer the guardrail blocked, in the
    request's language; None when it blocked nothing.

    The response says it with two things together: Bedrock stopped with
    ``guardrail_intervened``, and the trace holds a policy with the action ``BLOCKED``
    (`read_guardrail` reads that). The text Bedrock returns then is the guardrail's own
    message, in English whatever the request's language, and this sentence replaces it. A
    refused question is told from a withheld answer by the input assessment: only the topic
    filter reads the question, and once it refuses, the answer is not checked at all.

    One withheld answer is not withheld from the reader: the model wrote exactly the prompt's
    "not in the sources" sentence and nothing else, and only the grounding or the relevance
    check blocked it. That sentence is the handler's own and claims nothing a source could
    carry. It can be wrong about the sources (c32, whose deposit they hold), and the checks
    cannot tell, as they find it in no source: in the control run of 07.10.2026 they withheld
    11 of the 12 such answers, 8 of them for grounding (docs/decisions.md). The reader gets
    the sentence in place of "withheld"; any other text stays withheld.
    """
    if response.get("stopReason") != "guardrail_intervened" or guardrail["action"] != "blocked":
        return None
    refused = guardrail["question_refused"]
    if refused:
        return sentences.in_language(sentences.QUESTION_REFUSED_ANSWERS, language)
    not_in_sources = sentences.in_language(sentences.NOT_IN_SOURCES_ANSWERS, language)
    if (
        guardrail["policies"]
        and set(guardrail["policies"]) <= SOURCE_CHECKS
        and (written_text(response) or "").strip() == not_in_sources
    ):
        return not_in_sources
    return sentences.in_language(sentences.ANSWER_WITHHELD_ANSWERS, language)


def read_usage(response: Mapping[str, Any]) -> dict[str, int]:
    """Input and output tokens of one model call, 0 where Bedrock reports none."""
    usage = response.get("usage", {})
    return {
        "input_tokens": int(usage.get("inputTokens", 0)),
        "output_tokens": int(usage.get("outputTokens", 0)),
    }

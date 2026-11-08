"""The function's log: one JSON line per event, and the record a request's line is built from."""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import clocks
import sentences

logger = logging.getLogger("andur")
logger.setLevel(logging.INFO)


def log_event(event: str, **fields: Any) -> None:
    """Write one JSON line to the function's log."""
    logger.info(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


@dataclass
class RequestRecord:
    """What the log line of a request says, filled in while the request runs."""

    workspace: str | None = None
    #: Passages retrieve returned for another workspace and the handler dropped.
    dropped: int = 0
    #: What `read_guardrail` found. Scores and units go to the log line only.
    guardrail: dict[str, Any] = field(default_factory=dict)
    hits: int = 0
    #: Personal details the excerpt guardrail masked, by type.
    excerpt_policies: list[str] = field(default_factory=list)
    #: When the request began, on `clock`; a second model call needs time left.
    started: float = field(default_factory=lambda: clocks.clock())
    #: Model calls AWS turned away as busy or throttled.
    model_rejections: int = 0
    #: What the request's body names, once it is read; English until then.
    language: str = sentences.DEFAULT_LANGUAGE
    #: The topic model's tokens over the request's topic checks; 0 where a guardrail checks.
    topic_tokens: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    #: Whether the answer model gave its response: from then on the question cost a paid model
    #: call, and a failure after it is not given back (`refund_question`).
    model_answered: bool = False

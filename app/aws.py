"""AWS clients for the answer function: timeouts, retries and readable causes."""

import functools
from collections.abc import Mapping
from typing import Any, NamedTuple


class ClientLimits(NamedTuple):
    """How long one AWS call may take, and how often it is tried."""

    connect_timeout: int
    read_timeout: int
    attempts: int


CLIENT_LIMITS = {
    "ssm": ClientLimits(connect_timeout=3, read_timeout=5, attempts=2),
    "dynamodb": ClientLimits(connect_timeout=2, read_timeout=3, attempts=2),
    "bedrock-agent-runtime": ClientLimits(connect_timeout=3, read_timeout=10, attempts=2),
    "bedrock-runtime": ClientLimits(connect_timeout=3, read_timeout=40, attempts=1),
    "guardrail": ClientLimits(connect_timeout=2, read_timeout=5, attempts=2),
    # One attempt here: the handler asks the topic model again itself, in place of a call that
    # failed or gave no verdict, or at once twice to confirm a topic
    # (`TOPIC_MODEL_ROUNDS_PER_CHECK`). gpt-oss-120b answered in 0.47 s median and 4.65 s at
    # most, 319 checks from a PC to eu-central-1 (06.10.2026), one call in 6.69 s (07.10.2026);
    # two rounds of 2 + 8 seconds take as long as one guardrail call at most.
    "topic-model": ClientLimits(connect_timeout=2, read_timeout=8, attempts=1),
}

#: The calls of one topic check by the topic model that run one after the other; the calls
#: that confirm a topic run at once, in the second (topic_model.ROUNDS_PER_CHECK).
TOPIC_MODEL_ROUNDS_PER_CHECK = 2

SERVICE_OF_CLIENT = {"guardrail": "bedrock-runtime", "topic-model": "bedrock-runtime"}

RETRY_DELAY_SECONDS = 6


KEY_READS_PER_REQUEST = 1


BUDGET_WRITES_PER_REQUEST = 2

#: After the model call: the topic check of the answer, then the excerpts' masking. A topic
#: check by the topic model counts as a guardrail call: it takes no longer
#: (`topic_check_fits_a_guardrail_call`).
GUARDRAIL_CALLS_AFTER_MODEL = 2

#: Before the model call, the topic check of the question; then the two after it.
GUARDRAIL_CALLS_PER_REQUEST = 1 + GUARDRAIL_CALLS_AFTER_MODEL

#: How long CloudFront waits for an answer: `ORIGIN_RESPONSE_TIMEOUT_SECONDS` in the stack.
ORIGIN_TIMEOUT_SECONDS = 120

#: How long the page waits for an answer before it gives up itself: `REQUEST_TIMEOUT_MS` in
#: web/app.js. Longer than the function may run, so the function is the first to end a
#: request where no CloudFront stands in front of it.
PAGE_TIMEOUT_SECONDS = 200

#: How long the function may run: `ANSWER_TIMEOUT_SECONDS` in the stack. Without CloudFront
#: nothing between the caller and the function ends a request sooner.
FUNCTION_TIMEOUT_SECONDS = 195

#: The pause before the one second model call after AWS turned the first away.
MODEL_RETRY_PAUSE_SECONDS = 2

#: Time the caller waits that the handler's clock does not see: the cold start
#: (Init Duration 0.11 s, 28.09.2026), the way in to the function, the runtime around the
#: handler.
ORIGIN_MARGIN_SECONDS = 5


def topic_model_check_seconds() -> int:
    """How long one topic check by the topic model can take: both its calls timing out."""
    return TOPIC_MODEL_ROUNDS_PER_CHECK * call_seconds("topic-model")


def topic_check_fits_a_guardrail_call() -> bool:
    """Whether a topic check by the topic model ends within a guardrail call's time, so that
    every sum of this module, counted in guardrail calls, holds for either way of checking."""
    return topic_model_check_seconds() <= call_seconds("guardrail")


def call_seconds(service: str) -> int:
    """How long one call to a service can take when every attempt runs into its timeouts."""
    limits = CLIENT_LIMITS[service]
    return (
        limits.attempts * (limits.connect_timeout + limits.read_timeout)
        + (limits.attempts - 1) * RETRY_DELAY_SECONDS
    )


def caller_deadline_seconds(behind_cloudfront: bool = True) -> int:
    """How long the first to give up waits for an answer: CloudFront's 120 seconds behind
    CloudFront; without it the function's own 195, which the page (200) and `scripts/ask.py`
    (200) outlast. An answer later than that is lost, and its question counted."""
    if behind_cloudfront:
        return ORIGIN_TIMEOUT_SECONDS
    return min(PAGE_TIMEOUT_SECONDS, FUNCTION_TIMEOUT_SECONDS)


def latest_model_retry_start(behind_cloudfront: bool = True) -> int:
    """How far into a request the second model call may still start.

    After it come the pause, the model call and two guardrail calls (the topic
    check of the answer, the excerpts); all of that has to end before the first
    caller gives up (`caller_deadline_seconds`), less a margin for what the
    handler cannot see.
    """
    return (
        caller_deadline_seconds(behind_cloudfront)
        - ORIGIN_MARGIN_SECONDS
        - MODEL_RETRY_PAUSE_SECONDS
        - call_seconds("bedrock-runtime")
        - GUARDRAIL_CALLS_AFTER_MODEL * call_seconds("guardrail")
    )


def worst_case_seconds(behind_cloudfront: bool = True) -> int:
    """How long one request can take when every AWS call runs into every timeout.

    A request with a second model call ends by `caller_deadline_seconds`, so it
    counts only if that were the longer of the two.
    """
    calls = {
        "ssm": KEY_READS_PER_REQUEST,
        "dynamodb": BUDGET_WRITES_PER_REQUEST,
        "guardrail": GUARDRAIL_CALLS_PER_REQUEST,
        # Its topic checks take the place of the topic guardrail's, counted under "guardrail".
        "topic-model": 0,
    }
    every_timeout = sum(calls.get(service, 1) * call_seconds(service) for service in CLIENT_LIMITS)
    with_model_retry = (
        latest_model_retry_start(behind_cloudfront)
        + MODEL_RETRY_PAUSE_SECONDS
        + call_seconds("bedrock-runtime")
        + GUARDRAIL_CALLS_AFTER_MODEL * call_seconds("guardrail")
    )
    return max(every_timeout, with_model_retry)


@functools.cache
def client(service: str, region: str) -> Any:
    """A boto3 client with this module's limits, made once per cold start."""
    import boto3
    from botocore.config import Config

    limits = CLIENT_LIMITS[service]
    config = Config(
        connect_timeout=limits.connect_timeout,
        read_timeout=limits.read_timeout,
        retries={"mode": "standard", "total_max_attempts": limits.attempts},
    )
    return boto3.client(SERVICE_OF_CLIENT.get(service, service), region_name=region, config=config)


def sdk_versions() -> dict[str, str]:
    """The boto3 and botocore versions the runtime brings."""
    import boto3
    import botocore

    return {"boto3": boto3.__version__, "botocore": botocore.__version__}


def botocore_errors() -> tuple[type[BaseException], ...]:
    """The two botocore base exceptions: service errors and client-side errors."""
    from botocore.exceptions import BotoCoreError, ClientError

    return BotoCoreError, ClientError


def error_code(exc: BaseException) -> str | None:
    """The AWS error code of a `ClientError`, or None for anything else."""
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return None
    return response.get("Error", {}).get("Code")


_THROTTLING_CODES = {
    "ThrottlingException",
    "Throttling",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
}
_UNAVAILABLE_CODES = {
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelNotReadyException",
}


def error_detail(exc: BaseException) -> str:
    """What a log line may say about a botocore exception: its class and its code."""
    code = error_code(exc)
    name = type(exc).__name__
    return f"{name}: {code}" if code else name


#: The botocore exceptions of a call that ran out of time: it may have run, and been paid for.
_TIMEOUT_ERRORS = {"ReadTimeoutError", "ConnectTimeoutError"}


def timed_out(exc: BaseException | None) -> bool:
    """Whether ``exc``, or an exception it was raised from, is a timeout of an AWS call."""
    while exc is not None:
        if any(cls.__name__ in _TIMEOUT_ERRORS for cls in type(exc).__mro__):
            return True
        exc = exc.__cause__
    return False


def error_cause(exc: BaseException) -> str:
    """A short cause for a botocore exception, safe to send to the caller."""
    code = error_code(exc)
    name = type(exc).__name__
    if code in _THROTTLING_CODES:
        return "too many requests right now (throttled), try again in a moment"
    if code in {"AccessDeniedException", "AccessDenied"}:
        return "access denied"
    if code == "ResourceNotFoundException":
        return "not found"
    if code in _UNAVAILABLE_CODES:
        return f"the service is unavailable ({code})"
    if name in {"ReadTimeoutError", "ConnectTimeoutError"}:
        return "no response within the timeout"
    if name == "EndpointConnectionError":
        return "the service could not be reached"
    return code or name

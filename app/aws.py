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
}

SERVICE_OF_CLIENT = {"guardrail": "bedrock-runtime"}

RETRY_DELAY_SECONDS = 6


KEY_READS_PER_REQUEST = 1


BUDGET_WRITES_PER_REQUEST = 2

#: How long CloudFront waits for an answer: `ORIGIN_RESPONSE_TIMEOUT_SECONDS` in the stack.
ORIGIN_TIMEOUT_SECONDS = 120

#: The pause before the one second model call after AWS turned the first away.
MODEL_RETRY_PAUSE_SECONDS = 2

#: Time CloudFront waits that the handler's clock does not see: the cold start
#: (Init Duration 0.11 s, 28.09.2026), the way from CloudFront to the function,
#: the runtime around the handler.
ORIGIN_MARGIN_SECONDS = 5


def call_seconds(service: str) -> int:
    """How long one call to a service can take when every attempt runs into its timeouts."""
    limits = CLIENT_LIMITS[service]
    return (
        limits.attempts * (limits.connect_timeout + limits.read_timeout)
        + (limits.attempts - 1) * RETRY_DELAY_SECONDS
    )


def latest_model_retry_start() -> int:
    """How far into a request the second model call may still start.

    After it come the pause, the model call and the excerpt guardrail; all of
    that has to end before CloudFront gives up, less a margin for what the
    handler cannot see.
    """
    return (
        ORIGIN_TIMEOUT_SECONDS
        - ORIGIN_MARGIN_SECONDS
        - MODEL_RETRY_PAUSE_SECONDS
        - call_seconds("bedrock-runtime")
        - call_seconds("guardrail")
    )


def worst_case_seconds() -> int:
    """How long one request can take when every AWS call runs into every timeout.

    A request with a second model call ends by `ORIGIN_TIMEOUT_SECONDS`, so it
    counts only if that were the longer of the two.
    """
    calls = {"ssm": KEY_READS_PER_REQUEST, "dynamodb": BUDGET_WRITES_PER_REQUEST}
    every_timeout = sum(calls.get(service, 1) * call_seconds(service) for service in CLIENT_LIMITS)
    with_model_retry = (
        latest_model_retry_start()
        + MODEL_RETRY_PAUSE_SECONDS
        + call_seconds("bedrock-runtime")
        + call_seconds("guardrail")
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

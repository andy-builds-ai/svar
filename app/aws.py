"""AWS clients for the answer function: timeouts, retries and readable causes.

Lambda imports the modules of this folder by their plain names (``handler``,
``aws``), and the tests do the same (tests/conftest.py puts the folder on the
import path). The module knows nothing about HTTP: a missing boto3 surfaces as
the ImportError it is, and the handler decides what that means for a request.

boto3 is imported lazily, so the tests run without it; in Lambda the runtime
provides it.
"""

import functools
from collections.abc import Mapping
from typing import Any, NamedTuple


class ClientLimits(NamedTuple):
    """How long one AWS call may take, and how often it is tried."""

    connect_timeout: int
    read_timeout: int
    attempts: int


#: Limits per AWS service, in seconds. Retries use botocore's "standard" mode:
#: throttling, timeouts and 5xx are tried again.
#:
#: The model call is tried once. A converse that timed out may already have
#: generated its answer, and a second attempt would pay for all input tokens
#: again and double the wait — the caller gets a 502 and can ask again.
#: The model's read timeout is the longest, since a non-streaming answer
#: arrives in one piece at the end.
CLIENT_LIMITS = {
    "ssm": ClientLimits(connect_timeout=3, read_timeout=5, attempts=2),
    "bedrock-agent-runtime": ClientLimits(connect_timeout=3, read_timeout=10, attempts=2),
    "bedrock-runtime": ClientLimits(connect_timeout=3, read_timeout=40, attempts=1),
}

#: The longest pause botocore makes before a retry: up to 1 second of jittered
#: backoff, plus up to 5 seconds more when AWS sends a retry-after header.
RETRY_DELAY_SECONDS = 6


def worst_case_seconds() -> int:
    """How long one request can take when every AWS call runs into every timeout.

    Counts each service once. Reading the keys is one page for a handful of
    workspaces; a key store large enough to need several pages would add a
    read per page.
    """
    return sum(
        limits.attempts * (limits.connect_timeout + limits.read_timeout)
        + (limits.attempts - 1) * RETRY_DELAY_SECONDS
        for limits in CLIENT_LIMITS.values()
    )


@functools.cache
def client(service: str, region: str) -> Any:
    """A boto3 client with this module's limits, made once per cold start.

    Raises ImportError when boto3 is not installed.
    """
    import boto3
    from botocore.config import Config

    limits = CLIENT_LIMITS[service]
    config = Config(
        connect_timeout=limits.connect_timeout,
        read_timeout=limits.read_timeout,
        retries={"mode": "standard", "total_max_attempts": limits.attempts},
    )
    return boto3.client(service, region_name=region, config=config)


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
_UNAVAILABLE_CODES = {"ServiceUnavailableException", "InternalServerException", "ModelNotReadyException"}


def error_cause(exc: BaseException) -> str:
    """A short cause for a botocore exception, safe to send to the caller.

    AWS error messages can carry ARNs, and ARNs carry the account id, so the
    message itself is never used here — only the error code or the class name.
    The full exception goes to the log.
    """
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

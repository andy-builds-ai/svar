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
#:
#: The budget counter sits on the critical path of every question, so its
#: timeouts are the shortest of all. It is tried twice: a conditional
#: increment is not idempotent, and a retry after a lost response can charge
#: one question twice — but that is the safe direction. The count runs ahead,
#: never behind, so the limit stays hard.
CLIENT_LIMITS = {
    "ssm": ClientLimits(connect_timeout=3, read_timeout=5, attempts=2),
    "dynamodb": ClientLimits(connect_timeout=2, read_timeout=3, attempts=2),
    "bedrock-agent-runtime": ClientLimits(connect_timeout=3, read_timeout=10, attempts=2),
    "bedrock-runtime": ClientLimits(connect_timeout=3, read_timeout=40, attempts=1),
    "guardrail": ClientLimits(connect_timeout=2, read_timeout=5, attempts=2),
}

#: Clients that are named for their job, not their service. The excerpt
#: guardrail is an ``ApplyGuardrail`` call on ``bedrock-runtime``, but with
#: limits of its own: under the model's 40 seconds the worst case would pass
#: the function's timeout. It masks a few hundred characters, so it is short,
#: and it is tried twice — it generates nothing, so a second attempt pays for
#: nothing twice but a text unit.
SERVICE_OF_CLIENT = {"guardrail": "bedrock-runtime"}

#: The longest pause botocore makes before a retry: up to 1 second of jittered
#: backoff, plus up to 5 seconds more when AWS sends a retry-after header.
RETRY_DELAY_SECONDS = 6


#: How often one request can read Parameter Store: once for the cache, and
#: once more when the key presented is unknown — ``authorize`` in handler.py
#: rereads before it answers 401, so that a key created a moment ago works at
#: once. Both reads can run into every timeout in the same request.
KEY_READS_PER_REQUEST = 2


#: How often one request can write the budget table: once to count the
#: question, and once more to push an item's end past the window it carries —
#: ``keep_the_item_past_its_window`` in handler.py. The second write happens
#: for a hand-written item that is wrong, and only until it is right, but the
#: worst case has to hold for it too.
BUDGET_WRITES_PER_REQUEST = 2


def worst_case_seconds() -> int:
    """How long one request can take when every AWS call runs into every timeout.

    Counts each service once, except Parameter Store
    (`KEY_READS_PER_REQUEST`) and the budget table
    (`BUDGET_WRITES_PER_REQUEST`). Reading the keys is one page for a handful
    of workspaces; a key store large enough to need several pages would add a
    read per page.

    Today that is 171 seconds, counted with one page per key read, and that
    holds up to ten workspaces at most: ``GetParametersByPath`` returns no
    more than ten parameters per page (its MaxResults is 1 to 10), so from
    the eleventh key on, every key read takes two calls or more and the worst
    case grows.
    """
    calls = {"ssm": KEY_READS_PER_REQUEST, "dynamodb": BUDGET_WRITES_PER_REQUEST}
    return sum(
        calls.get(service, 1)
        * (
            limits.attempts * (limits.connect_timeout + limits.read_timeout)
            + (limits.attempts - 1) * RETRY_DELAY_SECONDS
        )
        for service, limits in CLIENT_LIMITS.items()
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
_UNAVAILABLE_CODES = {"ServiceUnavailableException", "InternalServerException", "ModelNotReadyException"}


def error_detail(exc: BaseException) -> str:
    """What a log line may say about a botocore exception: its class and its code.

    Never ``repr(exc)``. A botocore exception carries the service's own
    message, and a service quotes what it choked on: a Bedrock
    ``ValidationException`` on a converse call repeats the whole prompt, which
    is the question plus passages out of a company's documents. That belongs
    in no log. The class name and the error code are what a person actually
    reads to tell one failure from another, and neither carries data.
    """
    code = error_code(exc)
    name = type(exc).__name__
    return f"{name}: {code}" if code else name


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

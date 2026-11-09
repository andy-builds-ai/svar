"""What a botocore exception may say: its code, a line for the log, a cause for the caller."""

from collections.abc import Mapping


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

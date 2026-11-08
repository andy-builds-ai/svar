"""The AWS clients of the function, made through aws.py."""

import functools
from typing import Any

import aws
import errors
import log


@functools.cache
def log_sdk_versions() -> None:
    """Log the boto3 and botocore versions the runtime brings, once per cold start."""
    log.log_event("cold-start", **aws.sdk_versions())


def client(service: str, region: str) -> Any:
    """An AWS client for the handler; a missing boto3 is a configuration error."""
    try:
        made = aws.client(service, region)
        log_sdk_versions()
    except ImportError as exc:
        raise errors.ConfigurationError("boto3 is not available to the function.") from exc
    return made

"""Tests for the AWS plumbing of the answer function (app/aws.py), without AWS."""

import builtins
from typing import Any

import pytest

import aws  # app/aws.py, imported by its Lambda name (tests/conftest.py)


def client_error(code: str, message: str = "details", operation: str = "Converse") -> Exception:
    exceptions = pytest.importorskip("botocore.exceptions")
    return exceptions.ClientError({"Error": {"Code": code, "Message": message}}, operation)


@pytest.fixture(autouse=True)
def fresh_clients() -> Any:
    aws.client.cache_clear()
    yield
    aws.client.cache_clear()


# --- Limits --------------------------------------------------------------


def test_the_model_call_is_tried_once_everything_else_twice() -> None:
    """A retried converse pays for its input tokens twice and doubles the wait."""
    assert aws.CLIENT_LIMITS["bedrock-runtime"].attempts == 1
    assert aws.CLIENT_LIMITS["bedrock-agent-runtime"].attempts == 2
    assert aws.CLIENT_LIMITS["ssm"].attempts == 2
    assert aws.CLIENT_LIMITS["dynamodb"].attempts == 2
    assert set(aws.CLIENT_LIMITS) == {"ssm", "dynamodb", "bedrock-agent-runtime", "bedrock-runtime"}


def test_the_budget_counter_waits_the_shortest() -> None:
    """It sits in front of every question, so a slow table must not hold one up for long."""
    counter = aws.CLIENT_LIMITS["dynamodb"]
    assert counter.connect_timeout + counter.read_timeout == 5
    assert all(
        counter.connect_timeout + counter.read_timeout < limits.connect_timeout + limits.read_timeout
        for service, limits in aws.CLIENT_LIMITS.items()
        if service != "dynamodb"
    )


def test_worst_case_is_every_attempt_timing_out() -> None:
    """ssm 2 × 22, budget 2 × (2 + 3) + 6, retrieve 2 × (3 + 10) + 6, converse 1 × (3 + 40).

    Twice the keys, because an unknown key makes the handler read them again
    before it answers 401; one call each for the other three.
    """
    assert aws.KEY_READS_PER_REQUEST == 2
    assert aws.worst_case_seconds() == 2 * 22 + 16 + 32 + 43 == 135


def test_clients_get_their_service_limits_and_are_made_once() -> None:
    pytest.importorskip("boto3")
    for service, limits in aws.CLIENT_LIMITS.items():
        made = aws.client(service, "us-east-1")
        assert made.meta.config.connect_timeout == limits.connect_timeout
        assert made.meta.config.read_timeout == limits.read_timeout
        assert made.meta.config.retries == {"mode": "standard", "total_max_attempts": limits.attempts}
        assert aws.client(service, "us-east-1") is made


def test_a_missing_boto3_is_an_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """aws.py knows nothing about HTTP; the handler turns this into a configuration error."""
    real_import = builtins.__import__

    def without_boto3(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "boto3":
            raise ImportError("simulated: boto3 not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_boto3)
    with pytest.raises(ImportError):
        aws.client("ssm", "us-east-1")


def test_sdk_versions_name_boto3_and_botocore() -> None:
    pytest.importorskip("boto3")
    assert set(aws.sdk_versions()) == {"boto3", "botocore"}


# --- Error causes --------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ThrottlingException", "throttled"),
        ("ServiceQuotaExceededException", "throttled"),
        ("AccessDeniedException", "access denied"),
        ("ResourceNotFoundException", "not found"),
        ("ModelNotReadyException", "unavailable"),
        ("ValidationException", "ValidationException"),
    ],
)
def test_error_cause_names_the_cause(code: str, expected: str) -> None:
    assert expected in aws.error_cause(client_error(code))
    assert aws.error_code(client_error(code)) == code


def test_error_cause_for_timeouts_and_unreachable_endpoints() -> None:
    exceptions = pytest.importorskip("botocore.exceptions")
    assert "timeout" in aws.error_cause(exceptions.ReadTimeoutError(endpoint_url="https://x"))
    assert "timeout" in aws.error_cause(exceptions.ConnectTimeoutError(endpoint_url="https://x"))
    assert "could not be reached" in aws.error_cause(exceptions.EndpointConnectionError(endpoint_url="https://x"))


def test_error_cause_never_repeats_the_message() -> None:
    """AWS messages carry ARNs, and ARNs the account id."""
    error = client_error("AccessDeniedException", "User: arn:aws:sts::123456789012:assumed-role/x is not authorized")
    assert "123456789012" not in aws.error_cause(error)


def test_error_code_of_something_that_is_no_client_error() -> None:
    assert aws.error_code(ValueError("a bug")) is None
    assert aws.error_cause(ValueError("a bug")) == "ValueError"


def test_botocore_errors_are_the_two_base_classes() -> None:
    exceptions = pytest.importorskip("botocore.exceptions")
    assert aws.botocore_errors() == (exceptions.BotoCoreError, exceptions.ClientError)

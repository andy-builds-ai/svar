"""AWS plumbing shared by the scripts: clients, stack outputs, readable errors."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

STACK_NAME = "AndurStack"
REGION = "eu-west-1"

#: Error codes AWS returns when the credentials themselves are the problem.
_CREDENTIAL_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "InvalidSignatureException",
}

_LOGIN_REFRESH_OPERATIONS = {"CreateOAuth2Token"}

LOGIN_HINT = "Sign in again with `aws login`, then rerun."


class AwsProblem(Exception):
    """An AWS step failed for a reason the user can act on."""


def client(service: str, region: str) -> Any:
    """A boto3 client, or an `AwsProblem` if boto3 is not installed."""
    try:
        import boto3
    except ImportError as exc:
        raise AwsProblem(
            "boto3 is not installed. Run `pip install -e .` in the repository root."
        ) from exc
    with explained(f"Connecting to AWS ({service})"):
        return boto3.client(service, region_name=region)


def error_code(exc: BaseException | None) -> str | None:
    """The AWS error code of a botocore `ClientError`, or None for anything else."""
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return None
    return response.get("Error", {}).get("Code")


def is_login_problem(exc: BaseException) -> bool:
    """True when the credentials are missing or expired, not the request wrong."""
    code = error_code(exc)
    if code is not None:
        return (
            code in _CREDENTIAL_ERROR_CODES
            or getattr(exc, "operation_name", None) in _LOGIN_REFRESH_OPERATIONS
        )
    name = type(exc).__name__
    return any(word in name for word in ("Credential", "Token", "Login"))


def explain(action: str, exc: BaseException) -> str:
    """Turn a botocore exception into one or two lines a person can act on."""
    code = error_code(exc)
    name = type(exc).__name__
    if is_login_problem(exc):
        return f"{action} failed: the AWS login is missing or expired ({name}: {exc}). {LOGIN_HINT}"
    if code in {"AccessDenied", "AccessDeniedException"}:
        return f"{action} failed: access denied. The signed-in identity lacks a permission. ({exc})"
    if name == "EndpointConnectionError":
        return f"{action} failed: AWS could not be reached. Check the network connection. ({exc})"
    return f"{action} failed: {exc}"


@contextmanager
def explained(action: str) -> Iterator[None]:
    """Re-raise botocore exceptions as `AwsProblem` with a readable message."""
    try:
        yield
    except Exception as exc:
        if not type(exc).__module__.startswith("botocore"):
            raise
        raise AwsProblem(explain(action, exc)) from exc


def read_stack_outputs(cloudformation: Any, stack_name: str) -> dict[str, str]:
    """The outputs of a deployed stack, as ``{OutputKey: OutputValue}``."""
    try:
        with explained(f"Reading the outputs of stack {stack_name}"):
            response = cloudformation.describe_stacks(StackName=stack_name)
    except AwsProblem as problem:
        # CloudFormation reports a missing stack as a generic ValidationError.
        if error_code(problem.__cause__) == "ValidationError" and "does not exist" in str(
            problem.__cause__
        ):
            raise AwsProblem(
                f"Stack {stack_name} was not found in this region. Deploy it first with "
                "`cdk deploy`, or pass --stack-name / --region."
            ) from problem.__cause__
        raise
    stacks = response.get("Stacks", [])
    if not stacks:
        raise AwsProblem(f"Stack {stack_name} was not found.")
    return {item["OutputKey"]: item["OutputValue"] for item in stacks[0].get("Outputs", [])}


def resolve(overrides: Mapping[str, str | None], load_outputs: Any) -> dict[str, str]:
    """Fill in every setting not given on the command line from the stack outputs."""
    missing = [key for key, value in overrides.items() if not value]
    outputs = load_outputs() if missing else {}
    resolved: dict[str, str] = {}
    for key, value in overrides.items():
        if value:
            resolved[key] = value
        elif key in outputs:
            resolved[key] = outputs[key]
        else:
            raise AwsProblem(f"The stack has no output {key}. Is it the Andur stack?")
    return resolved

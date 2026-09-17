"""Creates the key of one workspace in Parameter Store, during `cdk deploy`.

CloudFormation cannot create SecureString parameters itself, so the stack
runs this function as a custom resource, once per workspace. The key is
generated here, inside AWS: it never appears in the template, in cdk.out or
in a deploy log.

    Create  store a new key, unless the parameter already exists
    Update  the same: an existing key is kept, a missing one is created
    Delete  remove the parameter

The CDK provider framework calls `on_event` and reports the result back to
CloudFormation. boto3 comes from the Lambda runtime and is imported lazily,
so the tests run against a fake client without it.
"""

import secrets
from collections.abc import Mapping
from typing import Any

#: 32 random bytes, written as 43 URL-safe characters.
KEY_BYTES = 32


def new_key() -> str:
    """A fresh random key, safe to send in an HTTP header."""
    return secrets.token_urlsafe(KEY_BYTES)


def parameter_name(event: Mapping[str, Any]) -> str:
    """The parameter this resource manages, checked against the path it must live under."""
    properties = event["ResourceProperties"]
    name, path = properties["ParameterName"], properties["ParameterPath"]
    if not name.startswith(path + "/") or "/" in name.removeprefix(path + "/"):
        raise ValueError(f"Parameter {name!r} is not directly under {path!r}.")
    return name


def ensure_key(ssm: Any, name: str) -> None:
    """Store a new key under ``name`` unless a key is already there."""
    try:
        ssm.put_parameter(
            Name=name,
            Description="Andur workspace key. Send it as Authorization: Bearer <key>.",
            Value=new_key(),
            Type="SecureString",
            Tier="Standard",
            Overwrite=False,
        )
    except ssm.exceptions.ParameterAlreadyExists:
        pass


def delete_key(ssm: Any, name: str) -> None:
    """Remove the key under ``name``; a key that is already gone is fine."""
    try:
        ssm.delete_parameter(Name=name)
    except ssm.exceptions.ParameterNotFound:
        pass


def on_event(event: Mapping[str, Any], context: Any, ssm: Any = None) -> dict[str, str]:
    """Handle one CloudFormation lifecycle event for one workspace key.

    The physical resource id is the parameter name. When a name changes,
    CloudFormation sends a Delete for the old id after the Update, so an old
    key does not stay behind. A Delete removes only the parameter this
    resource created, never whatever name the event claims.

    A Delete never fails. With properties that do not name a key under the
    path, the Create would have failed the same check, so there is no key to
    remove — and a failing Delete would leave the stack stuck in
    DELETE_FAILED or a rollback unable to finish.
    """
    if event["RequestType"] == "Delete":
        return on_delete(event, ssm)
    name = parameter_name(event)
    ensure_key(ssm or ssm_client(), name)
    return {"PhysicalResourceId": name}


def on_delete(event: Mapping[str, Any], ssm: Any = None) -> dict[str, str]:
    """Remove the key this resource created, if the event really names it."""
    physical_id = event.get("PhysicalResourceId", "")
    try:
        name = parameter_name(event)
    except (AttributeError, KeyError, TypeError, ValueError):
        return {"PhysicalResourceId": physical_id}
    if physical_id == name:
        delete_key(ssm or ssm_client(), name)
    return {"PhysicalResourceId": physical_id}


def ssm_client() -> Any:
    """A Parameter Store client from the runtime's boto3."""
    import boto3

    return boto3.client("ssm")

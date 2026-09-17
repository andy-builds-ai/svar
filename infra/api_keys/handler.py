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
    """
    if ssm is None:
        import boto3

        ssm = boto3.client("ssm")
    name = parameter_name(event)
    if event["RequestType"] == "Delete":
        if event.get("PhysicalResourceId") == name:
            delete_key(ssm, name)
        return {"PhysicalResourceId": event.get("PhysicalResourceId", name)}
    ensure_key(ssm, name)
    return {"PhysicalResourceId": name}

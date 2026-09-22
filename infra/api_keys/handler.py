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

#: What the SSM console shows next to the key. Whoever reads a key there reads
#: this line with it, so it has to name the header the function actually
#: checks: ``X-Andur-Key``, not ``Authorization``. CloudFront replaces
#: ``Authorization`` with its own signature on the way to the function, so a
#: key sent there never arrives and the answer is 401 (app/handler.py,
#: ``KEY_HEADER``).
#:
#: ``Overwrite=False`` means no deploy rewrites the description of a parameter
#: that already exists. A key created before this line changed keeps the old
#: text until someone edits it in the console.
KEY_DESCRIPTION = "Andur workspace key. Send it in the header X-Andur-Key."


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
    """Store a new key under ``name`` unless a key is already there.

    ``Overwrite=False`` is what decides, so every deploy generates a key that
    is thrown away again when one exists. That is the intended trade: letting
    the store refuse the write is one round trip and cannot race, while
    looking first and writing after can.
    """
    try:
        ssm.put_parameter(
            Name=name,
            Description=KEY_DESCRIPTION,
            Value=new_key(),
            Type="SecureString",
            Tier="Standard",
            Overwrite=False,
        )
    except ssm.exceptions.ParameterAlreadyExists:
        pass


def note(message: str) -> None:
    """One line into the function's log. Lambda takes stdout to CloudWatch."""
    print(message)


def delete_key(ssm: Any, name: str) -> None:
    """Remove the key under ``name``. A key already gone is fine — and so is
    a delete that fails for any other reason.

    "A Delete never fails" has to hold against throttling, a denied
    permission and an unreachable endpoint too, not only against a missing
    parameter: an exception here leaves the stack in DELETE_FAILED, or a
    rollback unable to finish. A key nobody can reach any more is a smaller
    problem than a stack nobody can move. What went wrong goes to the log.
    """
    try:
        ssm.delete_parameter(Name=name)
    except ssm.exceptions.ParameterNotFound:
        pass
    except Exception as exc:  # noqa: BLE001 — a Delete may not fail
        note(f"delete_parameter failed for {name}: {exc!r}")


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
        try:
            delete_key(ssm or ssm_client(), name)
        except Exception as exc:  # noqa: BLE001 — a Delete may not fail
            # `ssm_client()` imports boto3 and builds a client; both can fail,
            # and a Delete that raises here is the same stuck stack as one
            # that raises inside `delete_parameter`.
            note(f"could not delete {name}: {exc!r}")
    return {"PhysicalResourceId": physical_id}


def ssm_client() -> Any:
    """A Parameter Store client from the runtime's boto3."""
    import boto3

    return boto3.client("ssm")

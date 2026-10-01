"""Lets go of the workspace keys the stack used to create, without deleting them.

Until 2026-10-01 the stack created one key per workspace as a custom resource
(``Custom::AndurApiKey``), and the Delete of such a resource deleted the key.
Keys are now added and removed with ``python -m scripts.workspace``, and the
stack holds no key at all. The deploy that drops those resources from the
template still sends CloudFormation's Delete for each of them, to this
function. The function is updated in that same deploy before the Delete
arrives, because CloudFormation deletes removed resources only in the
cleanup phase, after every update has succeeded. So the Delete reaches this
code, which deletes nothing: every key keeps working, unchanged.

Once that deploy is done, this stack has no ``Custom::AndurApiKey`` left and
nothing calls this function any more. Still: remove it, with its provider, only
once no deployed stack holds a ``Custom::AndurApiKey`` any more, in any account
and region. A stack that skipped this deploy still has them, and its Delete
would then reach the old function, which may delete keys. One such stack
exists: the old one in us-east-1, deliberately left at a version with key
resources.

The CDK provider framework calls `on_event` and reports the result back to
CloudFormation.
"""

from collections.abc import Mapping
from typing import Any


def note(message: str) -> None:
    """One line into the function's log. Lambda takes stdout to CloudWatch."""
    print(message)


def on_event(event: Mapping[str, Any], context: Any) -> dict[str, str]:
    """Answer a Delete with success and touch nothing; refuse anything else.

    A Delete never fails, as before: a failing one leaves a stack stuck. A
    Create or an Update cannot come from this stack any more, because it has no
    key resource left; one that does arrive fails loudly, since it would mean a
    template that still expects this function to make a key.
    """
    physical_id = event.get("PhysicalResourceId", "")
    if event.get("RequestType") == "Delete":
        note(f"kept {physical_id}: keys are managed by scripts/workspace.py now")
        return {"PhysicalResourceId": physical_id}
    raise ValueError(
        "This stack no longer creates workspace keys. Use `python -m scripts.workspace add`."
    )

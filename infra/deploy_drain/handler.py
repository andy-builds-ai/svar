"""Holds a deploy back until every answer that started on the old guardrail versions is done.

A deploy that changes a guardrail setting replaces that guardrail's version. CloudFormation
creates the new version first and deletes the old one in the cleanup phase, after every
update has succeeded. An answer that started before the function changed still runs with the
old versions, for up to the function's timeout; on 02.10.2026 the cleanup deleted the old
version 13 seconds after the function changed. This resource comes after the answer function
and waits longer than an answer may run, so the cleanup comes only after the last of them.

It reads nothing and changes nothing: its role may write its log, and that is all. The CDK
provider framework calls `on_event` and reports the result back to CloudFormation.
"""

import time
from collections.abc import Mapping
from typing import Any

#: One resource, one id: an Update never replaces it, so no Delete follows one.
PHYSICAL_ID = "andur-deploy-drain"


def note(message: str) -> None:
    """One line into the function's log. Lambda takes stdout to CloudWatch."""
    print(message)


def on_event(event: Mapping[str, Any], context: Any) -> dict[str, str]:
    """Wait on a Create or an Update, return at once on a Delete.

    A Create waits too: the deploy that adds this resource to a running stack replaces guardrail
    versions. On a stack built from nothing it waits for no one, and costs the time only.
    A Delete has nothing to wait for, and a failing one leaves a stack stuck.
    """
    if event.get("RequestType") != "Delete":
        seconds = int(event["ResourceProperties"]["Seconds"])
        note(f"waiting {seconds} s for the answers on the old guardrail versions")
        time.sleep(seconds)
    return {"PhysicalResourceId": PHYSICAL_ID}

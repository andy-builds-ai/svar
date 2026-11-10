"""The step that puts a workspace's key on a plan, or back on the demo budget.

The plan belongs to Svar: the answer function counts every question against it
(svar/app/budget.py, svar/app/plans.py). Taug's workspace script adds the key and calls this
step for it, `python -m taug.scripts.workspace add <workspace> --plan <plan>` and
`python -m taug.scripts.workspace plan <workspace> <plan>`, so a new key is on its plan before
anyone has it. Taug and Svar have stacks of their own, and Taug still calls Svar here, since
the budget table is Svar's; the other way round, Taug's key check is a library packed into
Svar's function (taug/access/).
"""

import hashlib
from typing import Any

from svar.app.plans import DEMO, PLANS, plan_named
from taug.scripts.aws import AwsProblem, error_code, explained

#: The partition key of the budget table: the SHA-256 of the key, in hex
#: (``BUDGET_KEY_ATTRIBUTE`` and ``key_hash`` in svar/app/budget.py).
BUDGET_KEY_ATTRIBUTE = "key_hash"

#: The attribute that names a key's plan, and the one the table's TTL reads
#: (``PLAN_ATTRIBUTE`` and ``BUDGET_TTL_ATTRIBUTE`` in svar/app/budget.py).
PLAN_ATTRIBUTE = "plan"
BUDGET_TTL_ATTRIBUTE = "delete_after"

#: What ``--plan`` and ``plan`` accept: the demo budget, or a plan of svar/app/plans.py.
PLAN_CHOICES = (DEMO, *(plan.name for plan in PLANS))

#: The answer function writes the workspace into every budget item it creates
#: (``BUDGET_UPDATE_EXPRESSION`` in svar/app/budget.py).
BUDGET_WORKSPACE_ATTRIBUTE = "workspace"


def key_hash(key: str) -> str:
    """The budget table's key for ``key``, computed as the answer function computes it."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def budget_key(key: str) -> dict[str, dict[str, str]]:
    return {BUDGET_KEY_ATTRIBUTE: {"S": key_hash(key)}}


# --- the step ----------------------------------------------------------------


def describe_plan(name: str) -> str:
    """What a key on this plan may ask, in one line."""
    plan = plan_named(name)
    if plan is None:
        return (
            "demo: the stack's demo budget (svar/app/README.md, 'Keys are budgets, not passwords')"
        )
    return (
        f"{plan.name}: {plan.questions_per_month:,} questions a calendar month, at most "
        f"{plan.questions_per_day:,} a day, German time"
    )


def set_plan(dynamodb: Any, table: str, key: str, workspace: str, name: str) -> str:
    """Put the key's budget item on the plan ``name``, or back on the demo budget; return
    the plan it was on.

    A plan item has no ``delete_after``: the table's TTL would otherwise delete it one day,
    and the key would fall back to the demo budget. Back on the demo, the next question
    that passes writes it again; on a key whose demo budget is spent none passes, and the
    item stays until `remove`. The counts stay either way, so changing the plan is no
    way to a fresh month. An item of another workspace is left alone.
    """
    request: dict[str, Any] = {
        "TableName": table,
        "Key": budget_key(key),
        "ConditionExpression": "attribute_not_exists(#workspace) OR #workspace = :workspace",
        "ExpressionAttributeNames": {
            "#workspace": BUDGET_WORKSPACE_ATTRIBUTE,
            "#plan": PLAN_ATTRIBUTE,
        },
        "ExpressionAttributeValues": {":workspace": {"S": workspace}},
        "ReturnValues": "ALL_OLD",
    }
    if name == DEMO:
        request["UpdateExpression"] = (
            "SET #workspace = if_not_exists(#workspace, :workspace) REMOVE #plan"
        )
    else:
        request["UpdateExpression"] = (
            "SET #plan = :plan, #workspace = if_not_exists(#workspace, :workspace)"
            f" REMOVE {BUDGET_TTL_ATTRIBUTE}"
        )
        request["ExpressionAttributeValues"][":plan"] = {"S": name}
    try:
        with explained(f"Setting the plan of the key in {table}"):
            response = dynamodb.update_item(**request)
    except AwsProblem as problem:
        if error_code(problem.__cause__) != "ConditionalCheckFailedException":
            raise
        raise AwsProblem(
            "The budget item of this key names another workspace. Nothing was changed."
        ) from problem.__cause__
    before = response.get("Attributes", {}).get(PLAN_ATTRIBUTE, {})
    return before.get("S", "unknown") if before else DEMO

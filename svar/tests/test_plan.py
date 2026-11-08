"""Tests of Svar's plan step (svar/scripts/plan.py), without AWS.

The fake table executes the update and its condition the way the answer function's tests do
(taug/tests/test_workspace.py), so these tests see the item the answer function would read."""

import plans
import pytest

from svar.scripts import plan as plan_step
from taug.scripts import workspace
from taug.scripts.aws import AwsProblem
from taug.tests.test_workspace import TABLE, FakeDynamoDB

KEY = "k3y"
PLAN = plans.PLANS[0].name


def test_a_plan_is_written_without_an_expiry_and_the_demo_was_before() -> None:
    """A key that asked on the demo budget has an item with an expiry; on a plan it loses it,
    or the table's TTL would one day delete the plan."""
    dynamodb = FakeDynamoDB()
    digest = plan_step.key_hash(KEY)
    dynamodb.items[digest] = {
        plan_step.BUDGET_KEY_ATTRIBUTE: {"S": digest},
        plan_step.BUDGET_WORKSPACE_ATTRIBUTE: {"S": "quillmoor"},
        plan_step.BUDGET_TTL_ATTRIBUTE: {"N": "1900000000"},
    }
    before = plan_step.set_plan(dynamodb, TABLE, KEY, "quillmoor", PLAN)
    assert before == plan_step.DEMO
    item = dynamodb.items[plan_step.key_hash(KEY)]
    assert item[plan_step.PLAN_ATTRIBUTE] == {"S": PLAN}
    assert item[plan_step.BUDGET_WORKSPACE_ATTRIBUTE] == {"S": "quillmoor"}
    assert plan_step.BUDGET_TTL_ATTRIBUTE not in item


def test_back_on_the_demo_the_plan_goes_and_the_old_one_is_returned() -> None:
    dynamodb = FakeDynamoDB()
    plan_step.set_plan(dynamodb, TABLE, KEY, "quillmoor", PLAN)
    before = plan_step.set_plan(dynamodb, TABLE, KEY, "quillmoor", plan_step.DEMO)
    assert before == PLAN
    assert plan_step.PLAN_ATTRIBUTE not in dynamodb.items[plan_step.key_hash(KEY)]


def test_an_item_of_another_workspace_is_left_alone() -> None:
    dynamodb = FakeDynamoDB()
    plan_step.set_plan(dynamodb, TABLE, KEY, "quillmoor", PLAN)
    with pytest.raises(AwsProblem, match="names another workspace. Nothing was changed."):
        plan_step.set_plan(dynamodb, TABLE, KEY, "pellwick", plan_step.DEMO)
    assert dynamodb.items[plan_step.key_hash(KEY)][plan_step.PLAN_ATTRIBUTE] == {"S": PLAN}


def test_any_other_failure_is_passed_on_as_it_is() -> None:
    dynamodb = FakeDynamoDB()
    dynamodb.fail_update = "ProvisionedThroughputExceededException"
    with pytest.raises(AwsProblem, match="ProvisionedThroughputExceededException") as caught:
        plan_step.set_plan(dynamodb, TABLE, KEY, "quillmoor", PLAN)
    assert "another workspace" not in str(caught.value)


def test_the_description_names_the_demo_and_every_plan() -> None:
    assert plan_step.describe_plan(plan_step.DEMO).startswith("demo: ")
    for plan in plans.PLANS:
        assert plan_step.describe_plan(plan.name).startswith(f"{plan.name}: ")


def test_taugs_workspace_script_runs_svars_step_and_offers_its_choices() -> None:
    """`workspace add --plan` and `workspace plan` stay as they were and call this step."""
    assert workspace.set_plan is plan_step.set_plan
    assert workspace.describe_plan is plan_step.describe_plan
    assert workspace.PLAN_CHOICES is plan_step.PLAN_CHOICES

"""Giving a counted question back to its key after an error that bought nothing."""

from typing import Any

import aws
import budget
import clients
import config
import errors
import log

#: The errors of a question that ended without an answer after it was counted, each from an
#: AWS call: the search, the topic check, the model. Given back only before the answer model
#: answered, and not when a timeout caused it or the topic verdict could not be read.
REFUNDABLE_ERRORS = (errors.RetrievalError, errors.LLMError, errors.TopicCheckError)

#: Takes back what `BUDGET_UPDATE_EXPRESSION` counted; never below zero, never on a plan.
DEMO_REFUND_EXPRESSION = "SET questions_used = questions_used - :one"
DEMO_REFUND_CONDITION = "attribute_not_exists(#plan) AND questions_used > :zero"

#: Takes back what `PLAN_UPDATE_EXPRESSION` counted, in the month and day it counted in.
PLAN_REFUND_EXPRESSION = "SET #month = #month - :one, #day = #day - :one"
PLAN_REFUND_CONDITION = "#month > :zero AND #day > :zero"


def refund_request(charge: budget.Charge) -> dict[str, Any]:
    """The update that takes one question back from what ``charge`` counted."""
    values = {":zero": {"N": "0"}, ":one": {"N": "1"}}
    if charge.plan_counts is None:
        return {
            "UpdateExpression": DEMO_REFUND_EXPRESSION,
            "ConditionExpression": DEMO_REFUND_CONDITION,
            "ExpressionAttributeNames": {"#plan": budget.PLAN_ATTRIBUTE},
            "ExpressionAttributeValues": values,
        }
    month, day = charge.plan_counts
    return {
        "UpdateExpression": PLAN_REFUND_EXPRESSION,
        "ConditionExpression": PLAN_REFUND_CONDITION,
        "ExpressionAttributeNames": {"#month": month, "#day": day},
        "ExpressionAttributeValues": values,
    }


def refund_question(
    settings: config.Settings,
    workspace: str,
    key: str,
    charge: budget.Charge,
    error: errors.HandlerError,
    elapsed: float,
    model_answered: bool = False,
) -> bool:
    """Give a question back to the key's budget after ``error``; whether it was.

    Never when the question may have bought model time for nothing: after a timeout, which may
    have run and been paid for; after the answer model answered, which was paid for, also when
    the answer was empty or could not be checked; after a topic verdict nobody could read,
    which follows from the text the caller wrote. Whoever could cause any of these would
    otherwise ask without budget. A refund that fails, or that would not end before the
    function's own timeout, leaves the question counted and says so in the log: the count may
    run ahead, never behind.
    """
    reason = type(error).__name__
    kept = (
        "timeout"
        if aws.timed_out(error)
        else "model answered"
        if model_answered
        else "unreadable verdict"
        if getattr(error, "unreadable", False)
        else None
    )
    if kept:
        log.log_event("budget-kept", workspace=workspace, error=reason, cause=kept)
        return False
    latest = aws.FUNCTION_TIMEOUT_SECONDS - aws.ORIGIN_MARGIN_SECONDS - aws.call_seconds("dynamodb")
    if elapsed > latest:
        log.log_event(
            "budget-kept",
            workspace=workspace,
            error=reason,
            cause="no time left",
            elapsed_ms=round(elapsed * 1000),
        )
        return False
    try:
        clients.client("dynamodb", settings.region).update_item(
            TableName=settings.budget_table,
            Key={budget.BUDGET_KEY_ATTRIBUTE: {"S": budget.key_hash(key)}},
            **refund_request(charge),
        )
    except aws.botocore_errors() as exc:
        log.log_event(
            "budget-refund-failed", workspace=workspace, error=reason, detail=aws.error_detail(exc)
        )
        return False
    log.log_event("budget-refunded", workspace=workspace, error=reason, plan=charge.plan_counts)
    return True

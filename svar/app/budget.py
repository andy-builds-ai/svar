"""Counting a question against its key: the demo budget, or the quota of a key's plan."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aws
import clients
import clocks
import config
import errors
import log
import plans
import sentences

BUDGET_KEY_ATTRIBUTE = "key_hash"
BUDGET_TTL_ATTRIBUTE = "delete_after"

BUDGET_UPDATE_EXPRESSION = (
    "SET questions_used = if_not_exists(questions_used, :zero) + :one,"
    " workspace = if_not_exists(workspace, :workspace),"
    " question_limit = if_not_exists(question_limit, :limit),"
    " window_seconds = if_not_exists(window_seconds, :window),"
    " keep_seconds = if_not_exists(keep_seconds, :keep),"
    " first_used_at = if_not_exists(first_used_at, :now),"
    " expires_at = if_not_exists(first_used_at, :now) + if_not_exists(window_seconds, :window),"
    " delete_after = if_not_exists(first_used_at, :now) + if_not_exists(keep_seconds, :keep)"
)

#: A key whose item names a plan is refused here and counted by `charge_plan`.
BUDGET_CONDITION = (
    "attribute_not_exists(#plan) AND ("
    "attribute_not_exists(first_used_at)"
    " OR ("
    "(attribute_not_exists(questions_used)"
    " OR attribute_not_exists(question_limit)"
    " OR questions_used < question_limit)"
    " AND (attribute_not_exists(expires_at) OR expires_at > :now)"
    ")"
    ")"
)

#: The attribute that names a key's plan. Always written through the
#: placeholder ``#plan``: PLAN is one of DynamoDB's reserved words.
PLAN_ATTRIBUTE = "plan"


def key_hash(key: str) -> str:
    """The name a key goes by in the budget table: its SHA-256, as hex."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def budget_values(settings: config.Settings, workspace: str, now: int) -> dict[str, dict[str, str]]:
    """The placeholders of the update: this moment, this workspace, the defaults."""
    return {
        ":zero": {"N": "0"},
        ":one": {"N": "1"},
        ":now": {"N": str(now)},
        ":workspace": {"S": workspace},
        ":limit": {"N": str(settings.question_limit)},
        ":window": {"N": str(settings.window_hours * config.SECONDS_PER_HOUR)},
        ":keep": {"N": str(settings.keep_days * config.SECONDS_PER_DAY)},
    }


def budget_number(item: Mapping[str, Any], name: str) -> int | None:
    """One whole number out of a DynamoDB item, or None if it is missing or is not one."""
    value = item.get(name, {})
    try:
        return int(value["N"])
    except (KeyError, TypeError, ValueError):
        return None


def spent_message(item: Mapping[str, Any], now: int) -> dict[str, str]:
    """Which half of the budget ran out, read from the item the condition refused, in each
    language."""
    expires_at = budget_number(item, "expires_at")
    if expires_at is not None and now >= expires_at:
        return dict(sentences.WINDOW_CLOSED_MESSAGES)
    used, limit = budget_number(item, "questions_used"), budget_number(item, "question_limit")
    if used is not None and limit is not None and used >= limit:
        return {
            language: text.format(limit=limit)
            for language, text in sentences.QUESTIONS_SPENT_MESSAGES.items()
        }
    return dict(sentences.BUDGET_SPENT_MESSAGES)


def refused_item(exc: BaseException) -> Mapping[str, Any]:
    """The item DynamoDB sends back with a failed condition, or an empty one."""
    response = getattr(exc, "response", None)
    item = response.get("Item") if isinstance(response, Mapping) else None
    return item if isinstance(item, Mapping) else {}


BUDGET_TTL_REPAIR_EXPRESSION = (
    "SET keep_seconds = window_seconds + :keep, delete_after = expires_at + :keep"
)


def keep_the_item_past_its_window(
    settings: config.Settings, key: str, item: Mapping[str, Any]
) -> None:
    """Make sure the item outlasts the window it carries, and say so when it did not."""
    expires_at, delete_after = (
        budget_number(item, "expires_at"),
        budget_number(item, "delete_after"),
    )
    if expires_at is None or delete_after is None or delete_after > expires_at:
        return
    try:
        clients.client("dynamodb", settings.region).update_item(
            TableName=settings.budget_table,
            Key={BUDGET_KEY_ATTRIBUTE: {"S": key_hash(key)}},
            UpdateExpression=BUDGET_TTL_REPAIR_EXPRESSION,
            ExpressionAttributeValues={
                ":keep": {"N": str(settings.keep_days * config.SECONDS_PER_DAY)}
            },
        )
    except aws.botocore_errors() as exc:
        log.log_event("budget-ttl-unrepaired", detail=aws.error_detail(exc))
    else:
        log.log_event("budget-ttl-repaired", expired_at=expires_at, deleted_after=delete_after)


def write_budget(
    settings: config.Settings, workspace: str, key: str, request: Mapping[str, Any]
) -> tuple[bool, Mapping[str, Any]]:
    """One conditional write to the key's item: whether it passed, and the item it wrote
    (when the request asks for it) or the item its condition refused."""
    try:
        written = clients.client("dynamodb", settings.region).update_item(
            TableName=settings.budget_table,
            Key={BUDGET_KEY_ATTRIBUTE: {"S": key_hash(key)}},
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
            **request,
        )
    except aws.botocore_errors() as exc:
        if aws.error_code(exc) != "ConditionalCheckFailedException":
            log.log_event("budget-unreadable", workspace=workspace, detail=aws.error_detail(exc))
            raise errors.ConfigurationError(
                f"The key budget could not be counted: {aws.error_cause(exc)}."
            ) from exc
        return False, refused_item(exc)
    attributes = written.get("Attributes") if isinstance(written, Mapping) else None
    return True, attributes if isinstance(attributes, Mapping) else {}


@dataclass(frozen=True)
class Charge:
    """What `charge_question` counted: the demo budget's questions, or a plan's counts of
    the question's month and day (`month_count`, `day_count`)."""

    plan_counts: tuple[str, str] | None = None


def charge_question(settings: config.Settings, workspace: str, key: str) -> Charge:
    """Count this question against the key's budget, or refuse it; what it counted.

    The first write is the demo budget's, and its condition refuses every item that
    names a plan. The refused item comes back with the refusal, so a key on a plan is
    counted by a second write, and nothing is read first.
    """
    now = int(clocks.wall_clock())
    passed, item = write_budget(
        settings,
        workspace,
        key,
        {
            "UpdateExpression": BUDGET_UPDATE_EXPRESSION,
            "ConditionExpression": BUDGET_CONDITION,
            "ExpressionAttributeNames": {"#plan": PLAN_ATTRIBUTE},
            "ExpressionAttributeValues": budget_values(settings, workspace, now),
            "ReturnValues": "ALL_NEW",
        },
    )
    if passed:
        keep_the_item_past_its_window(settings, key, item)
        return Charge()
    if PLAN_ATTRIBUTE in item:
        return Charge(charge_plan(settings, workspace, key, item, now))
    spent = spent_message(item, now)
    raise errors.BudgetSpent(spent["en"], spent["de"])


def last_sunday(year: int, month: int) -> date:
    """The last Sunday of a month."""
    last_day = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - 6) % 7)


def one_o_clock_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 1, tzinfo=UTC)


def german_date(now: int) -> date:
    """The calendar day in Germany at a moment given in epoch seconds.

    Central European Time is UTC+1, and UTC+2 in summer time, which runs from 01:00 UTC
    on the last Sunday of March to 01:00 UTC on the last Sunday of October (EU directive
    2000/84/EC). Written out rather than read from ``zoneinfo``: that needs a time zone
    database, and nothing says the Lambda runtime brings one.
    """
    moment = datetime.fromtimestamp(now, UTC)
    summer = (
        one_o_clock_utc(last_sunday(moment.year, 3))
        <= moment
        < one_o_clock_utc(last_sunday(moment.year, 10))
    )
    return (moment + timedelta(hours=2 if summer else 1)).date()


def month_count(day: date) -> str:
    """The attribute that counts the questions of this day's month: ``month_2026-10``."""
    return f"month_{day:%Y-%m}"


def day_count(day: date) -> str:
    """The attribute that counts the questions of this day: ``day_2026-10-01``."""
    return f"day_{day.isoformat()}"


MONTH_COUNT_NAME = re.compile(r"month_\d{4}-\d{2}\Z")
DAY_COUNT_NAME = re.compile(r"day_\d{4}-\d{2}-\d{2}\Z")

#: At most this many old counts go with one question; any more go with the next.
MAX_STALE_COUNTS = 10


def stale_counts(item: Mapping[str, Any], today: date) -> list[str]:
    """The counts of the item from before last month and from before yesterday.

    Last month's and yesterday's count stay: a question that set out a moment before
    midnight is counted there after a later one has opened the new day, and it has to
    find the count it belongs to, not a fresh one.
    """
    last_month = month_count(today.replace(day=1) - timedelta(days=1))
    yesterday = day_count(today - timedelta(days=1))
    stale = [
        name
        for name in item
        if (MONTH_COUNT_NAME.match(name) and name < last_month)
        or (DAY_COUNT_NAME.match(name) and name < yesterday)
    ]
    return sorted(stale)[:MAX_STALE_COUNTS]


#: Both counts of the question's day and month, in one write. Each question counts in
#: the day and month it set out in, so two questions around midnight never compete
#: for one attribute, and nothing has to be reset when a day or a month begins.
PLAN_UPDATE_EXPRESSION = (
    "SET #month = if_not_exists(#month, :zero) + :one,"
    " #day = if_not_exists(#day, :zero) + :one,"
    " workspace = if_not_exists(workspace, :workspace)"
    f" REMOVE {BUDGET_TTL_ATTRIBUTE}"
)

PLAN_CONDITION = (
    "(attribute_not_exists(#month) OR #month < :month_limit)"
    " AND (attribute_not_exists(#day) OR #day < :day_limit)"
)


def charge_plan(
    settings: config.Settings, workspace: str, key: str, item: Mapping[str, Any], now: int
) -> tuple[str, str]:
    """Count a question of a key on a plan, or refuse it with the quota it reached; the
    counts it raised, of the question's month and day.

    ``item`` is what the demo write refused. A plan the table does not know, and a count
    that is not a number, are refused too: a key on a plan never gets a question that
    nothing counted.
    """
    named = item[PLAN_ATTRIBUTE]
    plan = plans.plan_named(named.get("S") if isinstance(named, Mapping) else None)
    if plan is None:
        log.log_event("budget-plan-unknown", workspace=workspace, plan=repr(named)[:100])
        raise errors.ConfigurationError("The budget item of this key names no known plan.")
    today = german_date(now)
    month, day = month_count(today), day_count(today)
    stale = stale_counts(item, today)
    passed, refused = write_budget(
        settings,
        workspace,
        key,
        {
            "UpdateExpression": PLAN_UPDATE_EXPRESSION
            + "".join(f", #stale{number}" for number in range(len(stale))),
            "ConditionExpression": PLAN_CONDITION,
            "ExpressionAttributeNames": {
                "#month": month,
                "#day": day,
                **{f"#stale{number}": name for number, name in enumerate(stale)},
            },
            "ExpressionAttributeValues": {
                ":zero": {"N": "0"},
                ":one": {"N": "1"},
                ":workspace": {"S": workspace},
                ":month_limit": {"N": str(plan.questions_per_month)},
                ":day_limit": {"N": str(plan.questions_per_day)},
            },
        },
    )
    if passed:
        return month, day
    for count, limit, messages in (
        (month, plan.questions_per_month, sentences.MONTH_QUOTA_MESSAGES),
        (day, plan.questions_per_day, sentences.DAY_QUOTA_MESSAGES),
    ):
        used = budget_number(refused, count)
        if used is not None and used >= limit:
            raise errors.QuotaReached(
                messages["en"].format(limit=f"{limit:,}"),
                messages["de"].format(limit=f"{limit:,}".replace(",", ".")),
            )
    log.log_event("budget-item-broken", workspace=workspace, plan=plan.name, counts=[month, day])
    raise errors.ConfigurationError(f"The budget item of this key cannot count {month} or {day}.")

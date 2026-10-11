"""A question that fails on AWS's side after it was counted is given back to the key's budget,
unless a timeout caused it (app/refunds.py, `refund_question`)."""

import logging
from typing import Any

import answering
import aws as aws_module
import budget
import clients
import clocks
import config
import errors
import llm
import pytest
import refunds
import topic_check

from tests.test_handler import (  # noqa: F401 (aws, budget_clock and cold_start are fixtures)
    ENVIRONMENT,
    KEYS,
    FakeClock,
    FakeDynamoDB,
    FakeWallClock,
    aws,
    bedrock_response,
    budget_clock,
    budget_item,
    call,
    client_error,
    cold_start,
    event,
    in_,
    key_on_plan,
    log_lines,
    number,
)


def timeout() -> Exception:
    exceptions = pytest.importorskip("botocore.exceptions")
    return exceptions.ReadTimeoutError(endpoint_url="https://bedrock-runtime")


def refund_writes(aws: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: F811
    """The refund writes the function sent to the budget table."""
    return [
        update
        for update in aws["dynamodb"].calls
        if update["UpdateExpression"]
        in (refunds.DEMO_REFUND_EXPRESSION, refunds.PLAN_REFUND_EXPRESSION)
    ]


def fail_the_model_twice(aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(clocks, "pause", lambda seconds: None)
    aws["bedrock-runtime"].error = client_error("ThrottlingException")


@pytest.mark.parametrize(
    "fail",
    ["model rejected twice", "model access denied", "search unreachable"],
)
def test_a_question_that_fails_on_awss_side_is_given_back(
    aws: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail: str,
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    if fail == "model rejected twice":
        fail_the_model_twice(aws, monkeypatch)
    elif fail == "model access denied":
        aws["bedrock-runtime"].error = client_error("AccessDeniedException")
    else:
        aws["bedrock-agent-runtime"].error = client_error(
            "ServiceUnavailableException", operation="Retrieve"
        )
    status, body = call(event())
    assert status == 502
    assert body["refunded"] is True
    assert body["error"].endswith(" This question was not counted against your key.")
    assert number(budget_item(aws), "questions_used") == 0
    (line,) = [line for line in log_lines(caplog) if line["event"] == "budget-refunded"]
    assert line["workspace"] == "quillmoor" and line["plan"] is None


def test_the_caller_reads_that_the_question_was_given_back_in_german(
    aws: dict[str, Any],  # noqa: F811
) -> None:
    aws["bedrock-agent-runtime"].error = client_error("AccessDeniedException", operation="Retrieve")
    status, body = call(event(in_("de")))
    assert status == 502
    assert body["error"].endswith(" Diese Frage wurde Ihrem Schlüssel nicht angerechnet.")


def test_a_question_whose_topics_cannot_be_checked_is_given_back(
    aws: dict[str, Any],  # noqa: F811
) -> None:
    aws["guardrail"].question_error = client_error(
        "ServiceUnavailableException", operation="ApplyGuardrail"
    )
    status, body = call(event())
    assert status == 502 and body["refunded"] is True
    assert number(budget_item(aws), "questions_used") == 0


@pytest.mark.parametrize("service", ["bedrock-runtime", "bedrock-agent-runtime"])
def test_a_timeout_keeps_the_question_counted(
    aws: dict[str, Any],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    service: str,
) -> None:
    """A call that timed out may have run and been paid for: whoever could cause timeouts
    would otherwise get model time without budget."""
    caplog.set_level(logging.INFO, logger="andur")
    aws[service].error = timeout()
    status, body = call(event())
    assert status == 502
    assert "refunded" not in body
    assert "not counted" not in body["error"]
    assert number(budget_item(aws), "questions_used") == 1
    assert refund_writes(aws) == []
    (line,) = [line for line in log_lines(caplog) if line["event"] == "budget-kept"]
    assert line["cause"] == "timeout"


@pytest.mark.parametrize("fail", ["empty answer", "answer not checkable"])
def test_a_question_whose_answer_the_model_gave_stays_counted(
    aws: dict[str, Any],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    fail: str,
) -> None:
    """The model call was made and paid for: whoever could cause an empty answer, or one the
    topic check cannot read, would otherwise ask without budget."""
    caplog.set_level(logging.INFO, logger="andur")
    if fail == "empty answer":
        aws["bedrock-runtime"].response = bedrock_response([{"text": " "}])
    else:
        aws["guardrail"].topic_error = client_error(
            "ServiceUnavailableException", operation="ApplyGuardrail"
        )
    status, body = call(event())
    assert status == 502 and "refunded" not in body
    assert number(budget_item(aws), "questions_used") == 1
    assert refund_writes(aws) == []
    (line,) = [line for line in log_lines(caplog) if line["event"] == "budget-kept"]
    assert line["cause"] == "model answered"


def test_an_unreadable_topic_verdict_keeps_the_question() -> None:
    """It follows from the text the caller wrote, so it is not given back."""
    error = topic_check.topic_check_failed("INPUT", *topic_check.UNREADABLE_VERDICT)
    assert error.unreadable is True
    assert (
        topic_check.topic_check_failed("INPUT", "access denied", "access denied").unreadable
        is False
    )


def test_a_timeout_of_the_first_topic_call_stays_in_the_chain_when_the_next_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first call timed out, the call in its place was throttled: the error carries the
    timeout, and the question stays counted."""
    exceptions = pytest.importorskip("botocore.exceptions")
    failures = [
        exceptions.ReadTimeoutError(endpoint_url="https://bedrock-runtime"),
        client_error("ThrottlingException"),
    ]

    class Runtime:
        def converse(self, **_: Any) -> Any:
            raise failures.pop(0)

    settings = config.load_settings(
        {
            **{k: v for k, v in ENVIRONMENT.items() if not k.startswith("TOPIC_GUARDRAIL_")},
            "TOPIC_MODEL_ID": "openai.gpt-oss-120b-1:0",
            "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS": "0.0002",
            "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS": "0.00079",
        }
    )
    with pytest.raises(errors.TopicCheckError) as failed:
        topic_check.check_topics_with_model(Runtime(), settings, "INPUT", "Wie exportiere ich?")
    assert aws_module.timed_out(failed.value)
    assert "too many requests" in str(failed.value)


def test_a_rejection_that_follows_a_timeout_in_its_chain_keeps_the_question() -> None:
    """The topic model's first call timed out and the one in its place gave no verdict: the
    error is raised from the timeout, so the question stays counted."""
    exceptions = pytest.importorskip("botocore.exceptions")
    first = exceptions.ReadTimeoutError(endpoint_url="https://bedrock-runtime")
    try:
        try:
            raise first
        except exceptions.ReadTimeoutError as exc:
            raise topic_check.topic_check_failed("INPUT", *topic_check.UNREADABLE_VERDICT) from exc
    except errors.TopicCheckError as error:
        assert aws_module.timed_out(error)
    assert not aws_module.timed_out(errors.LLMError("no text"))


def test_a_refund_that_fails_leaves_the_question_counted_and_says_so(
    aws: dict[str, Any],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="andur")
    # The first write counts the question; the second, the refund, fails.
    aws["dynamodb"] = FakeDynamoDB(
        error=client_error("ProvisionedThroughputExceededException", operation="UpdateItem"),
        error_from_call=2,
    )
    aws["bedrock-runtime"].error = client_error("AccessDeniedException")
    status, body = call(event())
    assert status == 502
    assert "refunded" not in body and "not counted" not in body["error"]
    assert number(budget_item(aws), "questions_used") == 1
    (line,) = [line for line in log_lines(caplog) if line["event"] == "budget-refund-failed"]
    assert line["detail"] == "ClientError: ProvisionedThroughputExceededException"


def test_the_refund_is_sent_through_the_client_that_never_repeats(
    aws: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count goes through the table's client with two attempts, the refund through the
    one with a single attempt: sent twice, it would take the question back twice."""
    asked: list[str] = []
    through = clients.client
    monkeypatch.setattr(
        clients, "client", lambda service, region: asked.append(service) or through(service, region)
    )
    aws["bedrock-runtime"].error = client_error("AccessDeniedException")
    status, body = call(event())
    assert status == 502 and body["refunded"] is True
    assert asked.count("budget-refund") == 1
    assert asked.index("dynamodb") < asked.index("budget-refund")
    assert aws_module.CLIENT_LIMITS["budget-refund"].attempts == 1
    (refund,) = refund_writes(aws)
    assert refund["UpdateExpression"] == refunds.DEMO_REFUND_EXPRESSION


def test_a_refund_never_counts_below_zero(
    aws: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The item was set back by hand between the count and the failure: the refund's
    condition refuses, and nothing goes negative."""
    aws["bedrock-runtime"].error = client_error("AccessDeniedException")
    real_answer = answering.answer_question

    def reset_then_fail(*args: Any) -> Any:
        budget_item(aws)["questions_used"] = {"N": "0"}
        return real_answer(*args)

    monkeypatch.setattr(answering, "answer_question", reset_then_fail)
    status, body = call(event())
    assert status == 502 and "refunded" not in body
    assert number(budget_item(aws), "questions_used") == 0


def test_no_refund_when_it_would_not_end_before_the_functions_timeout(
    aws: dict[str, Any],  # noqa: F811
    cold_start: FakeClock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every timeout of a request adds up to 189 of the function's 195 seconds; a refund
    after that would end as a bare Lambda timeout, without the error and its reason."""
    caplog.set_level(logging.INFO, logger="andur")
    aws["bedrock-runtime"].error = client_error("AccessDeniedException")
    real_ask = llm.ask_model

    def slow_ask(*args: Any) -> Any:
        cold_start.advance(
            aws_module.FUNCTION_TIMEOUT_SECONDS
            - aws_module.ORIGIN_MARGIN_SECONDS
            - aws_module.call_seconds("budget-refund")
            + 1
        )
        return real_ask(*args)

    monkeypatch.setattr(llm, "ask_model", slow_ask)
    status, body = call(event())
    assert status == 502 and "refunded" not in body
    assert number(budget_item(aws), "questions_used") == 1
    (line,) = [line for line in log_lines(caplog) if line["event"] == "budget-kept"]
    assert line["cause"] == "no time left"


def test_a_key_on_a_plan_gets_its_question_back_in_the_day_and_month_it_was_counted(
    aws: dict[str, Any],  # noqa: F811
    budget_clock: FakeWallClock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted a moment before midnight, failed after it: the refund takes back the counts it
    was charged to, not the new day's."""
    key_on_plan(aws)
    budget_clock.now = 1_790_000_000.0
    counted_day = budget.german_date(int(budget_clock.now))
    month, day = budget.month_count(counted_day), budget.day_count(counted_day)

    def fail_tomorrow(*_: Any) -> Any:
        budget_clock.advance(24 * 60 * 60)
        raise errors.LLMError("The model could not answer: access denied.")

    monkeypatch.setattr(answering, "answer_question", fail_tomorrow)
    status, body = call(event())
    assert status == 502 and body["refunded"] is True
    item = budget_item(aws)
    assert (number(item, month), number(item, day)) == (0, 0)
    (refund,) = refund_writes(aws)
    assert refund["ExpressionAttributeNames"] == {"#month": month, "#day": day}


@pytest.mark.parametrize("status_code", [200, 402])
def test_only_a_failed_question_is_given_back(
    aws: dict[str, Any],  # noqa: F811
    status_code: int,
) -> None:
    if status_code == 402:
        budget_item_before = {
            budget.BUDGET_KEY_ATTRIBUTE: {"S": budget.key_hash(KEYS["quillmoor"])},
            "questions_used": {"N": "10"},
            "question_limit": {"N": "10"},
            "first_used_at": {"N": "1"},
            "expires_at": {"N": "9999999999"},
        }
        aws["dynamodb"].items[budget.key_hash(KEYS["quillmoor"])] = budget_item_before
    assert call(event())[0] == status_code
    assert refund_writes(aws) == []


def test_a_crash_is_not_given_back(
    aws: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug is no failure on AWS's side; it is fixed, not refunded."""

    def broken(*_: Any) -> Any:
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(answering, "answer_question", broken)
    assert call(event())[0] == 500
    assert number(budget_item(aws), "questions_used") == 1

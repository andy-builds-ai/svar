"""Vard's loop without AWS: a scripted model, and a second responsibility that is no receiving."""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from vard import engine
from vard.engine import Level, Responsibility, Tool


def use(name: str, given: Any, number: int = 1) -> dict[str, Any]:
    return {"toolUse": {"toolUseId": f"u{number}", "name": name, "input": given}}


def reply(*blocks: dict[str, Any], tokens: tuple[int, int] = (100, 10)) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": list(blocks)}},
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
    }


class Scripted:
    """A model that gives its replies in order and keeps every request."""

    def __init__(self, replies: Sequence[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(request)))
        return self.replies.pop(0)


def support_inbox(tickets: dict[str, str], sent: list[str]) -> Responsibility:
    """A tiny second responsibility, a software firm's support inbox: read a ticket, send a
    reply only with a person's approval, never delete, close to end."""

    def schema(*required: str) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {key: {"type": "string"} for key in required},
            "required": list(required),
        }

    def send(given: dict[str, Any]) -> dict[str, bool]:
        sent.append(given["text"])
        return {"sent": True}

    ticket, reply_ = schema("ticket"), schema("ticket", "text")
    return Responsibility(
        task="You answer the support inbox.",
        tools=(
            Tool(
                "get_ticket",
                "Read a ticket.",
                ticket,
                Level.ALLOWED,
                lambda given: {"text": tickets[given["ticket"]]},
            ),
            Tool("send_reply", "Send a reply.", reply_, Level.ASK, send),
            Tool("delete_ticket", "Delete a ticket.", ticket, Level.DENIED, dict),
            Tool("close_ticket", "Close a ticket.", ticket, Level.ALLOWED, dict, closes=True),
        ),
        max_steps=5,
    )


def test_a_second_responsibility_runs_without_a_change_to_the_loop() -> None:
    sent: list[str] = []
    model = Scripted(
        [
            reply(use("get_ticket", {"ticket": "T-1"})),
            reply({"text": "Ich antworte."}, use("send_reply", {"ticket": "T-1", "text": "Hallo"})),
            reply(use("close_ticket", {"ticket": "T-1"})),
        ]
    )
    outcome = engine.run(
        model,
        "some-model",
        support_inbox({"T-1": "Login geht nicht."}, sent),
        "Ticket T-1 ist eingegangen.",
        approve=lambda name, given: name == "send_reply",
    )
    assert (outcome.ended, outcome.steps, sent) == (engine.CLOSED, 3, ["Hallo"])
    assert [call.name for call in outcome.closing] == ["close_ticket"]
    assert outcome.invalid == []
    assert [tool["toolSpec"]["name"] for tool in model.requests[0]["toolConfig"]["tools"]] == [
        "get_ticket",
        "send_reply",
        "delete_ticket",
        "close_ticket",
    ]
    assert model.requests[0]["system"] == [{"text": "You answer the support inbox."}]


def test_a_tool_result_goes_back_as_text_with_the_id_of_its_call() -> None:
    model = Scripted(
        [
            reply(use("get_ticket", {"ticket": "T-1"}, 7)),
            reply(use("close_ticket", {"ticket": "T-1"})),
        ]
    )
    engine.run(model, "m", support_inbox({"T-1": "Läuft nicht."}, []), "T-1")
    conversation = model.requests[1]["messages"]
    assert [m["role"] for m in conversation] == ["user", "assistant", "user"]
    (result,) = conversation[2]["content"]
    assert result["toolResult"]["toolUseId"] == "u7"
    assert json.loads(result["toolResult"]["content"][0]["text"]) == {"text": "Läuft nicht."}


@pytest.mark.parametrize(
    "call, why",
    [
        (use("book_goods_receipt", {"ticket": "T-1"}), "There is no tool 'book_goods_receipt'"),
        (use("delete_ticket", {"ticket": "T-1"}), "delete_ticket is not permitted"),
        (use("send_reply", {"ticket": "T-1", "text": "x"}), "needs a person's approval"),
        (use("get_ticket", "T-1"), "must be a JSON object"),
        (use("get_ticket", {}), "get_ticket is missing ticket"),
    ],
    ids=["unknown", "denied", "not-approved", "no-object", "missing-field"],
)
def test_a_call_the_loop_may_not_run_is_refused_counted_and_told(
    call: dict[str, Any], why: str
) -> None:
    sent: list[str] = []
    model = Scripted([reply(call), reply(use("close_ticket", {"ticket": "T-1"}))])
    outcome = engine.run(model, "m", support_inbox({"T-1": "x"}, sent), "T-1")
    (invalid,) = outcome.invalid
    assert why in str(invalid.refused) and sent == []
    told = model.requests[1]["messages"][2]["content"][0]["toolResult"]["content"][0]["text"]
    assert why in json.loads(told)["error"]
    assert outcome.ended == engine.CLOSED


def test_an_approval_that_says_no_keeps_the_tool_from_running() -> None:
    sent: list[str] = []
    model = Scripted(
        [reply(use("send_reply", {"ticket": "T-1", "text": "x"})), reply({"text": "Gut."})]
    )
    outcome = engine.run(
        model, "m", support_inbox({}, sent), "T-1", approve=lambda name, given: False
    )
    assert sent == [] and len(outcome.invalid) == 1 and outcome.ended == engine.ANSWERED


def test_a_reply_without_a_tool_ends_the_run_unclosed() -> None:
    outcome = engine.run(Scripted([reply({"text": "Alles gut."})]), "m", support_inbox({}, []), "x")
    assert (outcome.ended, outcome.steps, outcome.text, outcome.closing) == (
        engine.ANSWERED,
        1,
        "Alles gut.",
        [],
    )


def test_the_step_limit_ends_a_model_that_never_closes() -> None:
    model = Scripted([reply(use("get_ticket", {"ticket": "T-1"})) for _ in range(9)])
    outcome = engine.run(model, "m", support_inbox({"T-1": "x"}, []), "T-1")
    assert (outcome.ended, outcome.steps, len(model.requests)) == (engine.STEP_LIMIT, 5, 5)


def test_every_call_of_a_reply_runs_before_a_closing_one_ends_it() -> None:
    sent: list[str] = []
    model = Scripted(
        [
            reply(
                use("close_ticket", {"ticket": "T-1"}, 1),
                use("send_reply", {"ticket": "T-1", "text": "Erledigt"}, 2),
            )
        ]
    )
    outcome = engine.run(
        model, "m", support_inbox({}, sent), "T-1", approve=lambda name, given: True
    )
    assert [call.name for call in outcome.calls] == ["close_ticket", "send_reply"]
    assert sent == ["Erledigt"] and outcome.steps == 1


def test_reasoning_and_empty_text_stay_out_of_the_conversation() -> None:
    model = Scripted(
        [
            reply(
                {"reasoningContent": {"reasoningText": {"text": "hm"}}},
                {"text": "  "},
                use("get_ticket", {"ticket": "T-1"}),
            ),
            reply(use("close_ticket", {"ticket": "T-1"})),
        ]
    )
    engine.run(model, "m", support_inbox({"T-1": "x"}, []), "T-1")
    assert model.requests[1]["messages"][1]["content"] == [use("get_ticket", {"ticket": "T-1"})]


def test_usage_is_kept_per_step() -> None:
    model = Scripted(
        [
            reply(use("get_ticket", {"ticket": "T-1"}), tokens=(100, 5)),
            reply(use("close_ticket", {"ticket": "T-1"}), tokens=(180, 7)),
        ]
    )
    outcome = engine.run(model, "m", support_inbox({"T-1": "x"}, []), "T-1")
    assert outcome.usage == [
        {"inputTokens": 100, "outputTokens": 5},
        {"inputTokens": 180, "outputTokens": 7},
    ]


@pytest.mark.parametrize(
    "tools, steps, message",
    [
        ((), 3, "closes"),
        (
            (
                Tool("a", "", {}, Level.ALLOWED, dict, closes=True),
                Tool("a", "", {}, Level.ALLOWED, dict),
            ),
            3,
            "share a name",
        ),
        ((Tool("a", "", {}, Level.ALLOWED, dict, closes=True),), 0, "at least 1"),
    ],
)
def test_a_broken_responsibility_is_refused(
    tools: tuple[Tool, ...], steps: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Responsibility("task", tools, steps)


def test_the_loop_knows_nothing_of_aws_or_receiving() -> None:
    source = engine.__file__
    text = open(source, encoding="utf-8").read()
    for word in ("boto", "infra", "scripts", "delivery", "purchase", "receiving"):
        assert word not in text


def test_the_reason_the_last_reply_stopped_is_kept() -> None:
    cut = {**reply({"text": "Ich prüfe gerade"}), "stopReason": "max_tokens"}
    outcome = engine.run(Scripted([cut]), "m", support_inbox({}, []), "x")
    assert (outcome.ended, outcome.stop_reason) == (engine.ANSWERED, "max_tokens")

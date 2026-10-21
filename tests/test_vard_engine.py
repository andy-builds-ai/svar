"""Vard's loop without AWS: a scripted model, and a second responsibility that is no receiving."""

import json
import re
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
    """A tiny second responsibility, a support inbox: read a ticket, send a reply only with a
    person's approval, never delete, close to end."""

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


def broken(error: Exception, closes: bool = False) -> Tool:
    def run(given: dict[str, Any]) -> Any:
        raise error

    return Tool("get_ticket" if not closes else "close_ticket", "", {}, Level.ALLOWED, run, closes)


def with_tool(tool: Tool) -> Responsibility:
    others = [t for t in support_inbox({}, []).tools if t.name != tool.name]
    return Responsibility("You answer the support inbox.", (tool, *others), max_steps=5)


def test_a_tool_that_fails_tells_the_model_and_the_loop_goes_on() -> None:
    model = Scripted(
        [reply(use("get_ticket", {"ticket": "T-1"})), reply(use("close_ticket", {"ticket": "T-1"}))]
    )
    outcome = engine.run(model, "m", with_tool(broken(TimeoutError("system down"))), "T-1")
    (failed,) = outcome.failed
    assert failed.failed == "TimeoutError: system down" and failed.result is None
    assert outcome.invalid == [] and outcome.ended == engine.CLOSED
    told = model.requests[1]["messages"][2]["content"][0]["toolResult"]["content"][0]["text"]
    assert json.loads(told) == {"error": "get_ticket failed: TimeoutError: system down"}


def test_a_closing_tool_that_fails_closes_nothing() -> None:
    model = Scripted([reply(use("close_ticket", {"ticket": "T-1"})), reply({"text": "Gut."})])
    outcome = engine.run(model, "m", with_tool(broken(KeyError("T-1"), closes=True)), "T-1")
    assert outcome.closing == [] and len(outcome.failed) == 1
    assert (outcome.ended, outcome.steps) == (engine.ANSWERED, 2)


class Strict(Scripted):
    """A model that checks each request the way Bedrock does (ToolUseBlock, ToolName, ToolUseId
    of the bedrock-runtime service model), and that every result answers a call before it."""

    def converse(self, **request: Any) -> dict[str, Any]:
        calls: set[str] = set()
        for message in request["messages"]:
            for block in message["content"]:
                if "toolUse" in block:
                    tool_use = block["toolUse"]
                    assert set(tool_use) >= {"toolUseId", "name", "input"}
                    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool_use["name"])
                    assert re.fullmatch(r"[a-zA-Z0-9_.:-]{1,64}", tool_use["toolUseId"])
                    assert tool_use["toolUseId"] not in calls
                    calls.add(tool_use["toolUseId"])
                if "toolResult" in block:
                    assert block["toolResult"]["toolUseId"] in calls
        return super().converse(**request)


@pytest.mark.parametrize(
    "name", ["functions.get_ticket", "get ticket", "", "x" * 65, None, 7], ids=str
)
def test_a_name_bedrock_would_refuse_is_refused_and_the_loop_goes_on(name: Any) -> None:
    model = Strict(
        [
            reply(use(name, {"ticket": "T-1"}, 3)),
            reply(use("get_ticket", {"ticket": "T-1"}, 4)),
            reply(use("close_ticket", {"ticket": "T-1"}, 5)),
        ]
    )
    outcome = engine.run(model, "m", support_inbox({"T-1": "x"}, []), "T-1")
    assert (outcome.ended, outcome.steps) == (engine.CLOSED, 3)
    (invalid,) = outcome.invalid
    assert invalid.name == str(name) and invalid.input == {"ticket": "T-1"}
    stand_in = model.requests[1]["messages"][1]["content"][0]["toolUse"]
    assert stand_in == {"toolUseId": "u3", "name": engine.STAND_IN, "input": {"ticket": "T-1"}}
    told = model.requests[1]["messages"][2]["content"][0]["toolResult"]
    assert (
        told["toolUseId"] == "u3" and repr(name) in json.loads(told["content"][0]["text"])["error"]
    )


def test_a_missing_or_doubled_id_and_an_input_that_is_no_object_get_valid_ones() -> None:
    double = use("get_ticket", {"ticket": "T-1"}, 1)
    model = Strict(
        [
            reply(
                {"toolUse": {"name": "get_ticket", "input": {"ticket": "T-1"}}},
                double,
                double,
                use("get_ticket", "T-1", 2),
            ),
            reply(use("close_ticket", {"ticket": "T-1"})),
        ]
    )
    outcome = engine.run(model, "m", support_inbox({"T-1": "x"}, []), "T-1")
    assert outcome.ended == engine.CLOSED and [c.input for c in outcome.invalid] == ["T-1"]
    history = [b["toolUse"] for b in model.requests[1]["messages"][1]["content"]]
    assert [b["toolUseId"] for b in history] == ["vard-1-1", "u1", "vard-1-3", "u2"]
    assert history[3]["input"] == {}
    results = [b["toolResult"]["toolUseId"] for b in model.requests[1]["messages"][2]["content"]]
    assert results == ["vard-1-1", "u1", "vard-1-3", "u2"]


@pytest.mark.parametrize("name", ["get ticket", "invalid_tool_name", "x" * 65])
def test_a_tool_whose_name_bedrock_would_refuse_cannot_be_offered(name: str) -> None:
    with pytest.raises(ValueError, match="1 to 64 letters"):
        Responsibility("task", (Tool(name, "", {}, Level.ALLOWED, dict, closes=True),), 3)


def test_an_id_the_model_sends_again_in_a_later_step_gets_one_of_the_loops_own() -> None:
    model = Strict(
        [
            reply(use("get_ticket", {"ticket": "T-1"}, 1)),
            reply(use("get_ticket", {"ticket": "T-1"}, 1)),
            reply(use("close_ticket", {"ticket": "T-1"}, 9)),
        ]
    )
    outcome = engine.run(model, "m", support_inbox({"T-1": "x"}, []), "T-1")
    assert (outcome.ended, outcome.steps, outcome.invalid) == (engine.CLOSED, 3, [])
    conversation = model.requests[2]["messages"]
    calls = [m["content"][0]["toolUse"]["toolUseId"] for m in conversation[1::2]]
    results = [m["content"][0]["toolResult"]["toolUseId"] for m in conversation[2::2]]
    assert calls == results == ["u1", "vard-2-1"]


def test_a_result_the_loop_cannot_tell_fails_the_tool_and_the_loop_goes_on() -> None:
    odd = Tool("get_ticket", "", {}, Level.ALLOWED, lambda given: {("a", "b"): 1})
    model = Strict(
        [
            reply(use("get_ticket", {"ticket": "T-1"})),
            reply(use("close_ticket", {"ticket": "T-1"}, 2)),
        ]
    )
    outcome = engine.run(model, "m", with_tool(odd), "T-1")
    (failed,) = outcome.failed
    assert failed.failed.startswith("TypeError") and outcome.ended == engine.CLOSED

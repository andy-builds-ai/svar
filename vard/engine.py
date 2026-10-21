"""Vard's loop: a model uses tools, turn after turn, until it closes its task or runs out of steps.

The loop knows no task of its own. A `Responsibility` brings the task text, the tools and the
step limit; each tool carries a permission level, the way Claude Code's permissions do. A call
the loop does not run (a tool that is not offered, one that is denied, one that waits for an
approval nobody gives, an input without its required fields) goes back to the model as a
refusal and counts as invalid. A tool that fails while it runs does not stop the loop either:
its error goes back to the model under the key "error", and the call keeps it; the model did nothing
wrong, so the call is not invalid. Into the conversation goes back only what Bedrock takes: a
call whose name, id or input it would refuse stands there with a valid stand-in, while the `Call`
keeps what the model sent and the refusal names it. The client is anything with Bedrock's
`converse`; the loop imports nothing of AWS.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: What Bedrock takes as a tool's name and as a call's id: the shapes ToolName and ToolUseId of
#: the bedrock-runtime service model, version 2023-09-30.
TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")
TOOL_USE_ID = re.compile(r"[a-zA-Z0-9_.:-]{1,64}")

#: Stands in the conversation for a name Bedrock would refuse; no tool may carry it.
STAND_IN = "invalid_tool_name"


class Level(StrEnum):
    #: Runs without asking.
    ALLOWED = "allowed"
    #: Runs only when `approve` says yes.
    ASK = "ask"
    #: Never runs.
    DENIED = "denied"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    #: The input's JSON schema; its top-level `required` fields are checked before the tool runs.
    schema: Mapping[str, Any]
    level: Level
    run: Callable[[dict[str, Any]], Any]
    #: Once a closing tool has run, the loop ends after the reply that called it.
    closes: bool = False

    def spec(self) -> dict[str, Any]:
        return {
            "toolSpec": {
                "name": self.name,
                "description": self.description,
                "inputSchema": {"json": dict(self.schema)},
            }
        }


@dataclass(frozen=True)
class Responsibility:
    task: str
    tools: tuple[Tool, ...]
    #: The most model calls a run may take.
    max_steps: int

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError(f"two tools share a name: {names}")
        unfit = [n for n in names if not TOOL_NAME.fullmatch(n) or n == STAND_IN]
        if unfit:
            raise ValueError(f"a tool's name holds 1 to 64 letters, digits, _ or -: {unfit}")
        if not any(tool.closes for tool in self.tools):
            raise ValueError("a responsibility needs a tool that closes it")
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")


@dataclass
class Call:
    name: str
    input: Any
    #: Why the loop did not run the call; None when it ran.
    refused: str | None = None
    result: Any = None
    closes: bool = False
    #: The error the tool raised while it ran; None when it did not.
    failed: str | None = None


#: How a run ended: a closing tool ran, the model answered without a tool, or the steps ran out.
CLOSED, ANSWERED, STEP_LIMIT = "closed", "answered", "step_limit"


@dataclass
class Outcome:
    ended: str = STEP_LIMIT
    steps: int = 0
    calls: list[Call] = field(default_factory=list)
    #: The text of the model's last reply, and Bedrock's reason it stopped there.
    text: str = ""
    stop_reason: str | None = None
    #: `usage` of every model call, in order.
    usage: list[dict[str, int]] = field(default_factory=list)

    @property
    def closing(self) -> list[Call]:
        """The closing calls that ran through; one that raised closed nothing."""
        return [c for c in self.calls if c.closes and c.refused is None and c.failed is None]

    @property
    def invalid(self) -> list[Call]:
        return [call for call in self.calls if call.refused is not None]

    @property
    def failed(self) -> list[Call]:
        return [call for call in self.calls if call.failed is not None]


#: Asked before a tool of level `ask` runs, with its name and input.
Approve = Callable[[str, dict[str, Any]], bool]


def refusal(
    tools: Mapping[str, Tool], name: Any, given: Any, approve: Approve | None
) -> str | None:
    """Why a call may not run, or None if it may."""
    tool = tools.get(name) if isinstance(name, str) else None
    if tool is None:
        rule = (
            ""
            if isinstance(name, str) and TOOL_NAME.fullmatch(name)
            else (" A tool's name holds only letters, digits, _ and -.")
        )
        return f"There is no tool {name!r}.{rule} The tools are: {', '.join(tools)}."
    if tool.level == Level.DENIED:
        return f"{tool.name} is not permitted."
    if not isinstance(given, dict):
        return f"The input of {tool.name} must be a JSON object."
    missing = [key for key in tool.schema.get("required", ()) if given.get(key) is None]
    if missing:
        return f"{tool.name} is missing {', '.join(missing)}."
    if tool.level == Level.ASK and not (approve is not None and approve(tool.name, given)):
        return f"{tool.name} needs a person's approval, and it was not given."
    return None


def handle(tools: Mapping[str, Tool], use: Mapping[str, Any], approve: Approve | None) -> Call:
    name: Any = use.get("name")
    given: Any = use.get("input")
    reason = refusal(tools, name, given, approve)
    if reason is not None:
        return Call(str(name), given, refused=reason)
    tool = tools[name]
    try:
        result = tool.run(given)
        json.dumps(result, ensure_ascii=False, default=str)  # a result it cannot tell fails too
    except Exception as exc:  # noqa: BLE001 - any error of the tool goes back to the model
        return Call(tool.name, given, closes=tool.closes, failed=f"{type(exc).__name__}: {exc}")
    return Call(tool.name, given, result=result, closes=tool.closes)


def answer(call: Call) -> Any:
    """What the model is told about a call: the refusal, the tool's error or its result."""
    if call.refused is not None:
        return {"error": call.refused}
    if call.failed is not None:
        return {"error": f"{call.name} failed: {call.failed}"}
    return call.result


def kept(content: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The blocks of a reply that go back into the conversation: text that is not empty, and
    tool calls. Reasoning stays out; not every model is known to take it back."""
    return [
        block
        for block in content
        if "toolUse" in block or (isinstance(block.get("text"), str) and block["text"].strip())
    ]


def sendable(use: Mapping[str, Any], step: int, index: int, taken: set[str]) -> dict[str, Any]:
    """A tool call as it goes back into the conversation, so that Bedrock takes the next request:
    a name it would refuse becomes the stand-in, an input that is no object an empty one, and an
    id that is missing, refused or taken already in this conversation one of the loop's own."""
    name, given, use_id = use.get("name"), use.get("input"), use.get("toolUseId")
    if not (isinstance(name, str) and TOOL_NAME.fullmatch(name)):
        name = STAND_IN
    if not (isinstance(use_id, str) and TOOL_USE_ID.fullmatch(use_id)) or use_id in taken:
        use_id, extra = f"vard-{step}-{index}", 0
        while use_id in taken:
            extra += 1
            use_id = f"vard-{step}-{index}-{extra}"
    taken.add(use_id)
    return {"toolUseId": use_id, "name": name, "input": given if isinstance(given, dict) else {}}


def run(
    client: Any,
    model_id: str,
    responsibility: Responsibility,
    message: str,
    *,
    approve: Approve | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> Outcome:
    """Work through one message: call the model, run or refuse its tool calls, repeat."""
    tools = {tool.name: tool for tool in responsibility.tools}
    specs = [tool.spec() for tool in responsibility.tools]
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": message}]}]
    outcome = Outcome()
    # Every call's id in the conversation; a result answers exactly one of them.
    taken: set[str] = set()
    while outcome.steps < responsibility.max_steps:
        response = client.converse(
            modelId=model_id,
            system=[{"text": responsibility.task}],
            messages=messages,
            toolConfig={"tools": specs},
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        outcome.steps += 1
        outcome.usage.append(dict(response.get("usage", {})))
        outcome.stop_reason = response.get("stopReason")
        content = response.get("output", {}).get("message", {}).get("content") or []
        outcome.text = "\n".join(b["text"] for b in content if isinstance(b.get("text"), str))
        uses = [block["toolUse"] for block in content if "toolUse" in block]
        if not uses:
            outcome.ended = ANSWERED
            return outcome
        sent, history = [], []
        for block in kept(content):
            if "toolUse" in block:
                sent.append(sendable(block["toolUse"], outcome.steps, len(sent) + 1, taken))
                block = {"toolUse": sent[-1]}
            history.append(block)
        messages.append({"role": "assistant", "content": history})
        results = []
        for use, as_sent in zip(uses, sent, strict=True):
            call = handle(tools, use, approve)
            outcome.calls.append(call)
            told = json.dumps(answer(call), ensure_ascii=False, default=str)
            results.append(
                {"toolResult": {"toolUseId": as_sent["toolUseId"], "content": [{"text": told}]}}
            )
        if outcome.closing:
            outcome.ended = CLOSED
            return outcome
        messages.append({"role": "user", "content": results})
    return outcome

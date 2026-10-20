"""Vard's loop: a model uses tools, turn after turn, until it closes its task or runs out of steps.

The loop knows no task of its own. A `Responsibility` brings the task text, the tools and the
step limit; each tool carries a permission level, the way Claude Code's permissions do. A call
the loop does not run (a tool that is not offered, one that is denied, one that waits for an
approval nobody gives, an input without its required fields) goes back to the model as a
refusal and counts as invalid. The client is anything with Bedrock's `converse`; the loop
imports nothing of AWS.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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
    #: The input's JSON schema; its `required` fields are checked before the tool runs.
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


#: How a run ended: a closing tool ran, the model answered without a tool, or the steps ran out.
CLOSED, ANSWERED, STEP_LIMIT = "closed", "answered", "step_limit"


@dataclass
class Outcome:
    ended: str = STEP_LIMIT
    steps: int = 0
    calls: list[Call] = field(default_factory=list)
    #: The text of the model's last reply.
    text: str = ""
    #: `usage` of every model call, in order.
    usage: list[dict[str, int]] = field(default_factory=list)

    @property
    def closing(self) -> list[Call]:
        return [call for call in self.calls if call.closes and call.refused is None]

    @property
    def invalid(self) -> list[Call]:
        return [call for call in self.calls if call.refused is not None]


#: Asked before a tool of level `ask` runs, with its name and input.
Approve = Callable[[str, dict[str, Any]], bool]


def refusal(
    tools: Mapping[str, Tool], name: Any, given: Any, approve: Approve | None
) -> str | None:
    """Why a call may not run, or None if it may."""
    tool = tools.get(name) if isinstance(name, str) else None
    if tool is None:
        return f"There is no tool {name!r}. The tools are: {', '.join(tools)}."
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
    return Call(tool.name, given, result=tool.run(given), closes=tool.closes)


def kept(content: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The blocks of a reply that go back into the conversation: text that is not empty, and
    tool calls. Reasoning stays out; not every model is known to take it back."""
    return [
        block
        for block in content
        if "toolUse" in block or (isinstance(block.get("text"), str) and block["text"].strip())
    ]


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
        content = response.get("output", {}).get("message", {}).get("content") or []
        outcome.text = "\n".join(b["text"] for b in content if isinstance(b.get("text"), str))
        uses = [block["toolUse"] for block in content if "toolUse" in block]
        if not uses:
            outcome.ended = ANSWERED
            return outcome
        messages.append({"role": "assistant", "content": kept(content)})
        results = []
        for use in uses:
            call = handle(tools, use, approve)
            outcome.calls.append(call)
            answer = {"error": call.refused} if call.refused is not None else call.result
            results.append(
                {
                    "toolResult": {
                        "toolUseId": use.get("toolUseId", ""),
                        "content": [{"text": json.dumps(answer, ensure_ascii=False, default=str)}],
                    }
                }
            )
        if outcome.closing:
            outcome.ended = CLOSED
            return outcome
        messages.append({"role": "user", "content": results})
    return outcome

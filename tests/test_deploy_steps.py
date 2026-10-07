"""A deploy that replaces a guardrail version, step by step, on the synthesised templates.

CloudFormation updates a stack along its references: a resource that names another one, or
depends on it, waits for it; resources that do not may go in any order. A replaced resource
keeps its old instance until the cleanup phase, after every update. These tests take the
template before and after a change, walk every order those rules allow, and check after
every step that each answer still running, on the old function or on the new one, may call
the model with the guardrail version it holds, and that this version still exists.

Measured on 02.10.2026 (CloudTrail), the steps are far apart: the role 23 to 24 seconds
before the function, the old version deleted 13 to 46 seconds after it. No AWS here: the
values CloudFormation fills in are made up below.
"""

import itertools
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.deploy_drain import handler as drain_handler
from infra.stacks import answer_function, guardrails as stack_guardrails
from infra.stacks.andur_stack import AndurStack
from infra.stacks.answer_function import (
    ANSWER_MODEL_ID,
    ANSWER_TIMEOUT_SECONDS,
    DEPLOY_DRAIN_SECONDS,
    EU_PROFILE_PREFIX,
)
from tests.test_stack import REGIONAL_MODEL_ID, deploy_context

ACCOUNT = "111122223333"
REGION = "eu-west-1"

#: The number of each guardrail version before the deploy, and of the one it makes.
OLD, NEW = "3", "4"

#: The environment variables of the answer, excerpt and topic guardrail, in the order of
#: `Synthesised.versions`.
PREFIXES = ("", "EXCERPT_", "TOPIC_")

#: The answer role's condition as deployed until this change, on version 3.
DEPLOYED_ROLE_CONDITION = {
    "StringEquals": {
        "bedrock:GuardrailIdentifier": {
            "Fn::Join": [
                "",
                [
                    {"Fn::GetAtt": ["AnswerGuardrail", "GuardrailArn"]},
                    ":",
                    {"Fn::GetAtt": ["AnswerGuardrailVersion", "Version"]},
                ],
            ]
        }
    }
}


# --- The stack before and after ------------------------------------------


@dataclass(frozen=True)
class Synthesised:
    """One template with the logical ids the walk needs."""

    resources: dict[str, Any]
    role: str
    function: str
    drain: str
    versions: tuple[str, str, str]
    entry_point: str


def synthesise(
    change: Callable[[pytest.MonkeyPatch], None] | None = None, model_id: str = ANSWER_MODEL_ID
) -> Synthesised:
    """The stack's template, after ``change`` patched the stack module, or as it is."""
    with pytest.MonkeyPatch.context() as patch:
        if change is not None:
            change(patch)
        stack = AndurStack(cdk.App(context=deploy_context()), "StepStack", answer_model_id=model_id)
        resources = Template.from_stack(stack).to_json()["Resources"]
    entry_point = (
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{model_id}"
        if model_id.startswith(EU_PROFILE_PREFIX)
        else f"arn:aws:bedrock:{REGION}::foundation-model/{model_id}"
    )
    return Synthesised(
        resources=resources,
        role=stack.get_logical_id(stack.answer_role.node.default_child),
        function=stack.get_logical_id(stack.answer_function.node.default_child),
        drain=stack.get_logical_id(stack.deploy_drain.node.default_child),
        versions=(
            stack.get_logical_id(stack.guardrail_version),
            stack.get_logical_id(stack.excerpt_guardrail_version),
            stack.get_logical_id(stack.topic_guardrail_version),
        ),
        entry_point=entry_point,
    )


def threshold_changed(patch: pytest.MonkeyPatch) -> None:
    """Replaces the answer guardrail's version."""
    patch.setattr(stack_guardrails, "GROUNDING_THRESHOLD", 0.75)


def topic_changed(patch: pytest.MonkeyPatch) -> None:
    """Replaces the version of the topic guardrail; the answer guardrail reads no topic."""
    name, definition, examples = stack_guardrails.DENIED_TOPICS[0]
    patch.setattr(
        stack_guardrails,
        "DENIED_TOPICS",
        ((name, definition + " Changed.", examples), *stack_guardrails.DENIED_TOPICS[1:]),
    )


def question_topics_on(patch: pytest.MonkeyPatch) -> None:
    """The stack of the first of the two deploys of 06.10.2026, the answer guardrail reading
    the question's topics; from it to the stack as it is, the second replaces the versions of
    the answer and the topic guardrail."""
    patch.setattr(stack_guardrails, "ANSWER_GUARDRAIL_READS_QUESTION_TOPICS", True)


def masking_changed(patch: pytest.MonkeyPatch) -> None:
    """Replaces the versions of the answer and the excerpt guardrail."""
    patch.setattr(stack_guardrails, "MASKED_PII_TYPES", ("NAME", "EMAIL", "PHONE"))


@pytest.fixture(scope="module")
def current() -> Synthesised:
    return synthesise()


def as_deployed_until_now(synthesised: Synthesised) -> Synthesised:
    """``synthesised`` with the role pinned to the version number and without the drain:
    the shape the stack is deployed in before this change."""
    resources = json.loads(json.dumps(synthesised.resources))
    for statement in invoke_statements(resources[synthesised.role]):
        if "bedrock:GuardrailIdentifier" in json.dumps(statement.get("Condition", {})):
            statement["Condition"] = DEPLOYED_ROLE_CONDITION
    del resources[synthesised.drain]
    return replace(synthesised, resources=resources)


# --- Filling in what CloudFormation fills in -----------------------------


def guardrail_arn(guardrail_id: str) -> str:
    return f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/{guardrail_id}"


def guardrail_id(resources: Mapping[str, Any], logical_id: str) -> str:
    """A guardrail's id is its logical id in lower case; a version's id is its guardrail's."""
    if resources[logical_id]["Type"] == "AWS::Bedrock::GuardrailVersion":
        logical_id = resources[logical_id]["Properties"]["GuardrailIdentifier"]["Fn::GetAtt"][0]
    return logical_id.lower()


def resolve(value: Any, resources: Mapping[str, Any], numbers: Mapping[str, str]) -> Any:
    """``value`` with every reference filled in; ``numbers`` gives each version's number."""
    if isinstance(value, list):
        return [resolve(item, resources, numbers) for item in value]
    if not isinstance(value, dict):
        return value
    if "Ref" in value:
        name = value["Ref"]
        pseudo = {"AWS::Partition": "aws", "AWS::Region": REGION, "AWS::AccountId": ACCOUNT}
        if name in pseudo:
            return pseudo[name]
        if name in numbers:  # a version's Ref is "<id>|<number>"
            return f"{guardrail_id(resources, name)}|{numbers[name]}"
        return name
    if "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return separator.join(resolve(part, resources, numbers) for part in parts)
    if "Fn::GetAtt" in value:
        logical_id, attribute = value["Fn::GetAtt"]
        if attribute == "GuardrailArn":
            return guardrail_arn(guardrail_id(resources, logical_id))
        if attribute == "GuardrailId":
            return guardrail_id(resources, logical_id)
        if attribute == "Version":
            return numbers[logical_id]
        return f"{logical_id}.{attribute}"
    return {key: resolve(item, resources, numbers) for key, item in value.items()}


# --- What the role allows ------------------------------------------------


def wildcard(pattern: str) -> re.Pattern[str]:
    """An IAM pattern: ``*`` any run of characters, ``?`` one, the rest literal."""
    return re.compile("".join({"*": ".*", "?": "."}.get(c, re.escape(c)) for c in pattern))


def arn_like(value: str, pattern: str) -> bool:
    """IAM's ArnLike: each of the six parts of the ARN matched on its own."""
    values, patterns = value.split(":", 5), pattern.split(":", 5)
    return len(values) == len(patterns) == 6 and all(
        wildcard(p).fullmatch(v) for v, p in zip(values, patterns, strict=True)
    )


def condition_holds(condition: Mapping[str, Any], context: Mapping[str, str]) -> bool:
    """Every operator of ``condition`` against the request's ``context``. A key the request
    does not carry fails its operator, as in IAM; an operator this file cannot evaluate fails
    the test instead of passing quietly."""
    for operator, keys in condition.items():
        for key, expected in keys.items():
            if key not in context:
                return False
            allowed = expected if isinstance(expected, list) else [expected]
            if operator == "StringEquals":
                matched = context[key] in allowed
            elif operator == "ArnLike":
                matched = any(arn_like(context[key], pattern) for pattern in allowed)
            elif operator == "StringNotLike":
                matched = not any(wildcard(pattern).fullmatch(context[key]) for pattern in allowed)
            else:
                raise AssertionError(f"no evaluation for the condition operator {operator}")
            if not matched:
                return False
    return True


def allows_invoke(statement: Mapping[str, Any]) -> bool:
    """Whether ``statement`` allows bedrock:InvokeModel, also through a wildcard action."""
    actions = (
        statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
    )
    return statement["Effect"] == "Allow" and any(
        wildcard(action.lower()).fullmatch("bedrock:invokemodel") for action in actions
    )


def invoke_statements(role: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every statement of the role's own policies that allows bedrock:InvokeModel."""
    return [
        statement
        for policy in role["Properties"]["Policies"]
        for statement in policy["PolicyDocument"]["Statement"]
        if allows_invoke(statement)
    ]


def may_invoke(
    role: Mapping[str, Any],
    entry_point: str,
    identifier: str | None,
    resources: Mapping[str, Any],
    numbers: Mapping[str, str],
) -> bool:
    """Whether ``role`` lets a model call through ``entry_point`` with the guardrail
    ``identifier`` go through; ``None`` is a call without a guardrail."""
    context = {} if identifier is None else {"bedrock:GuardrailIdentifier": identifier}
    for statement in invoke_statements(role):
        resolved = resolve(statement, resources, numbers)
        targets = resolved["Resource"]
        targets = targets if isinstance(targets, list) else [targets]
        if any(wildcard(target).fullmatch(entry_point) for target in targets) and (
            condition_holds(resolved.get("Condition", {}), context)
        ):
            return True
    return False


# --- The walk ------------------------------------------------------------


def depends_on(resources: Mapping[str, Any], logical_id: str) -> set[str]:
    """Every resource ``logical_id`` names or depends on, directly or through others."""

    def direct(value: Any) -> Iterator[str]:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "Ref" and item in resources:
                    yield item
                elif key == "Fn::GetAtt":
                    yield item[0]
                else:
                    yield from direct(item)
        elif isinstance(value, list):
            for item in value:
                yield from direct(item)

    found: set[str] = set()
    waiting = [logical_id]
    while waiting:
        resource = resources[waiting.pop()]
        dependencies = resource.get("DependsOn", [])
        named = set(direct(resource.get("Properties", {})))
        named |= {dependencies} if isinstance(dependencies, str) else set(dependencies)
        for other in named - found:
            found.add(other)
            waiting.append(other)
    return found


@dataclass
class Moment:
    """What runs and what exists at one point of a deploy."""

    role: Mapping[str, Any]
    role_numbers: Mapping[str, str]
    running: list[Mapping[str, str]]
    existing: set[tuple[str, str]]


def environment(synthesised: Synthesised, numbers: Mapping[str, str]) -> Mapping[str, str]:
    """The answer function's environment as CloudFormation fills it in."""
    variables = synthesised.resources[synthesised.function]["Properties"]["Environment"]
    return resolve(variables["Variables"], synthesised.resources, numbers)


def problems_at(moment: Moment, step: str, after: Synthesised) -> list[str]:
    """What fails for an answer running at ``moment``: a refused model call, a missing
    version."""
    found = []
    for variables in moment.running:
        identifier = f"{guardrail_arn(variables['GUARDRAIL_ID'])}:{variables['GUARDRAIL_VERSION']}"
        if not may_invoke(
            moment.role, after.entry_point, identifier, after.resources, moment.role_numbers
        ):
            found.append(f"after {step}: the role refuses {identifier}")
        for prefix in PREFIXES:
            version = (variables[f"{prefix}GUARDRAIL_ID"], variables[f"{prefix}GUARDRAIL_VERSION"])
            if version not in moment.existing:
                found.append(f"after {step}: version {version} no longer exists")
    return found


def walk(before: Synthesised, after: Synthesised) -> tuple[list[list[str]], list[str]]:
    """Every order CloudFormation may take through the resources that matter here, and what
    fails in any of them."""
    old_numbers = dict.fromkeys(before.versions, OLD)
    replaced = [
        version
        for version in after.versions
        if before.resources[version]["Properties"] != after.resources[version]["Properties"]
    ]
    new_numbers = {**old_numbers, **dict.fromkeys(replaced, NEW)}

    def changed(logical_id: str) -> bool:
        """Changed as CloudFormation sees it: in the values it fills in, not in the text."""
        if logical_id not in before.resources:
            return True
        return resolve(before.resources[logical_id], before.resources, old_numbers) != resolve(
            after.resources[logical_id], after.resources, new_numbers
        )

    steps = [
        logical_id
        for logical_id in [*replaced, after.role, after.function, after.drain]
        if logical_id in after.resources and changed(logical_id)
    ]
    needs = {step: depends_on(after.resources, step) & set(steps) for step in steps}
    orders = [
        list(order)
        for order in itertools.permutations(steps)
        if all(needs[step] <= set(order[: order.index(step)]) for step in order)
    ]

    old_env = environment(before, old_numbers)
    new_env = environment(after, new_numbers)
    replaced_ids = {guardrail_id(after.resources, version) for version in replaced}
    problems: list[str] = []
    for order in orders:
        moment = Moment(
            role=before.resources[before.role],
            role_numbers=old_numbers,
            running=[old_env],
            existing={(old_env[f"{prefix}GUARDRAIL_ID"], OLD) for prefix in PREFIXES},
        )
        function_changed = False
        for step in order:
            if step in replaced:
                moment.existing.add((guardrail_id(after.resources, step), NEW))
            elif step == after.role:
                moment.role, moment.role_numbers = after.resources[after.role], new_numbers
            elif step == after.function:
                # New answers start on the new function; the old one's run on, up to its
                # timeout.
                moment.running = [old_env, new_env]
                function_changed = True
            elif step == after.drain:
                seconds = int(after.resources[after.drain]["Properties"]["Seconds"])
                if function_changed and seconds > ANSWER_TIMEOUT_SECONDS:
                    moment.running = [new_env]
            problems += problems_at(moment, step, after)
        # Cleanup, after every update: the replaced versions are deleted.
        moment.existing = {
            (guardrail, number)
            for guardrail, number in moment.existing
            if not (number == OLD and guardrail in replaced_ids)
        }
        problems += problems_at(moment, "cleanup", after)
    return orders, problems


# --- Tests ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "replaced"),
    [
        (threshold_changed, {"AnswerGuardrailVersion"}),
        (topic_changed, {"TopicGuardrailVersion"}),
        (masking_changed, {"AnswerGuardrailVersion", "ExcerptGuardrailVersion"}),
    ],
)
def test_every_step_of_a_new_guardrail_version_lets_old_and_new_answers_through(
    current: Synthesised, change: Callable[[pytest.MonkeyPatch], None], replaced: set[str]
) -> None:
    """From now on: the old answers keep their version and their right until they are done,
    the new ones have theirs from the first step they run in."""
    before = synthesise(change)
    assert {
        version
        for version in current.versions
        if before.resources[version]["Properties"] != current.resources[version]["Properties"]
    } == replaced
    orders, problems = walk(before, current)
    assert orders
    assert problems == []


def test_the_second_deploy_of_the_question_check_lets_old_and_new_answers_through(
    current: Synthesised,
) -> None:
    """From the first deploy, with the answer guardrail reading the question's topics, to the
    second without them, as it is: two versions replaced, every step safe."""
    before = synthesise(question_topics_on)
    assert {
        version
        for version in current.versions
        if before.resources[version]["Properties"] != current.resources[version]["Properties"]
    } == {"AnswerGuardrailVersion", "TopicGuardrailVersion"}
    orders, problems = walk(before, current)
    assert orders
    assert problems == []


def test_the_deploy_of_this_change_lets_old_and_new_answers_through(
    current: Synthesised,
) -> None:
    """The first deploy starts from the role pinned to version 3 and from no drain: the role
    widens before the function changes, and the drain is created after it. The change
    replaces the answer guardrail's version, the one the pinned role names."""
    before = as_deployed_until_now(synthesise(threshold_changed))
    orders, problems = walk(before, current)
    assert orders
    assert all(order.index(current.role) < order.index(current.function) for order in orders)
    assert problems == []


def test_the_old_form_fails_the_same_walk(current: Synthesised) -> None:
    """The walk finds the 502 of 02.10.2026: with the role pinned to the version and no
    drain, the old function's call is refused, and its version is deleted while it runs. The
    answer guardrail's version is replaced, as it was then; a changed topic no longer replaces
    it."""
    before = as_deployed_until_now(synthesise(threshold_changed))
    _, problems = walk(before, as_deployed_until_now(current))
    assert any("the role refuses" in problem for problem in problems)
    assert any("no longer exists" in problem for problem in problems)


def test_the_role_names_no_guardrail_version(current: Synthesised) -> None:
    """So a deploy that replaces a version leaves the role as it is."""
    assert not depends_on(current.resources, current.role) & set(current.versions)


@pytest.mark.parametrize("model_id", [ANSWER_MODEL_ID, REGIONAL_MODEL_ID])
def test_no_model_call_without_the_andur_answer_guardrail_is_allowed(model_id: str) -> None:
    """The proof that no answer comes without the guardrail: only a numbered version of the
    answer guardrail opens the model, through the EU profile or in the region."""
    synthesised = synthesise(model_id=model_id)
    resources = synthesised.resources
    numbers = dict.fromkeys(synthesised.versions, OLD)
    answer = guardrail_arn("answerguardrail")

    def allowed(identifier: str | None) -> bool:
        return may_invoke(
            resources[synthesised.role], synthesised.entry_point, identifier, resources, numbers
        )

    for number in ("1", OLD, NEW, "20"):
        assert allowed(f"{answer}:{number}"), number
    for refused in (
        None,  # no guardrail at all
        answer,  # the draft, which CloudFormation changes before it makes the version
        f"{answer}:DRAFT",  # the draft, should Bedrock name it with its suffix
        f"{guardrail_arn('excerptguardrail')}:1",  # masks excerpts, checks no topic
        f"{guardrail_arn('topicguardrail')}:1",  # checks topics, no grounding, no masking
        f"{guardrail_arn('answerguardrailcopy')}:1",  # an id that starts the same
        f"{guardrail_arn('abcdef123456')}:1",  # any other guardrail in the account
        f"arn:aws:bedrock:{REGION}:444455556666:guardrail/answerguardrail:1",  # other account
        f"arn:aws:bedrock:eu-central-1:{ACCOUNT}:guardrail/answerguardrail:1",  # other region
    ):
        assert not allowed(refused), refused


@pytest.mark.parametrize("model_id", [ANSWER_MODEL_ID, REGIONAL_MODEL_ID])
def test_no_policy_beside_the_role_s_own_allows_the_model(model_id: str) -> None:
    """The proof above reads the role's own policies; a policy attached from elsewhere (a
    later grant, a managed policy) would go round it, so there must be none that opens the
    model."""
    synthesised = synthesise(model_id=model_id)
    role_ref = {"Ref": synthesised.role}
    assert not synthesised.resources[synthesised.role]["Properties"].get("ManagedPolicyArns")
    for logical_id, resource in synthesised.resources.items():
        if resource["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        if role_ref not in resource["Properties"].get("Roles", []):
            continue
        statements = resource["Properties"]["PolicyDocument"]["Statement"]
        assert not any(allows_invoke(statement) for statement in statements), logical_id


def test_the_model_itself_opens_only_through_the_guarded_profile(current: Synthesised) -> None:
    """The one other statement that allows InvokeModel on the answer model needs the profile,
    and the profile needs the guardrail: there is no way round it."""
    profile_statement, model_statement = invoke_statements(current.resources[current.role])
    assert "bedrock:GuardrailIdentifier" in json.dumps(profile_statement["Condition"])
    assert model_statement["Condition"] == {
        "StringEquals": {"bedrock:InferenceProfileArn": profile_statement["Resource"]}
    }


def test_the_drain_comes_after_the_function_and_follows_every_version(
    current: Synthesised,
) -> None:
    drain = current.resources[current.drain]
    assert current.function in depends_on(current.resources, current.drain)
    assert drain["Properties"]["GuardrailVersions"] == [
        {"Ref": version} for version in current.versions
    ]
    assert int(drain["Properties"]["Seconds"]) == DEPLOY_DRAIN_SECONDS > ANSWER_TIMEOUT_SECONDS


def test_a_deploy_that_replaces_no_version_does_not_wait(current: Synthesised) -> None:
    """Only a new version changes the drain's properties; a price or a handler change
    deploys as fast as before."""

    def price_changed(patch: pytest.MonkeyPatch) -> None:
        patch.setattr(answer_function, "PRICE_TOPIC_PER_1K_UNITS", "0.16")

    orders, problems = walk(synthesise(price_changed), current)
    assert orders == [[current.function]]
    assert problems == []


def test_the_drain_and_its_framework_may_run_longer_than_the_wait(current: Synthesised) -> None:
    """A Lambda timeout during the wait would fail the deploy and roll it back."""
    token = current.resources[current.drain]["Properties"]["ServiceToken"]
    framework = current.resources[token["Fn::GetAtt"][0]]
    (drain_function,) = (
        resource
        for logical_id, resource in current.resources.items()
        if logical_id.startswith("DeployDrainFunction")
        and resource["Type"] == "AWS::Lambda::Function"
    )
    assert drain_function["Properties"]["Handler"] == "handler.on_event"
    assert DEPLOY_DRAIN_SECONDS < drain_function["Properties"]["Timeout"]
    assert drain_function["Properties"]["Timeout"] <= framework["Properties"]["Timeout"]


def test_the_drain_role_may_only_write_its_log(current: Synthesised) -> None:
    (role,) = (
        resource
        for logical_id, resource in current.resources.items()
        if logical_id.startswith("DeployDrainFunctionRole")
    )
    (policy,) = role["Properties"]["Policies"]
    assert policy["PolicyName"] == "WriteLogs"
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert sorted(statement["Action"]) == ["logs:CreateLogStream", "logs:PutLogEvents"]


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_the_drain_waits_on_create_and_update(
    request_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(drain_handler.time, "sleep", slept.append)
    event = {"RequestType": request_type, "ResourceProperties": {"Seconds": "210"}}

    assert drain_handler.on_event(event, None) == {"PhysicalResourceId": "andur-deploy-drain"}
    assert slept == [210]


def test_the_drain_returns_at_once_on_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(drain_handler.time, "sleep", slept.append)
    event = {"RequestType": "Delete", "PhysicalResourceId": "andur-deploy-drain"}

    assert drain_handler.on_event(event, None) == {"PhysicalResourceId": "andur-deploy-drain"}
    assert slept == []


def test_the_drain_touches_nothing_in_aws() -> None:
    assert "boto3" not in Path(drain_handler.__file__).read_text(encoding="utf-8")

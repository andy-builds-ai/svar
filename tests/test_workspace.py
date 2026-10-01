"""Tests for scripts/workspace.py against fakes of every AWS service it touches.

Most of them are about one thing: `remove` takes away one workspace and never touches
anything outside it. The fake account holds neighbours chosen to catch a careless prefix:
``kalv`` is removed, ``kalvenstett`` and ``kalv-x`` begin with the same letters and must
survive, and so must ``quillmoor``.
"""

import hashlib
from typing import Any

import handler  # app/handler.py, imported by its Lambda name (tests/conftest.py)
import pytest

from scripts import workspace
from tests.test_handler import (
    apply_update,
    assert_every_placeholder_is_used,
    condition_holds,
    with_names,
)

PATH = "/andur/api-keys"
TABLE = "andur-key-budgets"
BUCKET = "documents-bucket"
TARGET = "kalv"
NEIGHBOURS = ("kalvenstett", "kalv-x", "quillmoor")

OUTPUTS = {
    "ApiKeyParameterPath": PATH,
    "BudgetTableName": TABLE,
    "DocumentsBucketName": BUCKET,
    "KnowledgeBaseId": "KB123",
    "DataSourceId": "DS123",
}


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: module name and a response dict."""

    __module__ = "botocore.exceptions"

    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeCloudFormation:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs

    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        return {
            "Stacks": [
                {"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in self.outputs.items()]}
            ]
        }


class FakeSSM:
    def __init__(self) -> None:
        self.parameters: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.fail_delete: str | None = None
        #: Called after a successful delete: a question asked in the cache window.
        self.after_delete: Any = None
        #: The first put stores the key, but its answer is lost and botocore tries again.
        self.answer_lost = False

    def get_parameter(self, Name: str, WithDecryption: bool) -> dict[str, Any]:
        assert WithDecryption
        if Name not in self.parameters:
            raise FakeClientError("ParameterNotFound")
        return {"Parameter": {"Name": Name, "Value": self.parameters[Name]["Value"]}}

    def put_parameter(self, **kwargs: Any) -> None:
        if kwargs["Name"] in self.parameters and not kwargs["Overwrite"]:
            raise FakeClientError("ParameterAlreadyExists")
        self.parameters[kwargs["Name"]] = kwargs
        if self.answer_lost:
            raise FakeClientError("ParameterAlreadyExists")

    def delete_parameter(self, Name: str) -> None:
        if self.fail_delete:
            raise FakeClientError(self.fail_delete)
        if Name not in self.parameters:
            raise FakeClientError("ParameterNotFound")
        del self.parameters[Name]
        self.deleted.append(Name)
        if self.after_delete:
            self.after_delete()


class FakeDynamoDB:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.extra_in_scan: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail_update: str | None = None

    def update_item(self, **request: Any) -> dict[str, Any]:
        """The script's update and condition, executed as the answer function's tests do."""
        assert request["TableName"] == TABLE
        self.updates.append(request)
        if self.fail_update:
            raise FakeClientError(self.fail_update)
        assert_every_placeholder_is_used(request)
        names = request.get("ExpressionAttributeNames", {})
        values = request["ExpressionAttributeValues"]
        digest = request["Key"]["key_hash"]["S"]
        before = self.items.get(digest)
        if not condition_holds(
            with_names(request["ConditionExpression"], names), before or {}, values
        ):
            raise FakeClientError("ConditionalCheckFailedException")
        self.items[digest] = apply_update(
            with_names(request["UpdateExpression"], names),
            before or {"key_hash": {"S": digest}},
            values,
        )
        wants_old = request.get("ReturnValues") == "ALL_OLD" and before is not None
        return {"Attributes": dict(before)} if wants_old else {}

    def get_item(self, TableName: str, Key: dict[str, Any], ConsistentRead: bool) -> dict[str, Any]:
        assert TableName == TABLE
        item = self.items.get(Key["key_hash"]["S"])
        return {"Item": item} if item else {}

    def delete_item(self, TableName: str, Key: dict[str, Any]) -> None:
        assert TableName == TABLE
        assert set(Key) == {"key_hash"}
        self.items.pop(Key["key_hash"]["S"], None)
        self.deleted.append(Key["key_hash"]["S"])

    def scan(self, **request: Any) -> dict[str, Any]:
        """The filter the script sends, evaluated here; two pages, like a long table."""
        assert request["TableName"] == TABLE
        assert request["FilterExpression"] == "#workspace = :workspace"
        assert request["ExpressionAttributeNames"] == {"#workspace": "workspace"}
        wanted = request["ExpressionAttributeValues"][":workspace"]["S"]
        hashes = sorted(self.items)
        half = len(hashes) // 2
        page, rest = (
            (hashes[half:], None)
            if "ExclusiveStartKey" in request
            else (
                hashes[:half],
                {"key_hash": {"S": hashes[half - 1]}} if half else None,
            )
        )
        found = [
            self.items[digest]
            for digest in page
            if self.items[digest].get("workspace", {}).get("S") == wanted
        ]
        if rest is None:
            found += self.extra_in_scan
        response: dict[str, Any] = {"Items": found}
        if rest is not None:
            response["LastEvaluatedKey"] = rest
        return response


class FakeS3:
    def __init__(self) -> None:
        self.objects: set[str] = set()
        self.deleted: list[str] = []
        self.listed_prefixes: list[str] = []
        self.extra_in_listing: list[str] = []

    def get_paginator(self, operation: str) -> "FakeS3":
        assert operation == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        """Two pages, so a listing that reads only the first would show."""
        assert Bucket == BUCKET
        self.listed_prefixes.append(Prefix)
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        keys += self.extra_in_listing
        half = len(keys) // 2
        return [{"Contents": [{"Key": key} for key in part]} for part in (keys[:half], keys[half:])]

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        assert Bucket == BUCKET
        assert len(Delete["Objects"]) <= 1000
        for entry in Delete["Objects"]:
            self.objects.discard(entry["Key"])
            self.deleted.append(entry["Key"])
        return {}


class FakeAgent:
    def __init__(self, status: str = "COMPLETE") -> None:
        self.status = status
        self.started = 0

    def start_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str) -> dict[str, Any]:
        assert (knowledgeBaseId, dataSourceId) == ("KB123", "DS123")
        self.started += 1
        return {"ingestionJob": {"ingestionJobId": "job-1", "status": self.status}}


def budget_item(digest: str, name: str | None, used: str = "3") -> dict[str, Any]:
    item = {
        "key_hash": {"S": digest},
        "questions_used": {"N": used},
        "question_limit": {"N": "10"},
    }
    if name is not None:
        item["workspace"] = {"S": name}
    return item


def key_of(name: str) -> str:
    return f"secret-key-of-{name}-that-must-never-be-printed"


def objects_of(name: str, count: int = 2) -> set[str]:
    return {
        key
        for number in range(count)
        for key in (
            f"workspaces/{name}/human/doc-{number}--01-a.md",
            f"workspaces/{name}/human/doc-{number}--01-a.md.metadata.json",
        )
    }


class Account:
    """One fake AWS account with the target workspace and its neighbours, each complete."""

    def __init__(self, outputs: dict[str, str] = OUTPUTS) -> None:
        self.cloudformation = FakeCloudFormation(outputs)
        self.ssm = FakeSSM()
        self.dynamodb = FakeDynamoDB()
        self.s3 = FakeS3()
        self.agent = FakeAgent()
        self.connected: list[str] = []
        for name in (TARGET, *NEIGHBOURS):
            self.ssm.parameters[f"{PATH}/{name}"] = {"Value": key_of(name)}
            digest = workspace.key_hash(key_of(name))
            self.dynamodb.items[digest] = budget_item(digest, name)
            self.s3.objects |= objects_of(name)
        # Same last part, other path: not a key of this stack.
        self.ssm.parameters[f"{PATH}-other/{TARGET}"] = {"Value": "other"}

    def connect(self, service: str, region: str) -> Any:
        self.connected.append(service)
        return {
            "cloudformation": self.cloudformation,
            "ssm": self.ssm,
            "dynamodb": self.dynamodb,
            "s3": self.s3,
            "bedrock-agent": self.agent,
        }[service]

    def snapshot(self) -> tuple[Any, ...]:
        return (
            {name: dict(value) for name, value in self.ssm.parameters.items()},
            dict(self.dynamodb.items),
            set(self.s3.objects),
        )

    def neighbours_untouched(self) -> None:
        for name in NEIGHBOURS:
            assert self.ssm.parameters[f"{PATH}/{name}"]["Value"] == key_of(name)
            assert workspace.key_hash(key_of(name)) in self.dynamodb.items
            assert objects_of(name) <= self.s3.objects
        assert f"{PATH}-other/{TARGET}" in self.ssm.parameters


def run(account: Account, *argv: str) -> int:
    return workspace.main(list(argv), connect=account.connect)


# --- The name ------------------------------------------------------------


BAD_NAMES = [
    "",
    "Kalv",
    "kalv/",
    "kalv/../quillmoor",
    "../quillmoor",
    "kalv--x",
    "-kalv",
    "kalv-",
    "kalv ",
    "kalv*",
    "kalv\n",
    "a" * 65,
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_bad_name_is_refused_before_any_aws_call(name: str) -> None:
    """``-kalv`` never gets that far: the command line takes it for an option and stops."""
    for command in (
        ["add", name],
        ["add", name, "--plan", "firma"],
        ["plan", name, "betrieb"],
        ["remove", name, "--yes"],
    ):
        account = Account()
        try:
            code = run(account, *command)
        except SystemExit as stop:
            code = stop.code
        assert code in (1, 2)
        assert account.connected == []


@pytest.mark.parametrize("name", [*BAD_NAMES, "kalv", "kalv-x", "a" * 64, "0"])
def test_the_name_rule_is_the_answer_functions(name: str) -> None:
    """A name the function refuses would get a key nobody can use."""
    try:
        handler.requested_workspace({"workspace": name})
        accepted = True
    except handler.BadRequest:
        accepted = False
    assert (workspace.name_problem(name) is None) == accepted


def test_the_budget_key_is_the_answer_functions() -> None:
    assert workspace.BUDGET_KEY_ATTRIBUTE == handler.BUDGET_KEY_ATTRIBUTE
    assert workspace.key_hash("abc") == handler.key_hash("abc")


@pytest.mark.parametrize("path", ["/andur/api-keys/", "andur/api-keys", ""])
def test_a_key_path_the_stack_would_not_write_is_refused(path: str) -> None:
    account = Account({**OUTPUTS, "ApiKeyParameterPath": path})
    before = account.snapshot()
    assert run(account, "remove", TARGET, "--yes") == 2
    assert account.snapshot() == before


# --- add -----------------------------------------------------------------


def test_add_stores_a_strong_secure_key_and_prints_it_once_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = Account()
    assert run(account, "add", "new-firm") == 0
    stored = account.ssm.parameters[f"{PATH}/new-firm"]
    out, err = capsys.readouterr()

    assert out == stored["Value"] + "\n"
    assert stored["Value"] not in err
    assert len(stored["Value"]) == 43
    assert all(character.isalnum() or character in "-_" for character in stored["Value"])
    assert stored["Type"] == "SecureString"
    assert stored["Tier"] == "Standard"
    assert stored["Overwrite"] is False
    account.neighbours_untouched()


def test_keys_are_random() -> None:
    assert len({workspace.new_key() for _ in range(20)}) == 20


def test_the_description_names_the_header_the_function_reads() -> None:
    """It once said ``Authorization: Bearer <key>`` — the one header CloudFront replaces."""
    assert handler.KEY_HEADER in workspace.KEY_DESCRIPTION
    assert "Authorization" not in workspace.KEY_DESCRIPTION


def test_add_never_replaces_a_key(capsys: pytest.CaptureFixture[str]) -> None:
    account = Account()
    assert run(account, "add", "kalvenstett") == 2
    out, err = capsys.readouterr()

    assert out == ""
    assert "already" in err and "nothing was changed" in err
    assert key_of("kalvenstett") not in err
    account.neighbours_untouched()


def test_add_whose_answer_got_lost_finds_its_own_key_and_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """botocore repeats a put whose answer was lost; the repeat finds the first try's key."""
    account = Account()
    account.ssm.answer_lost = True
    assert run(account, "add", "new-firm") == 0
    out, err = capsys.readouterr()

    assert out == account.ssm.parameters[f"{PATH}/new-firm"]["Value"] + "\n"
    assert "already" not in err


def test_add_reads_only_the_key_path_from_the_stack() -> None:
    account = Account({"ApiKeyParameterPath": PATH})
    assert run(account, "add", "new-firm") == 0
    assert account.connected == ["cloudformation", "ssm"]
    assert account.dynamodb.updates == []


# --- plans ---------------------------------------------------------------


def test_the_plans_and_names_are_the_answer_functions() -> None:
    assert workspace.PLAN_CHOICES == (
        "demo",
        *(plan.name for plan in handler.plans.PLANS),
    )
    assert workspace.PLAN_ATTRIBUTE == handler.PLAN_ATTRIBUTE
    assert workspace.BUDGET_TTL_ATTRIBUTE == handler.BUDGET_TTL_ATTRIBUTE
    assert workspace.BUDGET_WORKSPACE_ATTRIBUTE == "workspace"
    assert "workspace = if_not_exists(workspace, :workspace)" in handler.BUDGET_UPDATE_EXPRESSION


def test_add_with_a_plan_puts_the_new_key_on_it(capsys: pytest.CaptureFixture[str]) -> None:
    account = Account()
    assert run(account, "add", "new-firm", "--plan", "betrieb") == 0
    out, err = capsys.readouterr()

    key = account.ssm.parameters[f"{PATH}/new-firm"]["Value"]
    assert out == key + "\n"
    assert key not in err
    assert "plan betrieb: 6,000 questions a calendar month, at most 600 a day" in err
    item = account.dynamodb.items[workspace.key_hash(key)]
    assert item == {
        "key_hash": {"S": workspace.key_hash(key)},
        "plan": {"S": "betrieb"},
        "workspace": {"S": "new-firm"},
    }
    account.neighbours_untouched()


def test_a_plan_nobody_defined_is_refused_before_any_aws_call() -> None:
    for command in (["add", "new-firm", "--plan", "gold"], ["plan", TARGET, "gold"]):
        account = Account()
        with pytest.raises(SystemExit) as stop:
            run(account, *command)
        assert stop.value.code == 2
        assert account.connected == []


def test_a_plan_that_does_not_get_through_still_prints_the_key_and_says_what_to_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The key is printed once, ever; losing it to a failed plan would lose it for good."""
    account = Account()
    account.dynamodb.fail_update = "ThrottlingException"
    assert run(account, "add", "new-firm", "--plan", "firma") == 2
    out, err = capsys.readouterr()

    assert out == account.ssm.parameters[f"{PATH}/new-firm"]["Value"] + "\n"
    assert "the key is stored, its plan is not" in err
    assert "python -m scripts.workspace plan new-firm firma" in err
    assert "default budget" in err


def test_plan_changes_the_plan_and_leaves_the_key_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = Account()
    digest = workspace.key_hash(key_of(TARGET))
    account.dynamodb.items[digest]["delete_after"] = {"N": "1900000000"}
    keys_before = {name: dict(value) for name, value in account.ssm.parameters.items()}

    assert run(account, "plan", TARGET, "firma") == 0
    out = capsys.readouterr().out
    item = account.dynamodb.items[digest]
    assert item["plan"] == {"S": "firma"}
    assert "delete_after" not in item
    # The demo counts stay where they were; they are not what a plan key is counted by.
    assert item["questions_used"] == {"N": "3"}
    assert account.ssm.parameters == keys_before
    assert "plan demo -> firma" in out
    assert "15,000 questions a calendar month, at most 1,500 a day" in out
    assert key_of(TARGET) not in out

    assert run(account, "plan", TARGET, "betrieb") == 0
    assert "plan firma -> betrieb" in capsys.readouterr().out
    assert account.dynamodb.items[digest]["plan"] == {"S": "betrieb"}
    account.neighbours_untouched()


def test_plan_demo_takes_the_key_off_its_plan() -> None:
    account = Account()
    digest = workspace.key_hash(key_of(TARGET))
    assert run(account, "plan", TARGET, "betrieb") == 0
    account.dynamodb.items[digest]["month_2026-10"] = {"N": "120"}

    assert run(account, "plan", TARGET, "demo") == 0
    item = account.dynamodb.items[digest]
    assert "plan" not in item
    assert item["month_2026-10"] == {"N": "120"}
    assert item["workspace"] == {"S": TARGET}


def test_plan_for_a_key_that_has_not_asked_yet_writes_an_item_remove_will_find() -> None:
    account = Account()
    digest = workspace.key_hash(key_of(TARGET))
    del account.dynamodb.items[digest]

    assert run(account, "plan", TARGET, "betrieb") == 0
    assert account.dynamodb.items[digest] == {
        "key_hash": {"S": digest},
        "plan": {"S": "betrieb"},
        "workspace": {"S": TARGET},
    }


def test_plan_without_a_key_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    account = Account()
    del account.ssm.parameters[f"{PATH}/{TARGET}"]
    before = account.snapshot()

    assert run(account, "plan", TARGET, "firma") == 2
    assert account.snapshot() == before
    assert f"add {TARGET} --plan firma" in capsys.readouterr().err


def test_plan_leaves_an_item_of_another_workspace_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = Account()
    digest = workspace.key_hash(key_of(TARGET))
    account.dynamodb.items[digest]["workspace"] = {"S": "kalvenstett"}
    before = account.snapshot()

    assert run(account, "plan", TARGET, "firma") == 2
    assert account.snapshot() == before
    assert "another workspace" in capsys.readouterr().err


def test_plan_reads_the_key_path_and_the_table_and_nothing_else() -> None:
    account = Account({"ApiKeyParameterPath": PATH, "BudgetTableName": TABLE})
    assert run(account, "plan", TARGET, "firma") == 0
    assert account.connected == ["cloudformation", "ssm", "dynamodb"]


def test_remove_shows_and_deletes_the_plan_items_of_every_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A plan item has no TTL, so nothing but `remove` ever takes it away."""
    account = Account()
    digest = workspace.key_hash(key_of(TARGET))
    assert run(account, "plan", TARGET, "betrieb") == 0
    account.dynamodb.items[digest] |= {
        "month_2026-09": {"N": "4000"},
        "month_2026-10": {"N": "120"},
        "day_2026-09-30": {"N": "40"},
        "day_2026-10-01": {"N": "7"},
    }
    account.dynamodb.items["old-hash"] = {
        "key_hash": {"S": "old-hash"},
        "plan": {"S": "firma"},
        "workspace": {"S": TARGET},
    }
    capsys.readouterr()

    assert run(account, "remove", TARGET) == 0
    out = capsys.readouterr().out
    assert (
        "2 item(s):\n"
        "    plan betrieb, 120 questions in 2026-10, 7 on 2026-10-01 (current key)\n"
        "    plan firma\n"
    ) in out

    assert run(account, "remove", TARGET, "--yes") == 0
    assert sorted(account.dynamodb.deleted) == sorted(["old-hash", digest])
    assert not any(
        item.get("workspace", {}).get("S") == TARGET for item in account.dynamodb.items.values()
    )
    account.neighbours_untouched()


# --- remove without --yes ------------------------------------------------


def test_remove_without_yes_lists_everything_and_deletes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = Account()
    before = account.snapshot()
    assert run(account, "remove", TARGET) == 0
    out = capsys.readouterr().out

    assert account.snapshot() == before
    assert account.ssm.deleted == account.dynamodb.deleted == account.s3.deleted == []
    assert account.agent.started == 0
    assert f"key        {PATH}/{TARGET}" in out
    assert "1 item(s):\n    3 of 10 questions used (current key)" in out
    assert "4 object(s) under workspaces/kalv/:" in out
    for key in objects_of(TARGET):
        assert f"    {key}" in out
    for name in NEIGHBOURS:
        assert f"workspaces/{name}/" not in out
    assert "Nothing was deleted" in out
    assert key_of(TARGET) not in out


# --- remove --yes --------------------------------------------------------


def test_remove_takes_exactly_the_one_workspace(capsys: pytest.CaptureFixture[str]) -> None:
    account = Account()
    assert run(account, "remove", TARGET, "--yes") == 0
    out = capsys.readouterr().out

    assert account.ssm.deleted == [f"{PATH}/{TARGET}"]
    assert account.dynamodb.deleted == [workspace.key_hash(key_of(TARGET))]
    assert sorted(account.s3.deleted) == sorted(objects_of(TARGET))
    assert account.s3.listed_prefixes == ["workspaces/kalv/"]
    assert account.agent.started == 1
    account.neighbours_untouched()
    assert not any(key.startswith("workspaces/kalv/") for key in account.s3.objects)
    assert key_of(TARGET) not in out
    assert "Ingestion job job-1: COMPLETE" in out


def test_a_listing_that_strays_outside_the_workspace_stops_everything() -> None:
    """S3 lists by prefix, but the list about to be deleted is checked again anyway."""
    account = Account()
    account.s3.extra_in_listing = ["workspaces/kalvenstett/human/doc-0--01-a.md"]
    before = account.snapshot()

    assert run(account, "remove", TARGET, "--yes") == 2
    assert account.snapshot() == before
    assert account.agent.started == 0


def test_the_budget_item_goes_before_the_key_so_a_stopped_run_can_be_repeated() -> None:
    account = Account()
    account.ssm.fail_delete = "ThrottlingException"
    assert run(account, "remove", TARGET, "--yes") == 2
    assert account.dynamodb.deleted == [workspace.key_hash(key_of(TARGET))]
    assert f"{PATH}/{TARGET}" in account.ssm.parameters
    assert objects_of(TARGET) <= account.s3.objects

    account.ssm.fail_delete = None
    assert run(account, "remove", TARGET, "--yes") == 0
    assert f"{PATH}/{TARGET}" not in account.ssm.parameters
    assert not objects_of(TARGET) & account.s3.objects
    account.neighbours_untouched()


def test_a_key_that_never_asked_leaves_the_budget_table_alone() -> None:
    account = Account()
    del account.dynamodb.items[workspace.key_hash(key_of(TARGET))]
    assert run(account, "remove", TARGET, "--yes") == 0
    assert account.dynamodb.deleted == []
    account.neighbours_untouched()


def test_without_a_key_the_items_are_found_by_their_workspace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(c) The key is gone already: no hash to look an item up by, but the item names its
    workspace."""
    account = Account()
    del account.ssm.parameters[f"{PATH}/{TARGET}"]
    assert run(account, "remove", TARGET, "--yes") == 0
    out = capsys.readouterr().out

    assert account.dynamodb.deleted == [workspace.key_hash(key_of(TARGET))]
    assert account.ssm.deleted == []
    assert sorted(account.s3.deleted) == sorted(objects_of(TARGET))
    assert "1 item(s):\n    3 of 10 questions used\n" in out
    assert "Not quite finished" not in out
    account.neighbours_untouched()


def test_the_items_of_earlier_keys_are_shown_and_deleted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(b) A replaced key leaves its item behind, under a hash no current key produces."""
    account = Account()
    account.dynamodb.items["old-hash"] = budget_item("old-hash", TARGET, used="10")
    account.dynamodb.items["preset-of-neighbour"] = budget_item("preset-of-neighbour", None)

    assert run(account, "remove", TARGET) == 0
    out = capsys.readouterr().out
    assert "2 item(s):\n    3 of 10 questions used (current key)\n    10 of 10" in out
    assert account.dynamodb.deleted == []

    assert run(account, "remove", TARGET, "--yes") == 0
    assert sorted(account.dynamodb.deleted) == sorted(
        ["old-hash", workspace.key_hash(key_of(TARGET))]
    )
    assert "preset-of-neighbour" in account.dynamodb.items
    account.neighbours_untouched()


def test_an_item_written_after_the_key_went_is_deleted_and_the_window_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(a) The function keeps keys for 5 minutes; a question in that time writes a new item."""
    account = Account()
    digest = workspace.key_hash(key_of(TARGET))

    def a_question_arrives() -> None:
        account.dynamodb.items[digest] = budget_item(digest, TARGET, used="1")

    account.ssm.after_delete = a_question_arrives
    assert run(account, "remove", TARGET, "--yes") == 0
    out = capsys.readouterr().out

    assert account.dynamodb.deleted == [digest, digest]
    assert not any(
        item.get("workspace", {}).get("S") == TARGET for item in account.dynamodb.items.values()
    )
    assert "1 budget item(s) written meanwhile, deleted" in out
    assert "Not quite finished" in out and "5 more minutes" in out
    assert f"remove {TARGET} --yes" in out
    account.neighbours_untouched()


def test_a_scan_that_returns_another_workspace_stops_everything() -> None:
    account = Account()
    account.dynamodb.extra_in_scan = [budget_item("stray", "kalvenstett")]
    before = account.snapshot()

    assert run(account, "remove", TARGET, "--yes") == 2
    assert account.snapshot() == before
    assert account.agent.started == 0


def test_a_name_with_nothing_behind_it_deletes_nothing() -> None:
    """A typo that names no workspace finds nothing; the ingestion job changes nothing."""
    account = Account()
    before = account.snapshot()
    assert run(account, "remove", "kal", "--yes") == 0
    assert account.snapshot() == before
    assert account.s3.listed_prefixes == ["workspaces/kal/"]


def test_many_documents_are_deleted_in_batches_and_only_those() -> None:
    account = Account()
    account.s3.objects |= objects_of(TARGET, count=800)
    assert run(account, "remove", TARGET, "--yes") == 0
    assert len(account.s3.deleted) == 1600
    assert all(key.startswith("workspaces/kalv/") for key in account.s3.deleted)
    account.neighbours_untouched()


def test_a_failed_ingestion_job_is_exit_code_3() -> None:
    account = Account()
    account.agent.status = "FAILED"
    assert run(account, "remove", TARGET, "--yes") == 3


def test_the_hash_is_the_sha256_of_the_key() -> None:
    assert workspace.key_hash(" k \n") == hashlib.sha256(b"k").hexdigest()

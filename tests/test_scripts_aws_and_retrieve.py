"""Tests for the shared AWS helpers and the retrieve script, without AWS.

Stack outputs and error messages are checked against fake clients and
exceptions shaped like botocore's. One test uses botocore's real ClientError
and is skipped when boto3 is not installed.
"""

from typing import Any

import pytest

from scripts import aws, retrieve
from scripts.aws import AwsProblem, explain, explained, read_stack_outputs, resolve
from scripts.retrieve import build_filter, format_hit


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: module name and a response dict."""

    __module__ = "botocore.exceptions"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeCloudFormation:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        self.calls += 1
        if self.error:
            raise self.error
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": "KnowledgeBaseId", "OutputValue": "KB123"},
                        {"OutputKey": "DataSourceId", "OutputValue": "DS456"},
                        {"OutputKey": "DocumentsBucketName", "OutputValue": "andur-documents-x"},
                    ]
                }
            ]
        }


# --- Stack outputs -------------------------------------------------------


def test_stack_outputs_become_a_dictionary() -> None:
    outputs = read_stack_outputs(FakeCloudFormation(), "AndurStack")
    assert outputs["KnowledgeBaseId"] == "KB123"
    assert outputs["DocumentsBucketName"] == "andur-documents-x"


def test_missing_stack_says_to_deploy() -> None:
    error = FakeClientError("ValidationError", "Stack with id AndurStack does not exist")
    with pytest.raises(AwsProblem, match="not found in this region"):
        read_stack_outputs(FakeCloudFormation(error), "AndurStack")


def test_arguments_override_stack_outputs_and_skip_the_lookup() -> None:
    cloudformation = FakeCloudFormation()

    def load() -> dict[str, str]:
        return read_stack_outputs(cloudformation, "AndurStack")

    assert resolve({"KnowledgeBaseId": "MINE"}, load) == {"KnowledgeBaseId": "MINE"}
    assert cloudformation.calls == 0
    assert resolve({"KnowledgeBaseId": None, "DataSourceId": "DS9"}, load) == {
        "KnowledgeBaseId": "KB123",
        "DataSourceId": "DS9",
    }
    assert cloudformation.calls == 1


def test_resolve_names_a_missing_output() -> None:
    with pytest.raises(AwsProblem, match="no output VectorIndexArn"):
        resolve({"VectorIndexArn": None}, lambda: {})


# --- Error messages ------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeClientError("ExpiredTokenException", "token expired"), "aws login"),
        (type("NoCredentialsError", (Exception,), {"__module__": "botocore.exceptions"})(), "aws login"),
        (FakeClientError("AccessDeniedException", "no"), "access denied"),
        (FakeClientError("ThrottlingException", "slow down"), "Doing it failed: An error occurred"),
    ],
    ids=["expired-token", "no-credentials", "access-denied", "other"],
)
def test_explain_gives_the_cause(error: Exception, expected: str) -> None:
    assert expected in explain("Doing it", error)


def test_expired_aws_login_session_asks_for_login() -> None:
    """What botocore raises when an `aws login` session can no longer refresh."""
    exceptions = pytest.importorskip("botocore.exceptions")
    error = exceptions.ClientError(
        {"Error": {"Code": "ValidationException", "Message": "The provided authorization grant is invalid"}},
        "CreateOAuth2Token",
    )
    assert "aws login" in explain("Reading the outputs", error)


def test_explained_translates_botocore_errors_only() -> None:
    with pytest.raises(AwsProblem, match="aws login"):
        with explained("Uploading"):
            raise FakeClientError("ExpiredToken", "expired")
    with pytest.raises(KeyError):
        with explained("Uploading"):
            raise KeyError("a bug stays a bug")


def test_explain_with_a_real_botocore_client_error() -> None:
    exceptions = pytest.importorskip("botocore.exceptions")
    error = exceptions.ClientError(
        {"Error": {"Code": "ExpiredTokenException", "Message": "expired"}}, "DescribeStacks"
    )
    assert "aws login" in explain("Reading the outputs", error)


def test_credential_failure_while_creating_a_client_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """boto3 loads credentials when the client is created, before any call."""
    boto3 = pytest.importorskip("boto3")
    exceptions = pytest.importorskip("botocore.exceptions")

    def expired_login(*_: Any, **__: Any) -> None:
        raise exceptions.LoginRefreshRequired()

    monkeypatch.setattr(boto3, "client", expired_login)
    with pytest.raises(AwsProblem, match="aws login"):
        aws.client("s3", "us-east-1")


def test_missing_boto3_is_a_readable_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def without_boto3(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_boto3)
    with pytest.raises(AwsProblem, match="pip install"):
        aws.client("s3", "us-east-1")


# --- Retrieve ------------------------------------------------------------


def test_filter_for_a_workspace_only() -> None:
    assert build_filter("quillmoor") == {"equals": {"key": "workspace", "value": "quillmoor"}}


def test_filter_for_a_workspace_and_a_layer() -> None:
    assert build_filter("quillmoor", "agent") == {
        "andAll": [
            {"equals": {"key": "workspace", "value": "quillmoor"}},
            {"equals": {"key": "layer", "value": "agent"}},
        ]
    }


def test_hit_shows_workspace_layer_file_and_a_short_excerpt() -> None:
    hit = {
        "score": 0.61234,
        "metadata": {"workspace": "quillmoor", "layer": "human"},
        "location": {"s3Location": {"uri": "s3://bucket/workspaces/quillmoor/human/faq.md"}},
        "content": {"text": "word " * 100},
    }
    header, excerpt = format_hit(1, hit)
    assert header == "1. score 0.612  workspace=quillmoor  layer=human  faq.md"
    assert excerpt.endswith(" ...")
    assert len(excerpt) <= 3 + retrieve.EXCERPT_CHARACTERS + 4


def test_retrieve_requires_a_workspace() -> None:
    with pytest.raises(SystemExit):
        retrieve.parse_args(["a question"])


def test_retrieve_sends_the_filter(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sent: dict[str, Any] = {}

    class FakeRuntime:
        def retrieve(self, **kwargs: Any) -> dict[str, Any]:
            sent.update(kwargs)
            return {"retrievalResults": []}

    monkeypatch.setattr(retrieve, "client", lambda service, region: FakeRuntime())
    code = retrieve.main(["How?", "--workspace", "pellwick", "--layer", "human", "--knowledge-base-id", "KB1"])

    assert code == 0
    assert sent["knowledgeBaseId"] == "KB1"
    assert sent["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"] == build_filter("pellwick", "human")
    assert "0 hit(s)" in capsys.readouterr().out

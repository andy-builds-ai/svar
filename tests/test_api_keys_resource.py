"""Tests for the custom resource that creates workspace keys, against a fake Parameter Store."""

from typing import Any

import pytest

from infra.api_keys import handler

PATH = "/andur/api-keys"
PROPERTIES = {"ParameterName": f"{PATH}/quillmoor", "ParameterPath": PATH}


class FakeSSM:
    """Parameter Store with the two exceptions the handler catches."""

    class exceptions:
        class ParameterAlreadyExists(Exception):
            pass

        class ParameterNotFound(Exception):
            pass

    def __init__(self) -> None:
        self.parameters: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    def put_parameter(self, **kwargs: Any) -> None:
        if kwargs["Name"] in self.parameters and not kwargs["Overwrite"]:
            raise self.exceptions.ParameterAlreadyExists()
        self.parameters[kwargs["Name"]] = kwargs

    def delete_parameter(self, Name: str) -> None:
        if Name not in self.parameters:
            raise self.exceptions.ParameterNotFound()
        del self.parameters[Name]
        self.deleted.append(Name)


def lifecycle(request_type: str, physical_id: str | None = None, properties: dict[str, str] = PROPERTIES) -> dict[str, Any]:
    event: dict[str, Any] = {"RequestType": request_type, "ResourceProperties": properties}
    if physical_id is not None:
        event["PhysicalResourceId"] = physical_id
    return event


def test_new_keys_are_long_random_and_header_safe() -> None:
    keys = {handler.new_key() for _ in range(20)}
    assert len(keys) == 20
    for key in keys:
        assert len(key) == 43
        assert all(character.isalnum() or character in "-_" for character in key)


def test_create_stores_a_secure_string_and_names_the_resource_after_it() -> None:
    ssm = FakeSSM()
    result = handler.on_event(lifecycle("Create"), None, ssm)

    assert result == {"PhysicalResourceId": f"{PATH}/quillmoor"}
    stored = ssm.parameters[f"{PATH}/quillmoor"]
    assert stored["Type"] == "SecureString"
    assert stored["Tier"] == "Standard"
    assert stored["Overwrite"] is False
    assert len(stored["Value"]) == 43


def test_create_and_update_keep_an_existing_key() -> None:
    ssm = FakeSSM()
    handler.on_event(lifecycle("Create"), None, ssm)
    first = ssm.parameters[f"{PATH}/quillmoor"]["Value"]

    handler.on_event(lifecycle("Update", f"{PATH}/quillmoor"), None, ssm)
    handler.on_event(lifecycle("Create"), None, ssm)
    assert ssm.parameters[f"{PATH}/quillmoor"]["Value"] == first


def test_update_creates_a_key_that_went_missing() -> None:
    ssm = FakeSSM()
    handler.on_event(lifecycle("Update", f"{PATH}/quillmoor"), None, ssm)
    assert f"{PATH}/quillmoor" in ssm.parameters


def test_delete_removes_the_key_and_tolerates_a_missing_one() -> None:
    ssm = FakeSSM()
    handler.on_event(lifecycle("Create"), None, ssm)

    assert handler.on_event(lifecycle("Delete", f"{PATH}/quillmoor"), None, ssm) == {
        "PhysicalResourceId": f"{PATH}/quillmoor"
    }
    assert ssm.deleted == [f"{PATH}/quillmoor"]
    handler.on_event(lifecycle("Delete", f"{PATH}/quillmoor"), None, ssm)


def test_delete_leaves_a_key_alone_that_this_resource_never_created() -> None:
    """A Delete after a failed Create carries an id that is not the parameter name."""
    ssm = FakeSSM()
    handler.on_event(lifecycle("Create"), None, ssm)

    result = handler.on_event(lifecycle("Delete", "request-id-of-a-failed-create"), None, ssm)
    assert result == {"PhysicalResourceId": "request-id-of-a-failed-create"}
    assert ssm.deleted == []


@pytest.mark.parametrize(
    "properties",
    [
        {"ParameterName": "/other/quillmoor", "ParameterPath": PATH},
        {"ParameterName": 5, "ParameterPath": PATH},
        {"ParameterPath": PATH},
        None,
    ],
    ids=["outside-the-path", "not-a-string", "no-name", "no-properties"],
)
def test_delete_with_invalid_properties_succeeds_and_deletes_nothing(properties: dict[str, Any] | None) -> None:
    """Create would have failed on the same properties, so there is no key — and a failing Delete sticks a stack."""
    ssm = FakeSSM()
    handler.on_event(lifecycle("Create"), None, ssm)
    event: dict[str, Any] = {"RequestType": "Delete", "PhysicalResourceId": f"{PATH}/quillmoor"}
    if properties is not None:
        event["ResourceProperties"] = properties

    assert handler.on_event(event, None, ssm) == {"PhysicalResourceId": f"{PATH}/quillmoor"}
    assert ssm.deleted == []


@pytest.mark.parametrize(
    "name",
    ["/other/quillmoor", f"{PATH}/quill/moor", f"{PATH}X/quillmoor", PATH],
)
def test_a_parameter_outside_the_key_path_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="not directly under"):
        handler.on_event(lifecycle("Create", properties={"ParameterName": name, "ParameterPath": PATH}), None, FakeSSM())

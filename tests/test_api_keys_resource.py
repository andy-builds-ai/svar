"""Tests for the function that lets go of the retired key resources without deleting a key."""

from pathlib import Path
from typing import Any

import pytest

from infra.api_keys import handler

PATH = "/andur/api-keys"
PROPERTIES = {"ParameterName": f"{PATH}/quillmoor", "ParameterPath": PATH}


def lifecycle(request_type: str, physical_id: str | None = None) -> dict[str, Any]:
    """An event as the provider framework passes it on, properties as deployed until now."""
    event: dict[str, Any] = {"RequestType": request_type, "ResourceProperties": PROPERTIES}
    if physical_id is not None:
        event["PhysicalResourceId"] = physical_id
    return event


def test_a_delete_succeeds_and_keeps_its_id(capsys: pytest.CaptureFixture[str]) -> None:
    """The Delete of every removed key resource comes here; CloudFormation must see success."""
    result = handler.on_event(lifecycle("Delete", f"{PATH}/quillmoor"), None)

    assert result == {"PhysicalResourceId": f"{PATH}/quillmoor"}
    assert f"kept {PATH}/quillmoor" in capsys.readouterr().out


def test_a_delete_without_an_id_or_properties_still_succeeds() -> None:
    assert handler.on_event({"RequestType": "Delete"}, None) == {"PhysicalResourceId": ""}


def test_the_function_cannot_reach_parameter_store_at_all() -> None:
    """Not "chooses not to delete": it has no client to delete with. Its role has no SSM
    permission either (tests/test_stack.py)."""
    source = Path(handler.__file__).read_text(encoding="utf-8")
    assert "boto3" not in source
    assert "delete_parameter" not in source


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_create_and_update_are_refused(request_type: str) -> None:
    """The stack has no key resource left; a template that still sends one is a mistake."""
    with pytest.raises(ValueError, match="scripts.workspace add"):
        handler.on_event(lifecycle(request_type, f"{PATH}/quillmoor"), None)

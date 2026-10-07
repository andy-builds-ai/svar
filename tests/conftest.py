"""Shared test setup.

Lambda imports the answer function's modules by their plain names, ``handler``
and ``aws``, because it packages the app/ folder as its import root. The tests
put the same folder on the import path, so they import exactly what Lambda
imports, under the same names.
"""

import sys
from pathlib import Path

import pytest

from infra import answer_models

APP_FOLDER = Path(__file__).resolve().parents[1] / "app"

if str(APP_FOLDER) not in sys.path:
    sys.path.insert(0, str(APP_FOLDER))


@pytest.fixture(autouse=True)
def no_real_aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test reaches a real AWS account: a test that misses a fake client fails on these
    credentials instead of spending money with the developer's login."""
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def frankfurt_topic_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """eu-central-1 with gpt-oss-20b checking the topics, so that these tests keep their model
    and prices whichever topic model `REGIONS` gives Frankfurt."""
    frankfurt = answer_models.REGIONS["eu-central-1"]
    monkeypatch.setitem(
        answer_models.REGIONS,
        "eu-central-1",
        frankfurt._replace(topic_model_id="openai.gpt-oss-20b-1:0", answer_guardrail_profile=False),
    )

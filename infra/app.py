#!/usr/bin/env python3
"""CDK entry point. `cdk synth` and `cdk deploy` run this file.

Everything the stack needs about *where* it goes lives here; everything
about *what* it contains lives in the stack module.
"""

import os
import sys
from pathlib import Path

import aws_cdk as cdk

# `cdk` runs this file directly, so only `infra/` lands on the import path.
# Adding the repository root lets the app and the tests import the stack by
# the same name, `infra.stacks.andur_stack`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.answer_models import DEFAULT_REGION, region_models  # noqa: E402
from infra.stacks.andur_stack import AndurStack  # noqa: E402
from infra.stacks.answer_function import ANSWER_MAX_CONCURRENCY  # noqa: E402

app = cdk.App()

#: `DEFAULT_REGION` (Ireland) unless `-c region=<region>` names another of `REGIONS`. Taken
#: from neither the caller's profile nor the shell, so a misconfigured shell cannot scatter
#: half a deployment across two regions.
REGION = app.node.try_get_context("region") or DEFAULT_REGION
MODELS = region_models(REGION)

#: `ANSWER_MAX_CONCURRENCY` unless `-c answer_max_concurrency=none` leaves the reservation out,
#: for an account whose concurrency limit has no room for it.
CONCURRENCY = app.node.try_get_context("answer_max_concurrency")
if CONCURRENCY not in (None, "none"):
    raise ValueError(f"answer_max_concurrency may only be none, not {CONCURRENCY!r}")

AndurStack(
    app,
    "AndurStack",
    description="Andur: documents, vector index and Bedrock knowledge base.",
    answer_model_id=MODELS.answer_model_id,
    topic_model_id=MODELS.topic_model_id,
    answer_guardrail_profile=MODELS.answer_guardrail_profile,
    behind_cloudfront=MODELS.behind_cloudfront,
    answer_max_concurrency=None if CONCURRENCY == "none" else ANSWER_MAX_CONCURRENCY,
    # The account comes from outside: the CDK CLI sets CDK_DEFAULT_ACCOUNT from
    # the active AWS profile (`--profile`). Run without the CLI, the account
    # stays None: the stack still synthesises, bound to the region, but cannot
    # be deployed. The tests do not come through this file — they build their
    # own stack without an environment.
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
)

app.synth()

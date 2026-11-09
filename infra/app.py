#!/usr/bin/env python3
"""CDK entry point. `cdk synth` and `cdk deploy` run this file.

Everything the stacks need about *where* they go lives here; everything
about *what* they contain lives in the stack modules (taug/infra/stack.py,
svar/infra/stack.py).
"""

import os
import sys
from pathlib import Path

import aws_cdk as cdk

# `cdk` runs this file directly, so only `infra/` lands on the import path.
# Adding the repository root lets the app and the tests import the stack by
# the same name, `svar.infra.stack`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from svar.infra.answer_function import ANSWER_MAX_CONCURRENCY  # noqa: E402
from svar.infra.stack import SVAR_DESCRIPTION, SvarStack  # noqa: E402
from svar.scripts.ask import SVAR_STACK_NAME  # noqa: E402
from taug.infra.answer_models import DEFAULT_REGION, region_models  # noqa: E402
from taug.infra.stack import TAUG_DESCRIPTION, TaugStack  # noqa: E402
from taug.scripts.aws import TAUG_STACK_NAME  # noqa: E402

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

#: Both stacks go to the same account and region.
ENVIRONMENT = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION)

# The stack names the scripts read their outputs by.
taug = TaugStack(app, TAUG_STACK_NAME, description=TAUG_DESCRIPTION, env=ENVIRONMENT)

# Svar keeps the stack name it was deployed under, so its function, address and page stay
# where they are; Taug's parts move out of it into TaugStack (docs/decisions.md).
svar = SvarStack(
    app,
    SVAR_STACK_NAME,
    description=SVAR_DESCRIPTION,
    knowledge_base=taug.knowledge_base,
    answer_model_id=MODELS.answer_model_id,
    topic_model_id=MODELS.topic_model_id,
    answer_guardrail_profile=MODELS.answer_guardrail_profile,
    behind_cloudfront=MODELS.behind_cloudfront,
    answer_max_concurrency=None if CONCURRENCY == "none" else ANSWER_MAX_CONCURRENCY,
    # The account comes from outside: the CDK CLI sets CDK_DEFAULT_ACCOUNT from
    # the active AWS profile (`--profile`). Run without the CLI, the account
    # stays None: the stack still synthesises, bound to the region, but cannot
    # be deployed. The tests do not come through this file — they build their
    # own stacks without an environment.
    env=ENVIRONMENT,
)
svar.add_stack_dependency(taug, "Svar reads the knowledge base from Taug.")

app.synth()

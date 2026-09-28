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

from infra.stacks.andur_stack import AndurStack  # noqa: E402

#: Ireland: documents, vectors and answers stay in the EU, and S3 Vectors and
#: the Bedrock models Andur uses are available here. Pinned rather than taken
#: from the caller's profile, so a misconfigured shell cannot scatter half a
#: deployment across two regions.
REGION = "eu-west-1"

app = cdk.App()

AndurStack(
    app,
    "AndurStack",
    description="Andur: documents, vector index and Bedrock knowledge base.",
    # The region is pinned above; only the account comes from outside. The
    # CDK CLI sets CDK_DEFAULT_ACCOUNT from the active AWS profile. Run
    # without the CLI, the account stays None: the stack still synthesises,
    # bound to the region, but cannot be deployed. The tests do not come
    # through this file — they build their own stack without an environment.
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
)

app.synth()

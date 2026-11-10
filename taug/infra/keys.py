"""Taug's key resources: where the workspace keys live, and the key retirer, built anew as
the first content of Taug's stack.

The keys belong to the workspaces (Taug); Svar's answer function reads them with Taug's
key check (taug/access/keys.py), packed into it."""

from typing import TYPE_CHECKING

from aws_cdk import (
    Aws,
    Duration,
    aws_iam as iam,
    aws_lambda as lambda_,
    custom_resources as cr,
)

from taug.infra.shared import (
    ASSET_EXCLUDES,
    LAMBDA_RUNTIME,
    REPOSITORY_ROOT,
    create_log_group,
    write_logs_policy,
)

if TYPE_CHECKING:
    from taug.infra.stack import TaugStack


#: Where the workspace keys live: ``<path>/<workspace>``, one SecureString each.
#: The stack creates none; `python -m taug.scripts.workspace` adds and removes them.
API_KEY_PARAMETER_PATH = "/andur/api-keys"


def create_key_retirer(stack: "TaugStack") -> cr.Provider:
    """The key retirer: the function that answered, in Andur's one stack, the Delete of the
    key resources that stack used to have, and deleted nothing
    (taug/infra/api_keys/handler.py).

    In Taug's stack it answers no Delete. A custom resource sends its Delete to the function
    its ``ServiceToken`` names, the provider in its own stack, and no resource names this
    one. No stack this code deploys holds a ``Custom::AndurApiKey``: the templates have
    none, and a stack in eu-west-1 or eu-central-1 still holding one loses it, to the
    retirer in that same stack, on the last deploy before the split, whose ``cdk diff``
    shows it. The old stack in us-east-1, deliberately left at a version with key
    resources, keeps its own retirer, and this code cannot be deployed there: the region
    has no models (``region_models``).

    It is built here, under its old construct ids, because ``cdk refactor`` creates no
    stack: Taug's stack is deployed with this in it before the knowledge base moves in
    (docs/decisions.md). Its role may only write its log, so not even a Delete that did
    reach it could take a key.
    """
    log_group = create_log_group(stack, "ApiKeyFunctionLogs")
    role = iam.Role(
        stack,
        "ApiKeyFunctionRole",
        description="Lets the Andur key retirer write its log; nothing else.",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        inline_policies={"WriteLogs": write_logs_policy(log_group)},
    )
    function = lambda_.Function(
        stack,
        "ApiKeyFunction",
        description="Answers the Delete of retired Andur key resources; deletes nothing.",
        runtime=LAMBDA_RUNTIME,
        architecture=lambda_.Architecture.ARM_64,
        code=lambda_.Code.from_asset(
            str(REPOSITORY_ROOT / "taug" / "infra" / "api_keys"), exclude=ASSET_EXCLUDES
        ),
        handler="handler.on_event",
        timeout=Duration.minutes(1),
        role=role,
        log_group=log_group,
    )
    return cr.Provider(
        stack,
        "ApiKeyProvider",
        on_event_handler=function,
        log_group=create_log_group(stack, "ApiKeyProviderLogs"),
    )


def api_key_path_arn() -> str:
    """The ARN of the key path; a key is this plus ``/<workspace>``."""
    return (
        f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{API_KEY_PARAMETER_PATH}"
    )

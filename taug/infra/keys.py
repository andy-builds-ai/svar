"""Taug's key resources in the one stack: where the workspace keys live, and the function
that answers the Delete of the key resources the stack used to have.

The keys belong to the workspaces (Taug); until the stack is split, the answer function of
Svar reads them (svar/infra/answer_function.py)."""

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
    from svar.infra.stack import AndurStack


#: Where the workspace keys live: ``<path>/<workspace>``, one SecureString each.
#: The stack creates none; `python -m taug.scripts.workspace` adds and removes them.
API_KEY_PARAMETER_PATH = "/andur/api-keys"


def create_key_retirer(stack: "AndurStack") -> cr.Provider:
    """The function that answers the Delete of the key resources this stack used to have.

    It deletes nothing (taug/infra/api_keys/handler.py), and it has no permission on Parameter
    Store either: the removed resources go in the deploy's cleanup phase, after this role
    and this function are updated, so the Delete finds neither code nor a right to
    delete a key. The construct ids are the old ones, so the deploy updates these
    resources in place instead of replacing them.

    Remove all of it only once no deployed stack holds a ``Custom::AndurApiKey`` any
    more, in any account and region: a stack that skipped this deploy would send the
    Delete to the old function, which may delete keys. The old stack in us-east-1 is
    one, deliberately left at a version with key resources (docs/decisions.md).
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

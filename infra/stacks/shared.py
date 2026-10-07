"""What the parts of the stack share: the Lambda runtime, how code is packaged, log
groups."""

from pathlib import Path
from typing import TYPE_CHECKING

from aws_cdk import (
    RemovalPolicy,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)

if TYPE_CHECKING:
    from infra.stacks.andur_stack import AndurStack


#: The newest Python runtime this CDK version offers.
LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_14

LOG_RETENTION = logs.RetentionDays.ONE_MONTH

#: The repository root; function code is packaged from folders below it.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

ASSET_EXCLUDES = ["**/__pycache__", "*.pyc", "README.md"]

WEB_FOLDER = REPOSITORY_ROOT / "web"


def create_log_group(
    stack: "AndurStack", construct_id: str, name: str | None = None
) -> logs.LogGroup:
    """A log group with a retention, deleted with the stack."""
    return logs.LogGroup(
        stack,
        construct_id,
        log_group_name=name,
        retention=LOG_RETENTION,
        removal_policy=RemovalPolicy.DESTROY,
    )


def write_logs_policy(log_group: logs.LogGroup) -> iam.PolicyDocument:
    """Write to one log group — instead of the managed policy's logs on every group."""
    return iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                sid="WriteOwnLogGroup",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[log_group.log_group_arn],
            )
        ]
    )

"""The answer function: its role, its settings, its address, the key budgets it counts
against, and what a deploy needs around it."""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsii
from aws_cdk import (
    AssetHashType,
    Aws,
    BundlingOptions,
    CustomResource,
    DockerImage,
    Duration,
    ILocalBundling,
    RemovalPolicy,
    aws_bedrock as bedrock,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    custom_resources as cr,
)

from svar.app.page import PAGE_FOLDER_VARIABLE
from svar.infra.answer_prompts import prompt_of
from svar.infra.guardrails import GUARDRAIL_PROFILE_REGIONS, guardrail_profile_arn
from svar.infra.web import WEB_FOLDER
from taug.infra.answer_models import (
    DEFAULT_REGION,
    answer_model,
    region_models,
)
from taug.infra.keys import API_KEY_PARAMETER_PATH, api_key_path_arn
from taug.infra.shared import (
    ASSET_EXCLUDES,
    LAMBDA_RUNTIME,
    REPOSITORY_ROOT,
    create_log_group,
    write_logs_policy,
)

if TYPE_CHECKING:
    from svar.infra.stack import AndurStack


#: A model id runs in the stack's region; an ``eu.`` inference profile id runs
#: in the EU regions that profile routes to, and nowhere else. Its token prices
#: come from taug/infra/answer_models.py, like the model of each region.
ANSWER_MODEL_ID = region_models(DEFAULT_REGION).answer_model_id

EU_PROFILE_PREFIX = "eu."

#: How many passages retrieve hands to the model per question.
NUMBER_OF_RESULTS = 10

#: What the answer function's package holds, from the repository root to its place in the
#: package: Svar's code at the top, where Lambda imports ``handler``, and Taug's library under
#: the name the tests import it by, ``taug.access``.
ANSWER_PACKAGE = (
    ("svar/app", "."),
    ("taug/__init__.py", "taug/__init__.py"),
    ("taug/access", "taug/access"),
)

#: `ASSET_EXCLUDES` as names: the package leaves these out wherever they are.
PACKAGE_EXCLUDES = tuple(pattern.removeprefix("**/") for pattern in ASSET_EXCLUDES)

#: USD per 1,000 tokens or guardrail text units, standard tier: the AWS Price
#: List API behind aws.amazon.com/bedrock/pricing, read on the region's price
#: date in taug/infra/answer_models.py. The token prices here are the default
#: model's; a stack takes its own model's from its region's table. The three
#: guardrail prices are the same in eu-west-1 and eu-central-1 (price lists of
#: 28.09.2026 and 06.10.2026).
PRICE_INPUT_PER_1K_TOKENS = answer_model(ANSWER_MODEL_ID).input_per_1k
PRICE_OUTPUT_PER_1K_TOKENS = answer_model(ANSWER_MODEL_ID).output_per_1k
PRICE_TOPIC_PER_1K_UNITS = "0.15"
PRICE_PII_PER_1K_UNITS = "0.10"
PRICE_GROUNDING_PER_1K_UNITS = "0.10"

BUDGET_TABLE_NAME = "andur-key-budgets"

BUDGET_KEY_ATTRIBUTE = "key_hash"

BUDGET_TTL_ATTRIBUTE = "delete_after"

DEFAULT_QUESTION_LIMIT = 10
DEFAULT_WINDOW_HOURS = 48

DEFAULT_KEEP_DAYS = 365

ANSWER_FUNCTION_NAME = "andur-answer"

#: Longer than `worst_case_seconds` in svar/app/aws.py, 189 since the topic check of the question
#: (06.10.2026) and 190 without CloudFront (07.10.2026), so a hanging AWS call ends as the
#: handler's 502, not as a Lambda timeout.
ANSWER_TIMEOUT_SECONDS = 195

ANSWER_MEMORY_MB = 512

#: At most this many answers at once. A new account may allow only 10 concurrent executions
#: in all and Lambda keeps 10 of them unreserved, so there the reservation fails the deploy.
#: `cdk deploy -c answer_max_concurrency=none` leaves it out; the account's own limit then
#: caps the function.
ANSWER_MAX_CONCURRENCY = 5

#: Where the function serves the page itself, the room it gets on top for one page load:
#: index.html and the four files it loads at once, each a call of the function. Without it two
#: questions in flight would leave a page load short and the page empty, since its words come
#: from its scripts.
PAGE_LOAD_CONCURRENCY = 5

#: How long a deploy that replaces a guardrail version waits after the answer function has
#: changed, before CloudFormation deletes the old versions: longer than an answer may run, so
#: no answer that started on an old version still needs it (svar/infra/deploy_drain/handler.py).
DEPLOY_DRAIN_SECONDS = ANSWER_TIMEOUT_SECONDS + 30

#: Where Lambda unpacks a layer: the page's files, where the function serves them.
PAGE_LAYER_FOLDER = "/opt"


def create_budget_table(stack: "AndurStack") -> dynamodb.TableV2:
    """One item per key: what it has spent, of how much, and until when."""
    return dynamodb.TableV2(
        stack,
        "KeyBudgetTable",
        table_name=BUDGET_TABLE_NAME,
        partition_key=dynamodb.Attribute(
            name=BUDGET_KEY_ATTRIBUTE, type=dynamodb.AttributeType.STRING
        ),
        billing=dynamodb.Billing.on_demand(),
        time_to_live_attribute=BUDGET_TTL_ATTRIBUTE,
        removal_policy=RemovalPolicy.DESTROY,
    )


def invoke_answer_model(stack: "AndurStack", guardrail_arn: str) -> list[iam.PolicyStatement]:
    """Invoke the answer model only with a numbered version of the answer guardrail.

    Any numbered version, not the deployed one alone: a deploy that changes a guardrail
    setting makes a new version, and with the number in this condition CloudFormation
    updated the role about 23 seconds before the function, which still called with the
    old number and ended in 502. Without the number, a new version leaves this role as it
    is. The draft (the ARN without a number), every other guardrail and a call without
    one stay refused. Example 3 of "Enforce the use of specific guardrails" in the Bedrock
    user guide; docs/decisions.md, 05.10.2026.

    An EU profile takes two statements: the profile in this region, and the
    model in the EU regions it routes to, reachable only through that profile.
    """
    # `:*` alone would also match a `:DRAFT` suffix, should Bedrock name the draft so; the
    # draft changes before CloudFormation makes the version, so it is shut out by name.
    with_guardrail = {
        "ArnLike": {"bedrock:GuardrailIdentifier": f"{guardrail_arn}:*"},
        "StringNotLike": {"bedrock:GuardrailIdentifier": "*:DRAFT"},
    }
    if not stack.answer_model_id.startswith(EU_PROFILE_PREFIX):
        return [
            iam.PolicyStatement(
                sid="InvokeAnswerModelStatement",
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/"
                    f"{stack.answer_model_id}"
                ],
                conditions=with_guardrail,
            )
        ]
    profile_arn = (
        f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:inference-profile/"
        f"{stack.answer_model_id}"
    )
    model_id = stack.answer_model_id.removeprefix(EU_PROFILE_PREFIX)
    return [
        iam.PolicyStatement(
            sid="InvokeAnswerModelProfile",
            actions=["bedrock:InvokeModel"],
            resources=[profile_arn],
            conditions=with_guardrail,
        ),
        iam.PolicyStatement(
            sid="InvokeAnswerModelInEurope",
            actions=["bedrock:InvokeModel"],
            resources=[f"arn:{Aws.PARTITION}:bedrock:eu-*::foundation-model/{model_id}"],
            conditions={"StringEquals": {"bedrock:InferenceProfileArn": profile_arn}},
        ),
    ]


def create_answer_role(
    stack: "AndurStack",
    knowledge_base: bedrock.CfnKnowledgeBase,
    budget_table: dynamodb.TableV2,
    log_group: logs.LogGroup,
    guardrail: bedrock.CfnGuardrail,
    excerpt_guardrail: bedrock.CfnGuardrail,
    topic_guardrail: bedrock.CfnGuardrail | None,
) -> iam.Role:
    """The execution role of the answer function: five inline policies, every statement
    on named resources. Most name one; `ApplyAnswerGuardrail` names nine — the three
    guardrails, and the EU guardrail profile in each of its six regions, which the topic
    guardrail runs through and, where `answer_guardrail_profile`, the answer guardrail. With
    a topic model it names the two guardrails alone, and `InvokeTopicModel` lets the function
    call that one model in the stack's region, with nothing else.

    It names no guardrail version, so a deploy that replaces one leaves the role as it is.
    """
    guardrails = [guardrail.attr_guardrail_arn, excerpt_guardrail.attr_guardrail_arn]
    if topic_guardrail is not None:
        guardrails += [
            topic_guardrail.attr_guardrail_arn,
            # The answer and topic guardrails run through the EU profile.
            *(guardrail_profile_arn(stack, region) for region in GUARDRAIL_PROFILE_REGIONS),
        ]
    topic_model = (
        []
        if stack.topic_model_id is None
        else [
            iam.PolicyStatement(
                sid="InvokeTopicModel",
                actions=["bedrock:InvokeModel"],
                # The stack's region by name, not by reference: the template shows where.
                resources=[
                    f"arn:{Aws.PARTITION}:bedrock:{stack.region}::foundation-model/"
                    f"{stack.topic_model_id}"
                ],
            )
        ]
    )
    return iam.Role(
        stack,
        "AnswerFunctionRole",
        description=("Lets Svar's answer function read keys, retrieve, invoke the model and log."),
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        inline_policies={
            "ReadApiKeys": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="ReadWorkspaceKeys",
                        actions=["ssm:GetParameter"],
                        resources=[f"{api_key_path_arn()}/*"],
                    )
                ]
            ),
            "CountQuestionsPerKey": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="CountQuestionAgainstKeyBudget",
                        actions=["dynamodb:UpdateItem"],
                        resources=[budget_table.table_arn],
                    )
                ]
            ),
            "RetrieveFromKnowledgeBase": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="RetrieveStatement",
                        actions=["bedrock:Retrieve"],
                        resources=[knowledge_base.attr_knowledge_base_arn],
                    )
                ]
            ),
            "InvokeAnswerModel": iam.PolicyDocument(
                statements=[
                    *invoke_answer_model(stack, guardrail.attr_guardrail_arn),
                    iam.PolicyStatement(
                        sid="ApplyAnswerGuardrail",
                        actions=["bedrock:ApplyGuardrail"],
                        resources=guardrails,
                    ),
                    *topic_model,
                ]
            ),
            "WriteLogs": write_logs_policy(log_group),
        },
    )


def copy_answer_package(output_dir: Path, root: Path = REPOSITORY_ROOT) -> None:
    """Copy `ANSWER_PACKAGE` from ``root`` into ``output_dir``, without `PACKAGE_EXCLUDES`."""
    ignore = shutil.ignore_patterns(*PACKAGE_EXCLUDES)
    for source, target in ANSWER_PACKAGE:
        source_path = root / source
        target_path = output_dir / target
        if source_path.is_dir():
            shutil.copytree(source_path, target_path, ignore=ignore, dirs_exist_ok=True)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)


@jsii.implements(ILocalBundling)
class AnswerPackage:
    """Bundles the answer function on this machine by copying `ANSWER_PACKAGE`; no Docker."""

    def __init__(self, root: Path = REPOSITORY_ROOT) -> None:
        self.root = root

    def try_bundle(self, output_dir: str, *options: Any, **named: Any) -> bool:
        # jsii hands the bundling options over as one positional argument; nothing here
        # needs them.
        copy_answer_package(Path(output_dir), self.root)
        return True


def answer_code(root: Path = REPOSITORY_ROOT) -> lambda_.Code:
    """The answer function's package. Its hash is the hash of the package itself
    (`AssetHashType.OUTPUT`): hashed by its source folder, svar/app, a change in
    taug/access/ alone would never reach Lambda. The CLI bundles every stack for
    synth, diff, deploy and refactor unless ``--exclusively`` names some."""
    return lambda_.Code.from_asset(
        str(root / "svar" / "app"),
        asset_hash_type=AssetHashType.OUTPUT,
        bundling=BundlingOptions(
            # Required by the type, never pulled: the local bundler always bundles.
            image=DockerImage.from_registry("local-bundling-only"),
            local=AnswerPackage(root),
        ),
    )


def create_answer_function(
    stack: "AndurStack",
    knowledge_base: bedrock.CfnKnowledgeBase,
    budget_table: dynamodb.TableV2,
    role: iam.Role,
    log_group: logs.LogGroup,
    guardrail_version: bedrock.CfnGuardrailVersion,
    excerpt_guardrail_version: bedrock.CfnGuardrailVersion,
    topic_guardrail_version: bedrock.CfnGuardrailVersion | None,
    page_layer: lambda_.LayerVersion | None,
) -> lambda_.Function:
    """The question handler from app/, configured entirely through its environment: the
    topic guardrail's id and version, or the topic model and its prices; with
    ``page_layer``, the folder of the page it serves itself."""
    if topic_guardrail_version is not None:
        topic_check = {
            "TOPIC_GUARDRAIL_ID": topic_guardrail_version.attr_guardrail_id,
            "TOPIC_GUARDRAIL_VERSION": topic_guardrail_version.attr_version,
        }
    else:
        assert stack.topic_model is not None
        topic_check = {
            "TOPIC_MODEL_ID": stack.topic_model.model_id,
            "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS": stack.topic_model.input_per_1k,
            "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS": stack.topic_model.output_per_1k,
        }
    return lambda_.Function(
        stack,
        "AnswerFunction",
        function_name=ANSWER_FUNCTION_NAME,
        description="Andur: Svar answers a question from one workspace's documents.",
        runtime=LAMBDA_RUNTIME,
        architecture=lambda_.Architecture.ARM_64,
        code=answer_code(),
        handler="handler.handler",
        timeout=Duration.seconds(ANSWER_TIMEOUT_SECONDS),
        memory_size=ANSWER_MEMORY_MB,
        reserved_concurrent_executions=reserved_concurrency(stack),
        role=role,
        log_group=log_group,
        layers=None if page_layer is None else [page_layer],
        environment={
            **({} if page_layer is None else {PAGE_FOLDER_VARIABLE: PAGE_LAYER_FOLDER}),
            "KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
            "MODEL_ID": stack.answer_model_id,
            "ANSWER_PROMPT": prompt_of(stack.answer_model),
            "REGION": Aws.REGION,
            "NUMBER_OF_RESULTS": str(NUMBER_OF_RESULTS),
            "API_KEY_PARAMETER_PATH": API_KEY_PARAMETER_PATH,
            "BUDGET_TABLE_NAME": budget_table.table_name,
            "QUESTION_LIMIT": str(DEFAULT_QUESTION_LIMIT),
            "WINDOW_HOURS": str(DEFAULT_WINDOW_HOURS),
            "KEEP_DAYS": str(DEFAULT_KEEP_DAYS),
            "GUARDRAIL_ID": guardrail_version.attr_guardrail_id,
            "GUARDRAIL_VERSION": guardrail_version.attr_version,
            "EXCERPT_GUARDRAIL_ID": excerpt_guardrail_version.attr_guardrail_id,
            "EXCERPT_GUARDRAIL_VERSION": excerpt_guardrail_version.attr_version,
            **topic_check,
            "PRICE_INPUT_PER_1K_TOKENS": stack.answer_model.input_per_1k,
            "PRICE_OUTPUT_PER_1K_TOKENS": stack.answer_model.output_per_1k,
            "PRICE_TOPIC_PER_1K_UNITS": PRICE_TOPIC_PER_1K_UNITS,
            "PRICE_PII_PER_1K_UNITS": PRICE_PII_PER_1K_UNITS,
            "PRICE_GROUNDING_PER_1K_UNITS": PRICE_GROUNDING_PER_1K_UNITS,
            "PRICE_DATE": stack.models.price_date,
        },
    )


def reserved_concurrency(stack: "AndurStack") -> int | None:
    """The answer function's reservation: `answer_max_concurrency`, and where it serves the
    page as well, `PAGE_LOAD_CONCURRENCY` on top; None leaves it to the account."""
    if stack.answer_max_concurrency is None:
        return None
    page = 0 if stack.behind_cloudfront else PAGE_LOAD_CONCURRENCY
    return stack.answer_max_concurrency + page


def create_function_url(stack: "AndurStack", function: lambda_.Function) -> lambda_.FunctionUrl:
    """The function's own HTTPS address. Behind CloudFront set to AWS_IAM: reachable for
    CloudFront alone. Without CloudFront open (NONE): it is the address, and the key header
    is the gate, as it is behind CloudFront. CDK grants both actions a URL of NONE needs
    since October 2025, `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`, the second
    only through the URL."""
    auth_type = (
        lambda_.FunctionUrlAuthType.AWS_IAM
        if stack.behind_cloudfront
        else lambda_.FunctionUrlAuthType.NONE
    )
    return function.add_function_url(auth_type=auth_type)


def create_page_layer(stack: "AndurStack") -> lambda_.LayerVersion:
    """The page's files from web/, for a function that serves them itself; Lambda unpacks
    them into `PAGE_LAYER_FOLDER`. No bucket and nothing public: the files reach a browser
    only through the function."""
    return lambda_.LayerVersion(
        stack,
        "PageLayer",
        description="Andur: the page's files, served by the answer function.",
        code=lambda_.Code.from_asset(str(WEB_FOLDER), exclude=ASSET_EXCLUDES),
        compatible_runtimes=[LAMBDA_RUNTIME],
        compatible_architectures=[lambda_.Architecture.ARM_64],
    )


def create_deploy_drain(
    stack: "AndurStack", function: lambda_.Function, versions: list[bedrock.CfnGuardrailVersion]
) -> CustomResource:
    """A resource that waits `DEPLOY_DRAIN_SECONDS` after ``function`` has changed, in every
    deploy that replaces one of ``versions``.

    CloudFormation deletes a replaced version only in the cleanup phase, after every
    resource is updated, this one too; so the old versions outlive the answers that
    started on them. A deploy that replaces no version leaves it alone and does not wait.
    """
    log_group = create_log_group(stack, "DeployDrainFunctionLogs")
    drain = lambda_.Function(
        stack,
        "DeployDrainFunction",
        description="Andur: waits during a deploy until no answer needs an old version.",
        runtime=LAMBDA_RUNTIME,
        architecture=lambda_.Architecture.ARM_64,
        code=lambda_.Code.from_asset(
            str(REPOSITORY_ROOT / "svar" / "infra" / "deploy_drain"), exclude=ASSET_EXCLUDES
        ),
        handler="handler.on_event",
        timeout=Duration.seconds(DEPLOY_DRAIN_SECONDS + 60),
        role=iam.Role(
            stack,
            "DeployDrainFunctionRole",
            description="Lets the Andur deploy drain write its log; nothing else.",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={"WriteLogs": write_logs_policy(log_group)},
        ),
        log_group=log_group,
    )
    provider = cr.Provider(
        stack,
        "DeployDrainProvider",
        on_event_handler=drain,
        log_group=create_log_group(stack, "DeployDrainProviderLogs"),
    )
    resource = CustomResource(
        stack,
        "DeployDrain",
        service_token=provider.service_token,
        resource_type="Custom::AndurDeployDrain",
        properties={
            # A new version changes its Ref, and that makes this an Update.
            "GuardrailVersions": [version.ref for version in versions],
            "Seconds": str(DEPLOY_DRAIN_SECONDS),
        },
    )
    resource.node.add_dependency(function)
    return resource

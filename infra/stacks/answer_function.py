"""The answer function: its role, its settings, its address, the key budgets it counts
against, and what a deploy needs around it."""

from typing import TYPE_CHECKING

from aws_cdk import (
    Aws,
    CustomResource,
    Duration,
    RemovalPolicy,
    aws_bedrock as bedrock,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    custom_resources as cr,
)

from app.page import PAGE_FOLDER_VARIABLE
from infra.answer_models import (
    DEFAULT_REGION,
    answer_model,
    region_models,
)
from infra.stacks.guardrails import GUARDRAIL_PROFILE_REGIONS, guardrail_profile_arn
from infra.stacks.shared import (
    ASSET_EXCLUDES,
    LAMBDA_RUNTIME,
    REPOSITORY_ROOT,
    WEB_FOLDER,
    create_log_group,
    write_logs_policy,
)

if TYPE_CHECKING:
    from infra.stacks.andur_stack import AndurStack


#: A model id runs in the stack's region; an ``eu.`` inference profile id runs
#: in the EU regions that profile routes to, and nowhere else. Its token prices
#: come from infra/answer_models.py, like the model of each region.
ANSWER_MODEL_ID = region_models(DEFAULT_REGION).answer_model_id

EU_PROFILE_PREFIX = "eu."

#: How many passages retrieve hands to the model per question.
NUMBER_OF_RESULTS = 10

#: USD per 1,000 tokens or guardrail text units, standard tier: the AWS Price
#: List API behind aws.amazon.com/bedrock/pricing, read on the region's price
#: date in infra/answer_models.py. The token prices here are the default
#: model's; a stack takes its own model's from its region's table. The three
#: guardrail prices are the same in eu-west-1 and eu-central-1 (price lists of
#: 28.09.2026 and 06.10.2026).
PRICE_INPUT_PER_1K_TOKENS = answer_model(ANSWER_MODEL_ID).input_per_1k
PRICE_OUTPUT_PER_1K_TOKENS = answer_model(ANSWER_MODEL_ID).output_per_1k
PRICE_TOPIC_PER_1K_UNITS = "0.15"
PRICE_PII_PER_1K_UNITS = "0.10"
PRICE_GROUNDING_PER_1K_UNITS = "0.10"

#: Where the workspace keys live: ``<path>/<workspace>``, one SecureString each.
#: The stack creates none; `python -m scripts.workspace` adds and removes them.
API_KEY_PARAMETER_PATH = "/andur/api-keys"

BUDGET_TABLE_NAME = "andur-key-budgets"

BUDGET_KEY_ATTRIBUTE = "key_hash"

BUDGET_TTL_ATTRIBUTE = "delete_after"

DEFAULT_QUESTION_LIMIT = 10
DEFAULT_WINDOW_HOURS = 48

DEFAULT_KEEP_DAYS = 365

ANSWER_FUNCTION_NAME = "andur-answer"

#: Longer than `worst_case_seconds` in app/aws.py, 189 since the topic check of the question
#: (06.10.2026) and 190 without CloudFront (07.10.2026), so a hanging AWS call ends as the
#: handler's 502, not as a Lambda timeout.
ANSWER_TIMEOUT_SECONDS = 195

ANSWER_MEMORY_MB = 512

#: At most this many answers at once. A new account may allow only 10 concurrent executions
#: in all and Lambda keeps 10 of them unreserved, so there the reservation fails the deploy (the
#: sub-account of 06.10.2026: 10). `cdk deploy -c answer_max_concurrency=none` leaves it out;
#: the account's own limit then caps the function.
ANSWER_MAX_CONCURRENCY = 5

#: Where the function serves the page itself, the room it gets on top for one page load:
#: index.html and the four files it loads at once, each a call of the function. Without it two
#: questions in flight would leave a page load short and the page empty, since its words come
#: from its scripts.
PAGE_LOAD_CONCURRENCY = 5

#: How long a deploy that replaces a guardrail version waits after the answer function has
#: changed, before CloudFormation deletes the old versions: longer than an answer may run, so
#: no answer that started on an old version still needs it (infra/deploy_drain/handler.py).
DEPLOY_DRAIN_SECONDS = ANSWER_TIMEOUT_SECONDS + 30

#: Where Lambda unpacks a layer: the page's files, where the function serves them.
PAGE_LAYER_FOLDER = "/opt"


def create_key_retirer(stack: "AndurStack") -> cr.Provider:
    """The function that answers the Delete of the key resources this stack used to have.

    It deletes nothing (infra/api_keys/handler.py), and it has no permission on Parameter
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
            str(REPOSITORY_ROOT / "infra" / "api_keys"), exclude=ASSET_EXCLUDES
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
        code=lambda_.Code.from_asset(str(REPOSITORY_ROOT / "app"), exclude=ASSET_EXCLUDES),
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
            "ANSWER_PROMPT": stack.answer_model.prompt,
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
            str(REPOSITORY_ROOT / "infra" / "deploy_drain"), exclude=ASSET_EXCLUDES
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

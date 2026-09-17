"""Assertions against the synthesised CloudFormation template.

These tests never call AWS. `Template.from_stack` renders the stack into the
JSON that CloudFormation would receive, and each test asserts one promise
that template has to keep. They catch a broken stack in a second, before a
deploy spends ten minutes finding out.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

# The CDK is a heavy dependency and only these tests need it; without it they
# skip rather than break the run, the way the botocore tests already do.
pytest.importorskip("aws_cdk")

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402

from scripts import ask  # noqa: E402

import aws  # app/aws.py, imported by its Lambda name (tests/conftest.py)
import handler  # app/handler.py, likewise
from infra.stacks.andur_stack import (
    ANSWER_FUNCTION_NAME,
    ANSWER_MAX_CONCURRENCY,
    ANSWER_MEMORY_MB,
    ANSWER_MODEL_ID,
    ANSWER_TIMEOUT_SECONDS,
    API_KEY_PARAMETER_PATH,
    ASK_PATH,
    BUDGET_KEY_ATTRIBUTE,
    BUDGET_TABLE_NAME,
    BUDGET_TTL_ATTRIBUTE,
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_PERCENT,
    DATA_SOURCE_NAME,
    DEFAULT_KEEP_DAYS,
    DEFAULT_QUESTION_LIMIT,
    DEFAULT_WINDOW_HOURS,
    DISTANCE_METRIC,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_ID,
    KNOWLEDGE_BASE_NAME,
    NON_FILTERABLE_METADATA_KEYS,
    NONCURRENT_VERSION_RETENTION_DAYS,
    NUMBER_OF_RESULTS,
    ORIGIN_RESPONSE_TIMEOUT_SECONDS,
    REPOSITORY_ROOT,
    SITE_ROOT_DOCUMENT,
    VECTOR_DATA_TYPE,
    VECTOR_INDEX_NAME,
    WEB_FOLDER,
    WORKSPACE_PREFIX,
    WORKSPACES,
    AndurStack,
    replacement_safe_name,
)


@pytest.fixture(scope="module")
def stack() -> AndurStack:
    app = cdk.App()
    return AndurStack(app, "TestAndurStack")


@pytest.fixture(scope="module")
def template(stack: AndurStack) -> Template:
    """One synthesis for the whole module — it is the slow part."""
    return Template.from_stack(stack)


def only_resource(template: Template, resource_type: str) -> Mapping[str, Any]:
    """The single resource of a type; fails loudly if there are more or none."""
    (resource,) = template.find_resources(resource_type).values()
    return resource


def get_att(stack: AndurStack, construct: Any, attribute: str) -> dict[str, list[str]]:
    """The reference CloudFormation renders when one resource points at another."""
    return {"Fn::GetAtt": [stack.get_logical_id(construct), attribute]}


def resource_of(stack: AndurStack, template: Template, construct: Any) -> Mapping[str, Any]:
    """The template entry of one construct; an L2 construct is looked up by its L1 child."""
    cfn = construct.node.default_child or construct
    return template.to_json()["Resources"][stack.get_logical_id(cfn)]


def role_policies(stack: AndurStack, template: Template, role: Any) -> dict[str, dict[str, Any]]:
    """The inline policies of one role, by policy name."""
    policies = resource_of(stack, template, role)["Properties"]["Policies"]
    return {p["PolicyName"]: p["PolicyDocument"] for p in policies}


def joined_last_part(value: Mapping[str, Any]) -> str:
    """The literal end of an ARN that CloudFormation renders as a Fn::Join."""
    return value["Fn::Join"][1][-1]


# --- Documents bucket ----------------------------------------------------


def test_documents_bucket_blocks_all_public_access(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


def test_documents_bucket_is_versioned(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket", {"VersioningConfiguration": {"Status": "Enabled"}}
    )


def test_old_document_versions_expire(template: Template) -> None:
    """Versioning without an expiry would keep every old version forever."""
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": [
                    Match.object_like(
                        {
                            "Status": "Enabled",
                            "NoncurrentVersionExpiration": {
                                "NoncurrentDays": NONCURRENT_VERSION_RETENTION_DAYS
                            },
                        }
                    )
                ]
            }
        },
    )


# --- What survives cdk destroy, and what does not ------------------------


def test_documents_and_their_policy_survive_stack_deletion(
    stack: AndurStack, template: Template
) -> None:
    """Documents are the one thing that cannot be rebuilt — nor their SSL-only rule."""
    bucket = resource_of(stack, template, stack.documents_bucket)
    policy = resource_of(stack, template, stack.documents_bucket.policy)
    assert bucket["DeletionPolicy"] == "Retain"
    assert policy["DeletionPolicy"] == "Retain"


def test_the_page_and_its_bucket_go_with_the_stack(stack: AndurStack, template: Template) -> None:
    """Every file in it is a copy of web/ in the repository, so nothing is lost."""
    assert resource_of(stack, template, stack.site_bucket)["DeletionPolicy"] == "Delete"
    # Without this the bucket would still hold the page and refuse to be deleted.
    copier = only_resource(template, "Custom::CDKBucketDeployment")
    assert copier["Properties"]["RetainOnDelete"] is False
    assert copier["Properties"]["Prune"] is True


def test_derived_data_is_deleted_with_the_stack(template: Template) -> None:
    """Vectors are rebuilt from documents, so nothing derived may linger."""
    assert only_resource(template, "AWS::S3Vectors::VectorBucket")["DeletionPolicy"] == "Delete"
    assert only_resource(template, "AWS::S3Vectors::Index")["DeletionPolicy"] == "Delete"

    data_source = only_resource(template, "AWS::Bedrock::DataSource")
    assert data_source["Properties"]["DataDeletionPolicy"] == "DELETE"


# --- Vector store --------------------------------------------------------


def test_vector_index_matches_the_embedding_model(template: Template) -> None:
    """Index and knowledge base must agree on dimension and data type."""
    template.has_resource_properties(
        "AWS::S3Vectors::Index",
        {
            "DataType": "float32",
            "Dimension": EMBEDDING_DIMENSION,
            "DistanceMetric": "cosine",
        },
    )


def test_vector_index_name_carries_its_configuration(template: Template) -> None:
    index = only_resource(template, "AWS::S3Vectors::Index")
    name = index["Properties"]["IndexName"]

    assert name == VECTOR_INDEX_NAME
    assert f"-{EMBEDDING_DIMENSION}-" in name
    assert f"-{DISTANCE_METRIC}-" in name


def test_replacement_safe_name_changes_with_every_setting() -> None:
    """Any change that replaces a resource must also rename it, or the two collide."""
    settings = [VECTOR_DATA_TYPE, EMBEDDING_DIMENSION, DISTANCE_METRIC, NON_FILTERABLE_METADATA_KEYS]
    variants = [
        ["float16", *settings[1:]],
        [settings[0], 512, *settings[2:]],
        [*settings[:2], "euclidean", settings[3]],
        [*settings[:3], [*NON_FILTERABLE_METADATA_KEYS, "author"]],
        [*settings[:3], list(reversed(NON_FILTERABLE_METADATA_KEYS))],
    ]
    names = {replacement_safe_name("andur-index-", s) for s in [settings, *variants]}
    assert len(names) == len(variants) + 1


def test_knowledge_base_and_data_source_names_follow_their_configuration(
    template: Template,
) -> None:
    """A new index replaces the base; the base must not collide with its old self."""
    knowledge_base = only_resource(template, "AWS::Bedrock::KnowledgeBase")["Properties"]
    data_source = only_resource(template, "AWS::Bedrock::DataSource")["Properties"]

    assert knowledge_base["Name"] == KNOWLEDGE_BASE_NAME
    assert data_source["Name"] == DATA_SOURCE_NAME
    assert KNOWLEDGE_BASE_NAME != replacement_safe_name(
        "andur-knowledge-base-",
        ["andur-index-other", EMBEDDING_MODEL_ID, EMBEDDING_DIMENSION, VECTOR_DATA_TYPE],
    )


def test_non_filterable_keys_in_the_template_are_exactly_the_constant(
    template: Template,
) -> None:
    """Not a subset: an extra key here would silently switch off a filter."""
    index = only_resource(template, "AWS::S3Vectors::Index")
    keys = index["Properties"]["MetadataConfiguration"]["NonFilterableMetadataKeys"]
    assert keys == NON_FILTERABLE_METADATA_KEYS


def test_bedrock_metadata_keys_are_not_filterable() -> None:
    """Chunk text and Bedrock's own metadata must not eat the filter budget."""
    assert "AMAZON_BEDROCK_TEXT" in NON_FILTERABLE_METADATA_KEYS
    assert "AMAZON_BEDROCK_METADATA" in NON_FILTERABLE_METADATA_KEYS


def test_workspace_and_layer_stay_filterable() -> None:
    """The two keys retrieval filters on must be absent from the exclusion list."""
    assert "workspace" not in NON_FILTERABLE_METADATA_KEYS
    assert "layer" not in NON_FILTERABLE_METADATA_KEYS


# --- Knowledge base ------------------------------------------------------


def test_knowledge_base_exists_and_uses_s3_vectors(template: Template) -> None:
    template.resource_count_is("AWS::Bedrock::KnowledgeBase", 1)
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {"StorageConfiguration": Match.object_like({"Type": "S3_VECTORS"})},
    )


def test_embedding_model_is_titan_text_embeddings_v2(template: Template) -> None:
    """The model id is buried in a Fn::Join; assert on the rendered JSON."""
    properties = only_resource(template, "AWS::Bedrock::KnowledgeBase")["Properties"]
    vector_config = properties["KnowledgeBaseConfiguration"]["VectorKnowledgeBaseConfiguration"]

    model_arn_parts = vector_config["EmbeddingModelArn"]["Fn::Join"][1]
    assert f"::foundation-model/{EMBEDDING_MODEL_ID}" in model_arn_parts
    assert EMBEDDING_MODEL_ID == "amazon.titan-embed-text-v2:0"

    embedding_config = vector_config["EmbeddingModelConfiguration"]
    assert embedding_config["BedrockEmbeddingModelConfiguration"] == {
        "Dimensions": EMBEDDING_DIMENSION,
        "EmbeddingDataType": "FLOAT32",
    }


def test_resources_are_wired_by_reference(stack: AndurStack, template: Template) -> None:
    """References, not copied strings: a renamed resource keeps its wiring."""
    knowledge_base = only_resource(template, "AWS::Bedrock::KnowledgeBase")["Properties"]
    assert knowledge_base["StorageConfiguration"]["S3VectorsConfiguration"]["IndexArn"] == get_att(
        stack, stack.vector_index, "IndexArn"
    )
    assert knowledge_base["RoleArn"] == get_att(
        stack, stack.knowledge_base_role.node.default_child, "Arn"
    )

    data_source = only_resource(template, "AWS::Bedrock::DataSource")["Properties"]
    assert data_source["DataSourceConfiguration"]["S3Configuration"]["BucketArn"] == get_att(
        stack, stack.documents_bucket.node.default_child, "Arn"
    )
    assert data_source["KnowledgeBaseId"] == get_att(
        stack, stack.knowledge_base, "KnowledgeBaseId"
    )


# --- Data source ---------------------------------------------------------


def test_data_source_crawls_only_the_workspace_prefix(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "DataSourceConfiguration": Match.object_like(
                {
                    "Type": "S3",
                    "S3Configuration": Match.object_like(
                        {"InclusionPrefixes": [WORKSPACE_PREFIX]}
                    ),
                }
            )
        },
    )


def test_chunking_is_fixed_size_as_configured(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "VectorIngestionConfiguration": {
                "ChunkingConfiguration": {
                    "ChunkingStrategy": "FIXED_SIZE",
                    "FixedSizeChunkingConfiguration": {
                        "MaxTokens": CHUNK_MAX_TOKENS,
                        "OverlapPercentage": CHUNK_OVERLAP_PERCENT,
                    },
                }
            }
        },
    )


# --- Permissions ---------------------------------------------------------


def test_role_may_only_read_our_own_documents_bucket(
    stack: AndurStack, template: Template
) -> None:
    bucket_arn = get_att(stack, stack.documents_bucket.node.default_child, "Arn")
    same_account = {"StringEquals": {"aws:ResourceAccount": {"Ref": "AWS::AccountId"}}}

    policies = role_policies(stack, template, stack.knowledge_base_role)
    list_bucket, get_object = policies["ReadDocuments"]["Statement"]

    assert list_bucket["Action"] == "s3:ListBucket"
    assert list_bucket["Resource"] == bucket_arn
    assert list_bucket["Condition"] == same_account

    assert get_object["Action"] == "s3:GetObject"
    assert get_object["Resource"] == {"Fn::Join": ["", [bucket_arn, "/*"]]}
    assert get_object["Condition"] == same_account


def test_role_may_only_invoke_the_embedding_model(stack: AndurStack, template: Template) -> None:
    policies = role_policies(stack, template, stack.knowledge_base_role)
    list_models, invoke_model = policies["InvokeEmbeddingModel"]["Statement"]

    assert list_models["Action"] == "bedrock:ListFoundationModels"
    assert list_models["Resource"] == "*"

    assert invoke_model["Action"] == "bedrock:InvokeModel"
    model_arn_parts = invoke_model["Resource"]["Fn::Join"][1]
    assert model_arn_parts[-1] == f"::foundation-model/{EMBEDDING_MODEL_ID}"


def all_policy_statements(template: Template) -> list[tuple[str, dict[str, Any]]]:
    """Every statement of every inline role policy and every policy resource, with its owner."""
    statements: list[tuple[str, dict[str, Any]]] = []
    for logical_id, role in template.find_resources("AWS::IAM::Role").items():
        for policy in role["Properties"].get("Policies", []):
            statements += [(logical_id, statement) for statement in policy["PolicyDocument"]["Statement"]]
    for logical_id, policy in template.find_resources("AWS::IAM::Policy").items():
        statements += [(logical_id, statement) for statement in policy["Properties"]["PolicyDocument"]["Statement"]]
    return statements


def test_the_only_wildcard_resources_are_model_listing_and_cache_clearing(template: Template) -> None:
    """Two actions that cannot be scoped; nothing else gets to use that excuse.

    ``bedrock:ListFoundationModels`` takes no resource at all. CloudFront
    invalidation is CDK's: the file copier clears the caches after an upload,
    and the role that does it is built by the construct, which grants the two
    invalidation actions on ``*``. Narrowing that would mean replacing the
    construct. Nothing in this stack's own roles is on a wildcard.
    """
    wildcard_actions = [
        statement["Action"] for _, statement in all_policy_statements(template) if statement["Resource"] == "*"
    ]
    assert wildcard_actions == [
        "bedrock:ListFoundationModels",
        ["cloudfront:GetInvalidation", "cloudfront:CreateInvalidation"],
    ]


def test_only_cdk_s_own_deploy_time_roles_have_a_managed_policy(
    stack: AndurStack, template: Template
) -> None:
    """Known exceptions: both belong to functions CDK generates for a deploy.

    One runs the key generator, the other copies web/ into the site bucket.
    Each gets AWSLambdaBasicExecutionRole from its construct; this stack does
    not create either role and cannot narrow them without replacing the
    constructs. Every role the stack defines itself carries inline policies
    only, and both of these run during a deploy and never afterwards.
    """
    provider_role = stack.node.find_child("ApiKeyProvider").node.find_child("framework-onEvent").role
    copier_role = stack.site_files.handler_role
    expected = {
        stack.get_logical_id(provider_role.node.default_child),
        stack.get_logical_id(copier_role.node.default_child),
    }
    with_managed = {
        logical_id: role["Properties"]["ManagedPolicyArns"]
        for logical_id, role in template.find_resources("AWS::IAM::Role").items()
        if "ManagedPolicyArns" in role["Properties"]
    }
    assert set(with_managed) == expected
    for managed_arns in with_managed.values():
        (managed_arn,) = managed_arns
        assert joined_last_part(managed_arn) == ":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


def test_role_may_only_touch_our_own_vector_index(stack: AndurStack, template: Template) -> None:
    """No wildcard on s3vectors — one index, five actions."""
    (statement,) = role_policies(stack, template, stack.knowledge_base_role)["UseVectorIndex"]["Statement"]
    assert sorted(statement["Action"]) == [
        "s3vectors:DeleteVectors",
        "s3vectors:GetIndex",
        "s3vectors:GetVectors",
        "s3vectors:PutVectors",
        "s3vectors:QueryVectors",
    ]
    assert statement["Resource"] == {"Fn::GetAtt": ["VectorIndex", "IndexArn"]}


def test_role_can_only_be_assumed_by_bedrock_in_this_account(stack: AndurStack, template: Template) -> None:
    """Without the source conditions the role is a confused-deputy hole."""
    role = resource_of(stack, template, stack.knowledge_base_role)
    (statement,) = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]

    assert statement["Principal"] == {"Service": "bedrock.amazonaws.com"}
    assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}

    source_arn_parts = statement["Condition"]["ArnLike"]["aws:SourceArn"]["Fn::Join"][1]
    assert source_arn_parts[-1] == ":knowledge-base/*"
    assert {"Ref": "AWS::AccountId"} in source_arn_parts


def test_the_only_compute_that_runs_between_deploys_is_the_answer_function(
    stack: AndurStack, template: Template
) -> None:
    """Four functions, and three of them only ever run during a deploy.

    The answer function, the key generator with the CDK framework that runs
    it, and CDK's file copier that puts web/ into the site bucket. Nothing
    else runs, so nothing else can cost money while nobody asks.
    """
    template.resource_count_is("AWS::Lambda::Function", 4)
    functions = template.find_resources("AWS::Lambda::Function")
    assert stack.get_logical_id(stack.answer_function.node.default_child) in functions


# --- Outputs -------------------------------------------------------------


def test_stack_publishes_what_later_steps_need(template: Template) -> None:
    """Build steps 2 and 3 address this stack only through its outputs."""
    outputs = template.find_outputs("*")
    assert {
        "KnowledgeBaseId",
        "DataSourceId",
        "DocumentsBucketName",
        "VectorBucketName",
        "VectorIndexArn",
        "KnowledgeBaseRoleArn",
        "FunctionUrl",
        "WebUrl",
        "ApiKeyParameterPath",
        "BudgetTableName",
    } <= set(outputs)


def test_vector_bucket_output_is_a_name_not_an_arn(template: Template) -> None:
    """`ref` on a vector bucket yields its ARN — an easy mislabel to make."""
    outputs = template.find_outputs("VectorBucketName")
    rendered = str(outputs["VectorBucketName"]["Value"])
    assert "arn:" not in rendered
    assert "andur-vectors-" in rendered


# --- Answer function (build step 3) --------------------------------------


def test_answer_function_runs_the_handler_on_the_newest_runtime(stack: AndurStack, template: Template) -> None:
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert properties["FunctionName"] == ANSWER_FUNCTION_NAME
    assert properties["Runtime"] == "python3.14"
    assert properties["Handler"] == "handler.handler"
    assert properties["Architectures"] == ["arm64"]
    assert properties["MemorySize"] == ANSWER_MEMORY_MB
    assert properties["Timeout"] == ANSWER_TIMEOUT_SECONDS


def test_timeouts_nest_from_aws_calls_to_function_to_ask_script() -> None:
    """Each layer waits longer than the one inside it.

    A hanging Bedrock call ends as the handler's 502, not as a Lambda timeout;
    the function ends before the 15 minutes a Function URL call may take; and
    the ask script waits longer than the function, so it prints the 502. The
    innermost number counts the keys twice, because an unknown key is read
    again before it is refused.
    """
    assert aws.worst_case_seconds() == 135
    assert aws.worst_case_seconds() < ANSWER_TIMEOUT_SECONDS < ask.HTTP_TIMEOUT_SECONDS < 900


def test_cloudfront_gives_up_before_the_function_does() -> None:
    """The one layer that does not nest, and knowingly so.

    CloudFront may wait 120 seconds without an approved quota increase, and
    the function may run 180. An answer between the two reaches the browser as
    CloudFront's 504 while the function runs it to the end and charges the
    question. Today an answer takes 0.7 to 3 seconds, so the gap is a corner,
    not a case — and the alternative, a shorter function timeout, would cut
    off answers that are merely slow.
    """
    assert ORIGIN_RESPONSE_TIMEOUT_SECONDS < ANSWER_TIMEOUT_SECONDS
    assert ORIGIN_RESPONSE_TIMEOUT_SECONDS < aws.worst_case_seconds()


def test_answer_function_runs_at_most_a_few_requests_at_once(stack: AndurStack, template: Template) -> None:
    """The hard cap on Lambda time and model calls behind an address anyone may find."""
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert properties["ReservedConcurrentExecutions"] == ANSWER_MAX_CONCURRENCY == 5


def test_answer_function_is_configured_only_through_its_environment(
    stack: AndurStack, template: Template
) -> None:
    variables = resource_of(stack, template, stack.answer_function)["Properties"]["Environment"]["Variables"]
    assert variables == {
        "KNOWLEDGE_BASE_ID": get_att(stack, stack.knowledge_base, "KnowledgeBaseId"),
        "MODEL_ID": ANSWER_MODEL_ID,
        "REGION": {"Ref": "AWS::Region"},
        "NUMBER_OF_RESULTS": str(NUMBER_OF_RESULTS),
        "API_KEY_PARAMETER_PATH": API_KEY_PARAMETER_PATH,
        "BUDGET_TABLE_NAME": {"Ref": stack.get_logical_id(stack.budget_table.node.default_child)},
        "QUESTION_LIMIT": str(DEFAULT_QUESTION_LIMIT),
        "WINDOW_HOURS": str(DEFAULT_WINDOW_HOURS),
        "KEEP_DAYS": str(DEFAULT_KEEP_DAYS),
    }
    assert set(variables) == set(handler.ENVIRONMENT_VARIABLES.values())


def test_the_model_id_is_configuration_not_handler_code() -> None:
    for module in (REPOSITORY_ROOT / "app").glob("*.py"):
        assert ANSWER_MODEL_ID not in module.read_text(encoding="utf-8"), module.name


def test_answer_role_has_five_permissions_each_on_one_resource(stack: AndurStack, template: Template) -> None:
    policies = role_policies(stack, template, stack.answer_role)
    assert set(policies) == {
        "ReadApiKeys",
        "CountQuestionsPerKey",
        "RetrieveFromKnowledgeBase",
        "InvokeAnswerModel",
        "WriteLogs",
    }

    (read_keys,) = policies["ReadApiKeys"]["Statement"]
    assert read_keys["Action"] == "ssm:GetParametersByPath"
    assert joined_last_part(read_keys["Resource"]) == f":parameter{API_KEY_PARAMETER_PATH}"

    (count,) = policies["CountQuestionsPerKey"]["Statement"]
    assert count["Action"] == "dynamodb:UpdateItem"
    assert count["Resource"] == get_att(stack, stack.budget_table.node.default_child, "Arn")

    (retrieve,) = policies["RetrieveFromKnowledgeBase"]["Statement"]
    assert retrieve["Action"] == "bedrock:Retrieve"
    assert retrieve["Resource"] == get_att(stack, stack.knowledge_base, "KnowledgeBaseArn")

    (invoke,) = policies["InvokeAnswerModel"]["Statement"]
    assert invoke["Action"] == "bedrock:InvokeModel"
    assert joined_last_part(invoke["Resource"]) == f"::foundation-model/{ANSWER_MODEL_ID}"

    (write_logs,) = policies["WriteLogs"]["Statement"]
    assert sorted(write_logs["Action"]) == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert write_logs["Resource"] == get_att(stack, stack.answer_log_group.node.default_child, "Arn")


def test_answer_role_has_no_managed_policy(stack: AndurStack, template: Template) -> None:
    """The managed basic execution policy would allow writing to every log group."""
    assert "ManagedPolicyArns" not in resource_of(stack, template, stack.answer_role)["Properties"]


def test_the_function_url_needs_signed_requests(stack: AndurStack, template: Template) -> None:
    """AuthType AWS_IAM: the URL is not a way in, and origin access control demands it.

    No CORS configuration either — the page is served from the same address,
    so there is no cross-origin request to allow.
    """
    template.resource_count_is("AWS::Lambda::Url", 1)
    url = resource_of(stack, template, stack.function_url)
    assert url["Properties"]["AuthType"] == "AWS_IAM"
    assert "Cors" not in url["Properties"]
    key_ids = {stack.get_logical_id(key.node.default_child) for key in stack.api_keys}
    assert key_ids <= set(url["DependsOn"])


def test_exactly_one_output_is_a_function_url(stack: AndurStack, template: Template) -> None:
    url_id = stack.get_logical_id(stack.function_url.node.default_child)
    url_outputs = [
        name for name, output in template.find_outputs("*").items() if url_id in json.dumps(output["Value"])
    ]
    assert url_outputs == ["FunctionUrl"]
    assert template.find_outputs("FunctionUrl")["FunctionUrl"]["Value"] == get_att(
        stack, stack.function_url.node.default_child, "FunctionUrl"
    )


# --- The page, and the one address in front of everything ----------------


def distribution_config(stack: AndurStack, template: Template) -> Mapping[str, Any]:
    return resource_of(stack, template, stack.distribution)["Properties"]["DistributionConfig"]


def test_only_cloudfront_may_invoke_the_function_and_only_this_distribution(
    stack: AndurStack, template: Template
) -> None:
    """Both permissions the documentation asks for, both bound to this distribution.

    One is added by the origin access control construct, the other by the
    stack. Without the source condition any distribution in any account could
    invoke the function.
    """
    distribution_arn_parts = [
        "arn:",
        {"Ref": "AWS::Partition"},
        ":cloudfront::",
        {"Ref": "AWS::AccountId"},
        ":distribution/",
        {"Ref": stack.get_logical_id(stack.distribution.node.default_child)},
    ]
    permissions = template.find_resources("AWS::Lambda::Permission")
    assert len(permissions) == 2
    actions = set()
    for permission in permissions.values():
        properties = permission["Properties"]
        assert properties["Principal"] == "cloudfront.amazonaws.com"
        assert properties["SourceArn"]["Fn::Join"] == ["", distribution_arn_parts]
        actions.add(properties["Action"])
    assert actions == {"lambda:InvokeFunctionUrl", "lambda:InvokeFunction"}


def test_the_site_bucket_is_private_and_no_website(stack: AndurStack, template: Template) -> None:
    """An S3 website endpoint serves plain HTTP only, so the bucket is never one."""
    properties = resource_of(stack, template, stack.site_bucket)["Properties"]
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert "WebsiteConfiguration" not in properties
    assert "VersioningConfiguration" not in properties


def test_the_site_bucket_opens_only_for_this_distribution(
    stack: AndurStack, template: Template
) -> None:
    policy = resource_of(stack, template, stack.site_bucket.policy)["Properties"]["PolicyDocument"]
    (read,) = [s for s in policy["Statement"] if s["Effect"] == "Allow"]
    assert read["Action"] == "s3:GetObject"
    assert read["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    source_arn = read["Condition"]["StringEquals"]["AWS:SourceArn"]["Fn::Join"][1]
    assert {"Ref": stack.get_logical_id(stack.distribution.node.default_child)} in source_arn


def test_both_origins_are_reached_through_an_origin_access_control(
    stack: AndurStack, template: Template
) -> None:
    """Neither the bucket nor the function URL is reachable beside the distribution."""
    origins = distribution_config(stack, template)["Origins"]
    assert len(origins) == 2
    for origin in origins:
        assert "OriginAccessControlId" in origin

    controls = template.find_resources("AWS::CloudFront::OriginAccessControl")
    assert len(controls) == 2
    kinds = set()
    for control in controls.values():
        config = control["Properties"]["OriginAccessControlConfig"]
        assert config["SigningBehavior"] == "always"
        assert config["SigningProtocol"] == "sigv4"
        kinds.add(config["OriginAccessControlOriginType"])
    assert kinds == {"s3", "lambda"}


def test_the_page_is_served_over_https_only(stack: AndurStack, template: Template) -> None:
    config = distribution_config(stack, template)
    assert config["DefaultRootObject"] == SITE_ROOT_DOCUMENT
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    assert config["DefaultCacheBehavior"]["AllowedMethods"] == ["GET", "HEAD"]
    for behavior in config["CacheBehaviors"]:
        assert behavior["ViewerProtocolPolicy"] == "https-only"


def test_the_ask_path_posts_to_the_function_and_is_never_cached(
    stack: AndurStack, template: Template
) -> None:
    """An answer differs per question and per key; a cached one would be the wrong answer."""
    (behavior,) = distribution_config(stack, template)["CacheBehaviors"]
    assert behavior["PathPattern"] == ASK_PATH
    assert "POST" in behavior["AllowedMethods"]
    # CachingDisabled and AllViewerExceptHostHeader, the two managed policies.
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert behavior["OriginRequestPolicyId"] == "b689b0a8-53d0-40ab-baf2-68738e2966ac"

    function_origin = [o for o in distribution_config(stack, template)["Origins"] if "CustomOriginConfig" in o]
    (origin,) = function_origin
    assert behavior["TargetOriginId"] == origin["Id"]
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"


def test_cloudfront_waits_as_long_as_an_account_may_without_asking(
    stack: AndurStack, template: Template
) -> None:
    """The response-timeout quota is 1-120 seconds; more needs an approved increase."""
    (origin,) = [o for o in distribution_config(stack, template)["Origins"] if "CustomOriginConfig" in o]
    assert origin["CustomOriginConfig"]["OriginReadTimeout"] == ORIGIN_RESPONSE_TIMEOUT_SECONDS == 120


def test_the_web_address_is_the_distribution(stack: AndurStack, template: Template) -> None:
    value = template.find_outputs("WebUrl")["WebUrl"]["Value"]
    assert value["Fn::Join"][1] == [
        "https://",
        get_att(stack, stack.distribution.node.default_child, "DomainName"),
    ]


# --- The page's own files ------------------------------------------------


def web_files() -> list[Path]:
    return sorted(p for p in WEB_FOLDER.iterdir() if p.is_file())


def test_the_page_loads_nothing_from_anywhere_else() -> None:
    """No framework, no font, no script from a server that is not this one."""
    markup = (WEB_FOLDER / SITE_ROOT_DOCUMENT).read_text(encoding="utf-8")
    referenced = re.findall(r'(?:src|href)="([^"]*)"', markup)
    assert referenced, "the page should reference its script and its stylesheet"
    names = {path.name for path in web_files()}
    for reference in referenced:
        # A data: URI fetches nothing; everything else must be a file of ours.
        assert reference.startswith("data:") or reference in names, reference
    assert {"app.js", "style.css"} <= set(referenced)


def test_no_file_of_the_page_names_an_address_of_its_own() -> None:
    """The page calls /ask relatively; an address in it would be one to keep in step."""
    for path in web_files():
        if path.suffix == ".md":
            continue
        content = path.read_text(encoding="utf-8")
        for forbidden in ("lambda-url", "amazonaws", "cloudfront.net"):
            assert forbidden not in content, f"{path.name} names {forbidden}"


#: What a workspace key looks like: 43 URL-safe characters from
#: ``secrets.token_urlsafe(32)``. Mixed case and a digit, so that a long
#: ordinary word cannot be mistaken for one.
KEY_SHAPED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43,}(?![A-Za-z0-9_-])")


def test_no_key_is_in_the_page() -> None:
    for path in web_files():
        for candidate in KEY_SHAPED.findall(path.read_text(encoding="utf-8")):
            looks_like_a_key = (
                any(c.isdigit() for c in candidate)
                and any(c.islower() for c in candidate)
                and any(c.isupper() for c in candidate)
            )
            assert not looks_like_a_key, f"{path.name} carries something key-shaped"


def test_the_page_never_prints_the_key() -> None:
    """Not to the console, and not into the address — the key goes into one header."""
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    assert "console." not in script


# --- Key budgets ---------------------------------------------------------


def budget_table(stack: AndurStack, template: Template) -> Mapping[str, Any]:
    """The rendered table. ``TableV2`` renders as a global table with one replica."""
    return resource_of(stack, template, stack.budget_table)


def test_the_budget_table_is_one_item_per_key_hash(stack: AndurStack, template: Template) -> None:
    template.resource_count_is("AWS::DynamoDB::GlobalTable", 1)
    properties = budget_table(stack, template)["Properties"]
    assert properties["TableName"] == BUDGET_TABLE_NAME
    assert properties["KeySchema"] == [{"AttributeName": BUDGET_KEY_ATTRIBUTE, "KeyType": "HASH"}]
    assert properties["AttributeDefinitions"] == [
        {"AttributeName": BUDGET_KEY_ATTRIBUTE, "AttributeType": "S"}
    ]
    assert properties["Replicas"] == [{"Region": {"Ref": "AWS::Region"}}]


def test_the_budget_table_is_billed_per_request(stack: AndurStack, template: Template) -> None:
    """A public URL has no rate to provision for, and an idle month must cost nothing."""
    assert budget_table(stack, template)["Properties"]["BillingMode"] == "PAY_PER_REQUEST"


def test_the_budget_table_clears_old_items_by_itself(stack: AndurStack, template: Template) -> None:
    """On the attribute the handler writes — a TTL on any other name would never fire."""
    assert budget_table(stack, template)["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": BUDGET_TTL_ATTRIBUTE,
        "Enabled": True,
    }
    assert f"{BUDGET_TTL_ATTRIBUTE} = " in handler.BUDGET_UPDATE_EXPRESSION


def test_the_table_and_the_handler_agree_on_the_attribute_names() -> None:
    """The stack creates the table, the handler writes the items; a typo would only show in AWS."""
    assert handler.BUDGET_KEY_ATTRIBUTE == BUDGET_KEY_ATTRIBUTE
    assert handler.BUDGET_TTL_ATTRIBUTE == BUDGET_TTL_ATTRIBUTE


def test_the_defaults_the_stack_hands_over_are_a_budget_the_handler_accepts() -> None:
    """The handler refuses an item that would go before its own window closes."""
    handler.load_settings(
        {
            "KNOWLEDGE_BASE_ID": "KB1",
            "MODEL_ID": ANSWER_MODEL_ID,
            "REGION": "us-east-1",
            "NUMBER_OF_RESULTS": str(NUMBER_OF_RESULTS),
            "API_KEY_PARAMETER_PATH": API_KEY_PARAMETER_PATH,
            "BUDGET_TABLE_NAME": BUDGET_TABLE_NAME,
            "QUESTION_LIMIT": str(DEFAULT_QUESTION_LIMIT),
            "WINDOW_HOURS": str(DEFAULT_WINDOW_HOURS),
            "KEEP_DAYS": str(DEFAULT_KEEP_DAYS),
        }
    )
    assert (DEFAULT_QUESTION_LIMIT, DEFAULT_WINDOW_HOURS) == (10, 48)


def test_no_role_may_do_more_than_count_on_the_budget_table(template: Template) -> None:
    """One action, on one table. Reading or writing items freely is nobody's job here."""
    actions = [
        statement["Action"]
        for _, statement in all_policy_statements(template)
        if "dynamodb" in json.dumps(statement["Action"])
    ]
    assert actions == ["dynamodb:UpdateItem"]


# --- Workspace keys ------------------------------------------------------


def test_one_key_per_workspace_is_generated_during_the_deploy(template: Template) -> None:
    keys = template.find_resources("Custom::AndurApiKey")
    names = sorted(key["Properties"]["ParameterName"] for key in keys.values())
    assert names == sorted(f"{API_KEY_PARAMETER_PATH}/{workspace}" for workspace in WORKSPACES)
    for key in keys.values():
        assert set(key["Properties"]) == {"ServiceToken", "ServiceTimeout", "ParameterName", "ParameterPath"}


def test_no_key_value_is_in_the_template(template: Template) -> None:
    """CloudFormation cannot hold a SecureString; a plain parameter would put the key into cdk.out."""
    template.resource_count_is("AWS::SSM::Parameter", 0)
    template.resource_count_is("AWS::SecretsManager::Secret", 0)


def test_key_function_may_only_write_keys_under_the_path(stack: AndurStack, template: Template) -> None:
    policies = role_policies(stack, template, stack.node.find_child("ApiKeyFunctionRole"))
    assert set(policies) == {"ManageApiKeys", "WriteLogs"}
    (statement,) = policies["ManageApiKeys"]["Statement"]
    assert sorted(statement["Action"]) == ["ssm:DeleteParameter", "ssm:PutParameter"]
    assert joined_last_part(statement["Resource"]) == f":parameter{API_KEY_PARAMETER_PATH}/*"


# --- Logs ----------------------------------------------------------------


def test_every_log_group_has_a_retention_and_goes_with_the_stack(template: Template) -> None:
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(log_groups) == 4
    for log_group in log_groups.values():
        assert log_group["Properties"]["RetentionInDays"] == 30
        assert log_group["DeletionPolicy"] == "Delete"


def test_every_function_logs_into_a_log_group_of_the_stack(template: Template) -> None:
    """Otherwise Lambda creates one on first use: no retention, and it outlives cdk destroy."""
    log_group_ids = set(template.find_resources("AWS::Logs::LogGroup"))
    for function in template.find_resources("AWS::Lambda::Function").values():
        assert function["Properties"]["LoggingConfig"]["LogGroup"]["Ref"] in log_group_ids


def test_answer_log_group_has_the_name_lambda_would_give_it(stack: AndurStack, template: Template) -> None:
    properties = resource_of(stack, template, stack.answer_log_group)["Properties"]
    assert properties["LogGroupName"] == f"/aws/lambda/{ANSWER_FUNCTION_NAME}"

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

import aws  # app/aws.py, imported by its Lambda name (tests/conftest.py)

# Imported, not skipped past. `aws-cdk-lib` is a hard dependency in
# pyproject.toml, and it has to stay one: with an `importorskip` here, an
# environment without the CDK reported "passed" for this whole module — over
# sixty assertions about the stack, IAM, CloudFront and the page gone without
# a red line anywhere. A missing dependency is a broken environment and has
# to look like one.
import aws_cdk as cdk
import handler  # app/handler.py, likewise
import pytest
from aws_cdk.assertions import Match, Template

from infra.answer_models import answer_model
from infra.stacks.andur_stack import (
    ANSWER_FUNCTION_NAME,
    ANSWER_GUARDRAIL_READS_QUESTION_TOPICS,
    ANSWER_MAX_CONCURRENCY,
    ANSWER_MEMORY_MB,
    ANSWER_MODEL_ID,
    ANSWER_TIMEOUT_SECONDS,
    API_KEY_PARAMETER_PATH,
    ASK_PATH,
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    BUDGET_KEY_ATTRIBUTE,
    BUDGET_TABLE_NAME,
    BUDGET_TTL_ATTRIBUTE,
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_PERCENT,
    DATA_SOURCE_NAME,
    DEFAULT_KEEP_DAYS,
    DEFAULT_QUESTION_LIMIT,
    DEFAULT_WINDOW_HOURS,
    DENIED_TOPICS,
    DISTANCE_METRIC,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_ID,
    EXCERPT_GUARDRAIL_NAME,
    GROUNDING_THRESHOLD,
    GUARDRAIL_NAME,
    GUARDRAIL_PROFILE_ID,
    GUARDRAIL_PROFILE_REGIONS,
    HSTS_MAX_AGE_DAYS,
    KNOWLEDGE_BASE_NAME,
    MASKED_PII_TYPES,
    NON_FILTERABLE_METADATA_KEYS,
    NONCURRENT_VERSION_RETENTION_DAYS,
    NUMBER_OF_RESULTS,
    ORIGIN_RESPONSE_TIMEOUT_SECONDS,
    PRICE_DATE,
    PRICE_GROUNDING_PER_1K_UNITS,
    PRICE_INPUT_PER_1K_TOKENS,
    PRICE_OUTPUT_PER_1K_TOKENS,
    PRICE_PII_PER_1K_UNITS,
    PRICE_TOPIC_PER_1K_UNITS,
    RELEVANCE_THRESHOLD,
    REPOSITORY_ROOT,
    SITE_ROOT_DOCUMENT,
    TOPIC_GUARDRAIL_NAME,
    TOPICS_TIER,
    VECTOR_DATA_TYPE,
    VECTOR_INDEX_NAME,
    WEB_FOLDER,
    WORKSPACE_PREFIX,
    AndurStack,
    replacement_safe_name,
)
from scripts import ask, grounding_probe, topic_probe

#: The file `cdk deploy` reads its context from. The tests read the same file,
#: so that what is synthesised here is what would be deployed.
CDK_JSON = REPOSITORY_ROOT / "cdk.json"


def deploy_context() -> dict[str, Any]:
    """The context of cdk.json — the feature flags a deploy renders with.

    A flag can change the template: ``@aws-cdk/aws-iam:minimizePolicies``
    merges policy statements, and this module unpacks statements by position
    (``list_bucket, get_object = ...``). Synthesised without the context, the
    tests would be reading a template nobody deploys.
    """
    return json.loads(CDK_JSON.read_text(encoding="utf-8"))["context"]


@pytest.fixture(scope="module")
def stack() -> AndurStack:
    app = cdk.App(context=deploy_context())
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


def profile_arn(region: Any) -> dict[str, Any]:
    """The EU guardrail profile in ``region`` as CloudFormation renders it."""
    middle = [":bedrock:", region, ":"] if isinstance(region, dict) else [f":bedrock:{region}:"]
    return {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                *middle,
                {"Ref": "AWS::AccountId"},
                f":guardrail-profile/{GUARDRAIL_PROFILE_ID}",
            ],
        ]
    }


# --- Documents bucket ----------------------------------------------------


def documents_bucket(stack: AndurStack, template: Template) -> Mapping[str, Any]:
    """The documents bucket itself, not whichever bucket happens to match.

    The stack has two: the documents bucket and the private site bucket.
    ``has_resource_properties`` is happy as soon as one resource of the type
    fits, so with it these three promises were kept by the site bucket while
    the documents bucket could have lost all of them — and the documents are
    the one thing here that cannot be rebuilt from anything else.
    """
    return resource_of(stack, template, stack.documents_bucket)["Properties"]


def test_documents_bucket_blocks_all_public_access(stack: AndurStack, template: Template) -> None:
    assert documents_bucket(stack, template)["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_documents_bucket_is_versioned(stack: AndurStack, template: Template) -> None:
    assert documents_bucket(stack, template)["VersioningConfiguration"] == {"Status": "Enabled"}


def test_old_document_versions_expire(stack: AndurStack, template: Template) -> None:
    """Versioning without an expiry would keep every old version forever."""
    (rule,) = documents_bucket(stack, template)["LifecycleConfiguration"]["Rules"]
    assert rule["Status"] == "Enabled"
    assert rule["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": NONCURRENT_VERSION_RETENTION_DAYS
    }


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
    settings = [
        VECTOR_DATA_TYPE,
        EMBEDDING_DIMENSION,
        DISTANCE_METRIC,
        NON_FILTERABLE_METADATA_KEYS,
    ]
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
    assert data_source["KnowledgeBaseId"] == get_att(stack, stack.knowledge_base, "KnowledgeBaseId")


# --- Data source ---------------------------------------------------------


def test_data_source_crawls_only_the_workspace_prefix(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "DataSourceConfiguration": Match.object_like(
                {
                    "Type": "S3",
                    "S3Configuration": Match.object_like({"InclusionPrefixes": [WORKSPACE_PREFIX]}),
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


def test_role_may_only_read_our_own_documents_bucket(stack: AndurStack, template: Template) -> None:
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
            statements += [
                (logical_id, statement) for statement in policy["PolicyDocument"]["Statement"]
            ]
    for logical_id, policy in template.find_resources("AWS::IAM::Policy").items():
        statements += [
            (logical_id, statement)
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        ]
    return statements


def test_the_only_wildcard_resources_are_model_listing_and_cache_clearing(
    template: Template,
) -> None:
    """Two actions that cannot be scoped; nothing else gets to use that excuse.

    ``bedrock:ListFoundationModels`` takes no resource at all. CloudFront
    invalidation is CDK's: the file copier clears the caches after an upload,
    and the role that does it is built by the construct, which grants the two
    invalidation actions on ``*``. Narrowing that would mean replacing the
    construct. Nothing in this stack's own roles is on a wildcard.
    """
    wildcard_actions = [
        sorted(action) if isinstance(action, list) else [action]
        for action in (
            statement["Action"]
            for _, statement in all_policy_statements(template)
            if statement["Resource"] == "*"
        )
    ]
    # Sorted, because the order inside one statement is the renderer's:
    # ``@aws-cdk/aws-iam:minimizePolicies`` in cdk.json merges statements and
    # decides for itself in which order the actions end up.
    assert sorted(wildcard_actions) == [
        ["bedrock:ListFoundationModels"],
        ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"],
    ]


def test_only_cdk_s_own_deploy_time_roles_have_a_managed_policy(
    stack: AndurStack, template: Template
) -> None:
    """Known exceptions: all three belong to functions CDK generates for a deploy.

    One answers the Delete of the retired key resources, one runs the deploy
    drain, the third copies web/ into the site bucket.
    Each gets AWSLambdaBasicExecutionRole from its construct; this stack does
    not create these roles and cannot narrow them without replacing the
    constructs. Every role the stack defines itself carries inline policies
    only, and all three of these run during a deploy and never afterwards.
    """
    provider_roles = [
        stack.node.find_child(provider).node.find_child("framework-onEvent").role
        for provider in ("ApiKeyProvider", "DeployDrainProvider")
    ]
    copier_role = stack.site_files.handler_role
    expected = {
        stack.get_logical_id(role.node.default_child) for role in [*provider_roles, copier_role]
    }
    with_managed = {
        logical_id: role["Properties"]["ManagedPolicyArns"]
        for logical_id, role in template.find_resources("AWS::IAM::Role").items()
        if "ManagedPolicyArns" in role["Properties"]
    }
    assert set(with_managed) == expected
    for managed_arns in with_managed.values():
        (managed_arn,) = managed_arns
        assert (
            joined_last_part(managed_arn)
            == ":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        )


def test_role_may_only_touch_our_own_vector_index(stack: AndurStack, template: Template) -> None:
    """No wildcard on s3vectors — one index, five actions."""
    (statement,) = role_policies(stack, template, stack.knowledge_base_role)["UseVectorIndex"][
        "Statement"
    ]
    assert sorted(statement["Action"]) == [
        "s3vectors:DeleteVectors",
        "s3vectors:GetIndex",
        "s3vectors:GetVectors",
        "s3vectors:PutVectors",
        "s3vectors:QueryVectors",
    ]
    assert statement["Resource"] == {"Fn::GetAtt": ["VectorIndex", "IndexArn"]}


def test_role_can_only_be_assumed_by_bedrock_in_this_account(
    stack: AndurStack, template: Template
) -> None:
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
    """Six functions, and five of them only ever run during a deploy.

    The answer function, the retired key function and the deploy drain, each with
    the CDK framework that runs it, and CDK's file copier that puts web/ into the
    site bucket. Nothing else runs, so nothing else can cost money while nobody
    asks.
    """
    template.resource_count_is("AWS::Lambda::Function", 6)
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


def test_answer_function_runs_the_handler_on_the_newest_runtime(
    stack: AndurStack, template: Template
) -> None:
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
    innermost number counts the key once, because only the requested
    workspace's key is read, at most once per request, and the budget table
    twice, because a wrong item is put right with a second write, and the guardrail
    three times, for the topic checks of the question and the answer and for the
    excerpts.
    """
    assert aws.worst_case_seconds() == 189
    assert aws.worst_case_seconds() < ANSWER_TIMEOUT_SECONDS < ask.HTTP_TIMEOUT_SECONDS < 900


def test_cloudfront_gives_up_before_the_function_does() -> None:
    """The one layer that does not nest, and knowingly so.

    CloudFront may wait 120 seconds without an approved quota increase, and
    the function may run 195. An answer between the two reaches the browser as
    CloudFront's 504 while the function runs it to the end and charges the
    question. An answer takes seconds (0.7 to 4.4 inside the function with Nova
    Pro on 28.09.2026), so the gap is a corner, not a case — and the
    alternative, a shorter function timeout, would cut off answers that are
    merely slow.
    """
    assert ORIGIN_RESPONSE_TIMEOUT_SECONDS < ANSWER_TIMEOUT_SECONDS
    assert aws.ORIGIN_TIMEOUT_SECONDS == ORIGIN_RESPONSE_TIMEOUT_SECONDS
    assert ORIGIN_RESPONSE_TIMEOUT_SECONDS < aws.worst_case_seconds()


def test_answer_function_runs_at_most_a_few_requests_at_once(
    stack: AndurStack, template: Template
) -> None:
    """The hard cap on Lambda time and model calls behind an address anyone may find."""
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert properties["ReservedConcurrentExecutions"] == ANSWER_MAX_CONCURRENCY == 5


def test_answer_function_is_configured_only_through_its_environment(
    stack: AndurStack, template: Template
) -> None:
    variables = resource_of(stack, template, stack.answer_function)["Properties"]["Environment"][
        "Variables"
    ]
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
        "GUARDRAIL_ID": get_att(stack, stack.guardrail_version, "GuardrailId"),
        "GUARDRAIL_VERSION": get_att(stack, stack.guardrail_version, "Version"),
        "EXCERPT_GUARDRAIL_ID": get_att(stack, stack.excerpt_guardrail_version, "GuardrailId"),
        "EXCERPT_GUARDRAIL_VERSION": get_att(stack, stack.excerpt_guardrail_version, "Version"),
        "TOPIC_GUARDRAIL_ID": get_att(stack, stack.topic_guardrail_version, "GuardrailId"),
        "TOPIC_GUARDRAIL_VERSION": get_att(stack, stack.topic_guardrail_version, "Version"),
        "PRICE_INPUT_PER_1K_TOKENS": PRICE_INPUT_PER_1K_TOKENS,
        "PRICE_OUTPUT_PER_1K_TOKENS": PRICE_OUTPUT_PER_1K_TOKENS,
        "PRICE_TOPIC_PER_1K_UNITS": PRICE_TOPIC_PER_1K_UNITS,
        "PRICE_PII_PER_1K_UNITS": PRICE_PII_PER_1K_UNITS,
        "PRICE_GROUNDING_PER_1K_UNITS": PRICE_GROUNDING_PER_1K_UNITS,
        "PRICE_DATE": PRICE_DATE,
    }
    assert set(variables) == set(handler.ENVIRONMENT_VARIABLES.values())


def test_the_model_id_is_configuration_not_handler_code() -> None:
    for module in (REPOSITORY_ROOT / "app").glob("*.py"):
        assert ANSWER_MODEL_ID not in module.read_text(encoding="utf-8"), module.name


def test_answer_role_has_five_policies_on_named_resources(
    stack: AndurStack, template: Template
) -> None:
    policies = role_policies(stack, template, stack.answer_role)
    assert set(policies) == {
        "ReadApiKeys",
        "CountQuestionsPerKey",
        "RetrieveFromKnowledgeBase",
        "InvokeAnswerModel",
        "WriteLogs",
    }

    (read_keys,) = policies["ReadApiKeys"]["Statement"]
    assert read_keys["Action"] == "ssm:GetParameter"
    assert joined_last_part(read_keys["Resource"]) == f":parameter{API_KEY_PARAMETER_PATH}/*"

    (count,) = policies["CountQuestionsPerKey"]["Statement"]
    assert count["Action"] == "dynamodb:UpdateItem"
    assert count["Resource"] == get_att(stack, stack.budget_table.node.default_child, "Arn")

    (retrieve,) = policies["RetrieveFromKnowledgeBase"]["Statement"]
    assert retrieve["Action"] == "bedrock:Retrieve"
    assert retrieve["Resource"] == get_att(stack, stack.knowledge_base, "KnowledgeBaseArn")

    # The default model is an EU profile: the profile needs the guardrail, the
    # model behind it needs the profile.
    profile, model, apply_guardrail = policies["InvokeAnswerModel"]["Statement"]
    assert profile["Action"] == model["Action"] == "bedrock:InvokeModel"
    assert joined_last_part(profile["Resource"]) == f":inference-profile/{ANSWER_MODEL_ID}"
    assert model["Resource"] == {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":bedrock:eu-*::foundation-model/" + ANSWER_MODEL_ID.removeprefix("eu."),
            ],
        ]
    }
    assert model["Condition"] == {
        "StringEquals": {"bedrock:InferenceProfileArn": profile["Resource"]}
    }
    # Only through a numbered version of the answer guardrail: <guardrail arn>:<number>, never
    # the draft. What that allows and refuses, step by step through a deploy:
    # tests/test_deploy_steps.py.
    assert profile["Condition"] == guarded_by(stack)
    # The answer and topic guardrails run through the EU profile: the profile in every
    # region it routes to, and in none outside the EU.
    # The CDK sorts the resources of a statement; compared without order.
    assert apply_guardrail["Action"] == "bedrock:ApplyGuardrail"
    assert sorted(map(json.dumps, apply_guardrail["Resource"])) == sorted(
        map(
            json.dumps,
            [
                get_att(stack, stack.guardrail, "GuardrailArn"),
                get_att(stack, stack.excerpt_guardrail, "GuardrailArn"),
                get_att(stack, stack.topic_guardrail, "GuardrailArn"),
                *(profile_arn(region) for region in GUARDRAIL_PROFILE_REGIONS),
            ],
        )
    )

    (write_logs,) = policies["WriteLogs"]["Statement"]
    assert sorted(write_logs["Action"]) == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert write_logs["Resource"] == get_att(
        stack, stack.answer_log_group.node.default_child, "Arn"
    )


REGIONAL_MODEL_ID = "nvidia.nemotron-super-3-120b"


def guarded_by(stack: AndurStack) -> dict[str, Any]:
    """The condition on every statement that opens the answer model."""
    return {
        "ArnLike": {
            "bedrock:GuardrailIdentifier": {
                "Fn::Join": ["", [get_att(stack, stack.guardrail, "GuardrailArn"), ":*"]]
            }
        },
        "StringNotLike": {"bedrock:GuardrailIdentifier": "*:DRAFT"},
    }


def test_a_model_in_the_region_is_invoked_directly_with_its_own_prices() -> None:
    app = cdk.App(context=deploy_context())
    stack = AndurStack(app, "RegionalModelStack", answer_model_id=REGIONAL_MODEL_ID)
    template = Template.from_stack(stack)
    policies = role_policies(stack, template, stack.answer_role)
    invoke, apply_guardrail = policies["InvokeAnswerModel"]["Statement"]

    assert invoke["Action"] == "bedrock:InvokeModel"
    assert joined_last_part(invoke["Resource"]) == f"::foundation-model/{REGIONAL_MODEL_ID}"
    assert invoke["Condition"] == guarded_by(stack)
    assert apply_guardrail["Action"] == "bedrock:ApplyGuardrail"
    variables = resource_of(stack, template, stack.answer_function)["Properties"]["Environment"]
    assert variables["Variables"]["MODEL_ID"] == REGIONAL_MODEL_ID
    assert variables["Variables"]["PRICE_INPUT_PER_1K_TOKENS"] == "0.00018"
    assert variables["Variables"]["PRICE_OUTPUT_PER_1K_TOKENS"] == "0.00078"


def test_a_model_without_a_price_does_not_synthesise() -> None:
    app = cdk.App(context=deploy_context())
    with pytest.raises(KeyError, match="has no price"):
        AndurStack(app, "UnpricedModelStack", answer_model_id="eu.unpriced-model-v1:0")


def test_the_default_model_carries_its_own_prices() -> None:
    row = answer_model(ANSWER_MODEL_ID)
    assert (PRICE_INPUT_PER_1K_TOKENS, PRICE_OUTPUT_PER_1K_TOKENS) == (
        row.input_per_1k,
        row.output_per_1k,
    )
    assert (row.name, row.input_per_1k, row.output_per_1k) == ("Nova Pro", "0.00092", "0.00368")


def test_account_wide_names_carry_the_region(stack: AndurStack, template: Template) -> None:
    """IAM and CloudFront names hold for the whole account: without the region,
    a second stack in another region collides with the first."""
    resources = template.to_json()["Resources"].values()
    names = [
        resource_of(stack, template, stack.knowledge_base_role)["Properties"]["RoleName"],
        *(
            r["Properties"]["ResponseHeadersPolicyConfig"]["Name"]
            for r in resources
            if r["Type"] == "AWS::CloudFront::ResponseHeadersPolicy"
        ),
        *(
            r["Properties"]["OriginAccessControlConfig"]["Name"]
            for r in resources
            if r["Type"] == "AWS::CloudFront::OriginAccessControl"
        ),
    ]
    assert len(names) == 4
    for name in names:
        assert {"Ref": "AWS::Region"} in name["Fn::Join"][1], name


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


def test_exactly_one_output_is_a_function_url(stack: AndurStack, template: Template) -> None:
    url_id = stack.get_logical_id(stack.function_url.node.default_child)
    url_outputs = [
        name
        for name, output in template.find_outputs("*").items()
        if url_id in json.dumps(output["Value"])
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

    function_origin = [
        o for o in distribution_config(stack, template)["Origins"] if "CustomOriginConfig" in o
    ]
    (origin,) = function_origin
    assert behavior["TargetOriginId"] == origin["Id"]
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"


def test_cloudfront_waits_as_long_as_an_account_may_without_asking(
    stack: AndurStack, template: Template
) -> None:
    """The response-timeout quota is 1-120 seconds; more needs an approved increase."""
    (origin,) = [
        o for o in distribution_config(stack, template)["Origins"] if "CustomOriginConfig" in o
    ]
    assert (
        origin["CustomOriginConfig"]["OriginReadTimeout"] == ORIGIN_RESPONSE_TIMEOUT_SECONDS == 120
    )


def security_headers(template: Template) -> Mapping[str, Any]:
    """The one response headers policy, as CloudFormation would receive it."""
    policy = only_resource(template, "AWS::CloudFront::ResponseHeadersPolicy")
    return policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]


def test_both_behaviours_send_the_security_headers(stack: AndurStack, template: Template) -> None:
    """The page and the answer, one policy — the distribution is the only free place for it."""
    config = distribution_config(stack, template)
    headers = stack.node.find_child("SecurityHeaders")
    policy_id = {"Ref": stack.get_logical_id(headers.node.default_child)}
    assert config["DefaultCacheBehavior"]["ResponseHeadersPolicyId"] == policy_id
    for behavior in config["CacheBehaviors"]:
        assert behavior["ResponseHeadersPolicyId"] == policy_id


def test_the_page_says_what_it_may_load_and_who_may_frame_it(template: Template) -> None:
    """A page that keeps a key in localStorage may not be framed and may not load strangers.

    ``default-src 'none'`` is the floor and every source below it is the
    page's own origin, so a script or a stylesheet added from anywhere else
    does not run. ``frame-ancestors 'none'`` keeps the page out of somebody
    else's frame, where the key field would be theirs to read.
    """
    policy = security_headers(template)["ContentSecurityPolicy"]
    assert policy["Override"] is True
    directives = dict(part.split(" ", 1) for part in policy["ContentSecurityPolicy"].split("; "))
    assert directives["default-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["script-src"] == directives["style-src"] == "'self'"
    assert directives["connect-src"] == "'self'"


def test_the_address_may_only_be_reached_over_https_and_not_be_sniffed(
    template: Template,
) -> None:
    headers = security_headers(template)
    assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] == (
        HSTS_MAX_AGE_DAYS * 24 * 3600
    )
    assert headers["StrictTransportSecurity"]["IncludeSubdomains"] is True
    assert headers["ContentTypeOptions"] == {"Override": True}
    assert headers["ReferrerPolicy"]["ReferrerPolicy"] == "no-referrer"
    assert headers["FrameOptions"]["FrameOption"] == "DENY"


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


def page_constant(name: str) -> str:
    """One ``const NAME = "value";`` out of the page's script.

    The page cannot import from Python, so it spells these out. What it
    spells out is what the tests below read back.
    """
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    (value,) = re.findall(rf'^const {name} = "([^"]*)";$', script, re.MULTILINE)
    return value


def page_number(name: str) -> int:
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    (value,) = re.findall(rf"^const {name} = (\d+);$", script, re.MULTILINE)
    return int(value)


def test_the_page_and_the_stack_mean_the_same_path_and_the_same_headers() -> None:
    """A typo here is a green suite and a page that reaches the wrong origin.

    The page posts to `ASK_PATH`; CloudFront sends exactly that one path to
    the function and everything else to the bucket. Move the path in the
    stack alone and the page's POST falls onto the default behaviour: S3
    answers 403, CloudFront hands back HTML, and the page — which can only
    read what it gets — tells the reader something about their key. The two
    headers are the same kind of agreement one layer further in.
    """
    assert page_constant("ASK_PATH") == ASK_PATH
    assert page_constant("KEY_HEADER") == handler.KEY_HEADER
    assert page_constant("BODY_HASH_HEADER") == ask.BODY_HASH_HEADER
    assert page_number("MAX_QUESTION_CHARACTERS") == handler.MAX_QUESTION_CHARACTERS


def test_the_page_asks_once_at_a_time() -> None:
    """Enter submits the form directly, and a disabled button does not stop it.

    A second question while the first is in flight is a second question off
    the same ten, two of the five Lambda places, and an answer that may not
    belong to the question in the field. The guard is a flag the submit
    handler reads before anything else.
    """
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    assert "let asking = false;" in script
    assert "if (asking) {" in script


def test_the_page_stops_waiting_by_itself() -> None:
    """Without this a hung connection leaves the button on "Asking…" forever.

    The page waits a little longer than CloudFront does, so in every ordinary
    case CloudFront's 504 arrives first and says more. This is the floor
    under it: an abort, a cleared timer, and a sentence.
    """
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    assert "new AbortController()" in script
    assert "signal: controller.signal" in script
    assert "clearTimeout(giveUp)" in script
    assert "AbortError" in script
    # Longer than the distribution waits, so the 504 is the answer that wins.
    assert page_number("REQUEST_TIMEOUT_MS") > ORIGIN_RESPONSE_TIMEOUT_SECONDS * 1000


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
            "GUARDRAIL_ID": "gr1",
            "GUARDRAIL_VERSION": "1",
            "EXCERPT_GUARDRAIL_ID": "gr2",
            "EXCERPT_GUARDRAIL_VERSION": "1",
            "TOPIC_GUARDRAIL_ID": "gr3",
            "TOPIC_GUARDRAIL_VERSION": "1",
            "PRICE_INPUT_PER_1K_TOKENS": PRICE_INPUT_PER_1K_TOKENS,
            "PRICE_OUTPUT_PER_1K_TOKENS": PRICE_OUTPUT_PER_1K_TOKENS,
            "PRICE_TOPIC_PER_1K_UNITS": PRICE_TOPIC_PER_1K_UNITS,
            "PRICE_PII_PER_1K_UNITS": PRICE_PII_PER_1K_UNITS,
            "PRICE_GROUNDING_PER_1K_UNITS": PRICE_GROUNDING_PER_1K_UNITS,
            "PRICE_DATE": PRICE_DATE,
        }
    )
    assert (DEFAULT_QUESTION_LIMIT, DEFAULT_WINDOW_HOURS) == (10, 48)


# --- Guardrail -----------------------------------------------------------


def test_three_guardrails_each_with_one_version(stack: AndurStack, template: Template) -> None:
    template.resource_count_is("AWS::Bedrock::Guardrail", 3)
    template.resource_count_is("AWS::Bedrock::GuardrailVersion", 3)
    for guardrail, version, name in (
        (stack.guardrail, stack.guardrail_version, GUARDRAIL_NAME),
        (stack.excerpt_guardrail, stack.excerpt_guardrail_version, EXCERPT_GUARDRAIL_NAME),
        (stack.topic_guardrail, stack.topic_guardrail_version, TOPIC_GUARDRAIL_NAME),
    ):
        assert resource_of(stack, template, guardrail)["Properties"]["Name"] == name
        assert resource_of(stack, template, version)["Properties"][
            "GuardrailIdentifier"
        ] == get_att(stack, guardrail, "GuardrailId")


def test_the_excerpt_guardrail_masks_the_same_details_and_does_nothing_else(
    stack: AndurStack, template: Template
) -> None:
    """No topic, no grounding: an excerpt about VAT is no tax question, and one price only."""
    properties = resource_of(stack, template, stack.excerpt_guardrail)["Properties"]
    assert "TopicPolicyConfig" not in properties
    assert "ContextualGroundingPolicyConfig" not in properties
    assert "ContentPolicyConfig" not in properties and "WordPolicyConfig" not in properties
    entities = properties["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    answer_entities = resource_of(stack, template, stack.guardrail)["Properties"][
        "SensitiveInformationPolicyConfig"
    ]["PiiEntitiesConfig"]
    assert entities == answer_entities
    assert 1 <= len(properties["BlockedOutputsMessaging"]) <= 500


@pytest.mark.parametrize(("which", "on_answer"), [("guardrail", False), ("topic_guardrail", True)])
def test_the_four_topics_are_refused_in_the_question_and_in_the_answer_without_citations(
    stack: AndurStack, template: Template, which: str, on_answer: bool
) -> None:
    """The topic guardrail reads all four in the question, after its frame, and in the answer
    without its citations. The answer guardrail on the model call reads them in the question
    while `ANSWER_GUARDRAIL_READS_QUESTION_TOPICS`, never in the answer: in the answer as the
    model writes it, `Private lives` withheld short harmless answers for their citations [1][4]
    or [1][2][4], among them q15 and de11 (04.10.2026)."""
    assert ANSWER_GUARDRAIL_READS_QUESTION_TOPICS is True
    properties = resource_of(stack, template, getattr(stack, which))["Properties"]
    topics = properties["TopicPolicyConfig"]["TopicsConfig"]
    assert [topic["Name"] for topic in topics] == [
        "Legal action",
        "Tax returns",
        "Medical advice",
        "Private lives",
    ]
    for topic in topics:
        assert topic["Type"] == "DENY"
        assert (topic["InputEnabled"], topic["InputAction"]) == (True, "BLOCK")
        assert (topic["OutputEnabled"], topic["OutputAction"]) == (
            (True, "BLOCK") if on_answer else (False, "NONE")
        )
    assert properties["TopicPolicyConfig"]["TopicsTierConfig"] == {"TierName": TOPICS_TIER}


def test_after_the_second_deploy_the_answer_guardrail_reads_no_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off, the topic guardrail is the only one that reads the topics; masking,
    grounding and the profile stay on the answer guardrail, and both get a new version."""
    from infra.stacks import andur_stack

    def guardrails() -> dict[str, dict[str, Any]]:
        other = AndurStack(cdk.App(context=deploy_context()), "SecondDeployStack")
        resources = Template.from_stack(other).to_json()["Resources"]
        return {
            name: resources[other.get_logical_id(construct)]["Properties"]
            for name, construct in (
                ("answer", other.guardrail),
                ("topic", other.topic_guardrail),
                ("answer version", other.guardrail_version),
                ("topic version", other.topic_guardrail_version),
            )
        }

    first = guardrails()
    monkeypatch.setattr(andur_stack, "ANSWER_GUARDRAIL_READS_QUESTION_TOPICS", False)
    second = guardrails()
    assert "TopicPolicyConfig" not in second["answer"]
    assert {key for key in first["answer"] if key not in second["answer"]} == {"TopicPolicyConfig"}
    assert second["answer"]["CrossRegionConfig"] == first["answer"]["CrossRegionConfig"]
    assert second["topic"]["TopicPolicyConfig"] == first["topic"]["TopicPolicyConfig"]
    assert second["answer version"]["Description"] != first["answer version"]["Description"]
    assert second["topic version"]["Description"] != first["topic version"]["Description"]


def test_the_topic_guardrail_checks_topics_and_does_nothing_else(
    stack: AndurStack, template: Template
) -> None:
    """No masking and no grounding: those stay on the model call, where they read the answer
    with its citations as before. Its messages are the answer guardrail's."""
    properties = resource_of(stack, template, stack.topic_guardrail)["Properties"]
    answer = resource_of(stack, template, stack.guardrail)["Properties"]
    assert set(properties) == {
        "Name",
        "Description",
        "BlockedInputMessaging",
        "BlockedOutputsMessaging",
        "TopicPolicyConfig",
        "CrossRegionConfig",
    }
    assert properties["CrossRegionConfig"] == answer["CrossRegionConfig"]
    assert properties["BlockedInputMessaging"] == answer["BlockedInputMessaging"]
    assert properties["BlockedOutputsMessaging"] == answer["BlockedOutputsMessaging"]
    assert "SensitiveInformationPolicyConfig" in answer
    assert "ContextualGroundingPolicyConfig" in answer


def test_the_topic_probe_builds_the_topics_the_stack_deploys(
    stack: AndurStack, template: Template
) -> None:
    """The probe measures what a deploy would ship in the topic guardrail: the same topics in
    the same order, the same actions, tier and EU profile. Bedrock names the fields in
    camelCase, CloudFormation in PascalCase, and CloudFormation calls the tier
    `TopicsTierConfig`."""
    properties = resource_of(stack, template, stack.topic_guardrail)["Properties"]
    deployed = properties["TopicPolicyConfig"]
    probe = topic_probe.probe_request()
    assert [
        {key[0].upper() + key[1:]: value for key, value in topic.items()}
        for topic in probe["topicPolicyConfig"]["topicsConfig"]
    ] == deployed["TopicsConfig"]
    assert probe["topicPolicyConfig"]["tierConfig"] == {
        "tierName": deployed["TopicsTierConfig"]["TierName"]
    }
    assert probe["crossRegionConfig"] == {"guardrailProfileIdentifier": GUARDRAIL_PROFILE_ID}
    assert properties["CrossRegionConfig"] == {
        "GuardrailProfileArn": profile_arn({"Ref": "AWS::Region"})
    }
    assert probe["blockedInputMessaging"] == properties["BlockedInputMessaging"]
    assert probe["blockedOutputsMessaging"] == properties["BlockedOutputsMessaging"]
    assert probe["name"] == "andur-topic-probe" != properties["Name"]


def pascal_case(value: Any) -> Any:
    """Bedrock's camelCase field names as CloudFormation spells them, all the way down."""
    if isinstance(value, Mapping):
        return {key[0].upper() + key[1:]: pascal_case(item) for key, item in value.items()}
    if isinstance(value, list):
        return [pascal_case(item) for item in value]
    return value


def test_the_grounding_probe_copies_the_answer_guardrail(
    stack: AndurStack, template: Template
) -> None:
    """What the probe checks German answers with is what the function checks them with: the
    answer guardrail's topics on the same sides, its masking, its grounding thresholds, its
    profile and messages. CloudFormation calls the tier `TopicsTierConfig`."""
    properties = resource_of(stack, template, stack.guardrail)["Properties"]
    probe = grounding_probe.probe_request()
    topics = probe["topicPolicyConfig"]
    assert pascal_case(topics["topicsConfig"]) == properties["TopicPolicyConfig"]["TopicsConfig"]
    assert pascal_case(topics["tierConfig"]) == properties["TopicPolicyConfig"]["TopicsTierConfig"]
    for name in ("sensitiveInformationPolicyConfig", "contextualGroundingPolicyConfig"):
        assert pascal_case(probe[name]) == properties[name[0].upper() + name[1:]], name
    assert probe["crossRegionConfig"] == {"guardrailProfileIdentifier": GUARDRAIL_PROFILE_ID}
    assert properties["CrossRegionConfig"] == {
        "GuardrailProfileArn": profile_arn({"Ref": "AWS::Region"})
    }
    assert probe["blockedInputMessaging"] == properties["BlockedInputMessaging"]
    assert probe["blockedOutputsMessaging"] == properties["BlockedOutputsMessaging"]
    assert probe["name"] == "andur-grounding-probe" != properties["Name"]
    assert set(probe) - {"name", "description"} == {
        key[0].lower() + key[1:] for key in properties if key not in {"Name", "Description"}
    }


def test_after_the_second_deploy_the_grounding_probe_copies_the_answer_guardrail_without_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infra.stacks import andur_stack

    monkeypatch.setattr(andur_stack, "ANSWER_GUARDRAIL_READS_QUESTION_TOPICS", False)
    monkeypatch.setattr(grounding_probe, "ANSWER_GUARDRAIL_READS_QUESTION_TOPICS", False)
    other = AndurStack(cdk.App(context=deploy_context()), "SecondDeployProbeStack")
    properties = resource_of(other, Template.from_stack(other), other.guardrail)["Properties"]
    probe = grounding_probe.probe_request()
    assert "topicPolicyConfig" not in probe and "TopicPolicyConfig" not in properties
    assert set(probe) - {"name", "description"} == {
        key[0].lower() + key[1:] for key in properties if key not in {"Name", "Description"}
    }


def test_topic_definitions_follow_the_user_guides_best_practices() -> None:
    """ "Best practices for creating denied topics" in the Bedrock user guide: a definition
    describes a subject; it is no instruction ("Block all ...") and no negative or exception
    ("All contents except ..."). A name is a noun phrase that does not describe the topic."""
    instruction = re.compile(r"\b(block|deny|refuse|reject|do not answer)\b", re.IGNORECASE)
    exception = re.compile(r"\b(except|unless|other than|apart from|not)\b", re.IGNORECASE)
    for name, definition, examples in DENIED_TOPICS:
        assert not instruction.search(definition), name
        assert not exception.search(definition), name
        assert len(name.split()) <= 3, name
        assert not any(example in definition for example in examples), name
    assert len({name for name, _, _ in DENIED_TOPICS}) == len(DENIED_TOPICS)


def test_denied_topics_run_in_the_standard_tier_through_the_eu_profile(
    stack: AndurStack, template: Template
) -> None:
    """The Classic tier reads English, French and Spanish only. Measured 01.10.2026 against
    the deployed guardrail: 5 of 16 German refusals passed, among them a tax question. The
    Standard tier reads German and needs a guardrail profile; the EU one stays in the EU."""
    properties = resource_of(stack, template, stack.guardrail)["Properties"]
    assert TOPICS_TIER == "STANDARD"
    assert properties["TopicPolicyConfig"]["TopicsTierConfig"] == {"TierName": "STANDARD"}
    assert properties["CrossRegionConfig"] == {
        "GuardrailProfileArn": profile_arn({"Ref": "AWS::Region"})
    }
    excerpt = resource_of(stack, template, stack.excerpt_guardrail)["Properties"]
    assert "CrossRegionConfig" not in excerpt


def test_the_eu_guardrail_profile_routes_inside_the_eu_only() -> None:
    """The destination regions of eu.guardrail.v1:0 called from an EU region, as the user
    guide lists them: Frankfurt, Ireland, Paris, Stockholm, Milan, Spain."""
    assert GUARDRAIL_PROFILE_ID == "eu.guardrail.v1:0"
    assert GUARDRAIL_PROFILE_REGIONS == (
        "eu-central-1",
        "eu-west-1",
        "eu-west-3",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
    )


def test_the_pinned_region_synthesises_with_its_own_profile() -> None:
    app = cdk.App(context=deploy_context())
    stack = AndurStack(app, "IrelandStack", env=cdk.Environment(region="eu-west-1"))
    properties = resource_of(stack, Template.from_stack(stack), stack.guardrail)["Properties"]
    assert properties["CrossRegionConfig"] == {"GuardrailProfileArn": profile_arn("eu-west-1")}


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-2", "il-central-1"])
def test_a_stack_outside_the_profiles_regions_does_not_synthesise(region: str) -> None:
    """London has its own UK profile, and from Tel Aviv the EU profile also routes to
    Tel Aviv: neither keeps the guardrail's checks in the EU."""
    app = cdk.App(context=deploy_context())
    with pytest.raises(ValueError, match="not a source region"):
        AndurStack(app, "OutsideStack", env=cdk.Environment(region=region))


def test_topics_stay_inside_what_bedrock_accepts() -> None:
    """The Classic tier's limits, which the Standard tier's (definitions up to 1,000
    characters) contain: a definition of at most 200 characters, up to five examples of at
    most 100. Too long is no synth error; it is a failed deploy after several minutes.
    """
    name_pattern = re.compile(r"^[0-9a-zA-Z-_ !?.]+$")
    for name, definition, examples in DENIED_TOPICS:
        assert name_pattern.match(name) and len(name) <= 100, name
        assert 1 <= len(definition) <= 200, name
        assert 1 <= len(examples) <= 5, name
        assert all(1 <= len(example) <= 100 for example in examples), name
    assert 1 <= len(BLOCKED_INPUT_MESSAGE) <= 500
    assert 1 <= len(BLOCKED_OUTPUT_MESSAGE) <= 500


def test_the_handler_says_in_english_what_the_guardrail_says_and_in_german_the_same() -> None:
    """The handler replaces the guardrail's English message with its own sentence in the
    language of the request. The stack's message stays as the fallback, and the two must not
    drift apart: a visitor who asks in English reads the same words either way."""
    assert handler.QUESTION_REFUSED_ANSWERS["en"] == BLOCKED_INPUT_MESSAGE
    assert handler.ANSWER_WITHHELD_ANSWERS["en"] == BLOCKED_OUTPUT_MESSAGE
    assert 1 <= len(handler.QUESTION_REFUSED_ANSWERS["de"]) <= 500
    assert 1 <= len(handler.ANSWER_WITHHELD_ANSWERS["de"]) <= 500


def test_personal_details_are_masked_in_the_answer_only(
    stack: AndurStack, template: Template
) -> None:
    config = resource_of(stack, template, stack.guardrail)["Properties"][
        "SensitiveInformationPolicyConfig"
    ]
    entities = config["PiiEntitiesConfig"]
    assert (
        [entity["Type"] for entity in entities]
        == list(MASKED_PII_TYPES)
        == ["NAME", "EMAIL", "PHONE", "ADDRESS"]
    )
    for entity in entities:
        assert entity["Action"] == "ANONYMIZE"
        assert (entity["OutputEnabled"], entity["OutputAction"]) == (True, "ANONYMIZE")
        assert entity["InputEnabled"] is False


def test_grounding_and_relevance_are_checked_at_documented_thresholds(
    stack: AndurStack, template: Template
) -> None:
    config = resource_of(stack, template, stack.guardrail)["Properties"][
        "ContextualGroundingPolicyConfig"
    ]
    filters = {check["Type"]: check for check in config["FiltersConfig"]}
    assert set(filters) == {"GROUNDING", "RELEVANCE"}
    assert filters["GROUNDING"]["Threshold"] == GROUNDING_THRESHOLD == 0.7
    assert filters["RELEVANCE"]["Threshold"] == RELEVANCE_THRESHOLD == 0.7
    for check in filters.values():
        assert 0 <= check["Threshold"] <= 0.99  # 1 blocks everything, says the user guide
        assert (check["Action"], check["Enabled"]) == ("BLOCK", True)


def test_a_changed_setting_makes_a_new_guardrail_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The version's description is create-only, and it carries a digest of the settings.

    Without that, a new threshold would reach the guardrail's draft and never
    the version the function uses.
    """
    from infra.stacks import andur_stack

    def version_description() -> str:
        other = AndurStack(cdk.App(context=deploy_context()), "VersionStack")
        return Template.from_stack(other).to_json()["Resources"][
            other.get_logical_id(other.guardrail_version)
        ]["Properties"]["Description"]

    before = version_description()
    assert before == version_description()
    monkeypatch.setattr(andur_stack, "GROUNDING_THRESHOLD", 0.8)
    after = version_description()
    assert after != before
    assert len(after) <= 200


def test_a_changed_output_action_makes_new_versions_of_both_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest covers the built properties, not a summary of them.

    An output action was never in the summary the digest used to be taken
    over: switching masking to blocking changed the guardrail's draft and left
    both versions — the ones the function uses — as they were.
    """
    from aws_cdk import aws_bedrock as bedrock

    from infra.stacks import andur_stack

    def descriptions() -> tuple[str, str]:
        other = AndurStack(cdk.App(context=deploy_context()), "ActionStack")
        resources = Template.from_stack(other).to_json()["Resources"]
        return tuple(
            resources[other.get_logical_id(version)]["Properties"]["Description"]
            for version in (other.guardrail_version, other.excerpt_guardrail_version)
        )

    real_policy = andur_stack.masking_policy

    def blocking_policy() -> bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty:
        entities = [
            bedrock.CfnGuardrail.PiiEntityConfigProperty(
                type=entity.type,
                action=entity.action,
                input_enabled=entity.input_enabled,
                input_action=entity.input_action,
                output_enabled=entity.output_enabled,
                output_action="BLOCK",
            )
            for entity in real_policy().pii_entities_config
        ]
        return bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
            pii_entities_config=entities
        )

    answer_before, excerpt_before = descriptions()
    monkeypatch.setattr(andur_stack, "masking_policy", blocking_policy)
    answer_after, excerpt_after = descriptions()
    assert answer_after != answer_before
    assert excerpt_after != excerpt_before


def test_the_prices_are_ones_the_handler_accepts_and_dated() -> None:
    for value in (
        PRICE_INPUT_PER_1K_TOKENS,
        PRICE_OUTPUT_PER_1K_TOKENS,
        PRICE_TOPIC_PER_1K_UNITS,
        PRICE_PII_PER_1K_UNITS,
        PRICE_GROUNDING_PER_1K_UNITS,
    ):
        handler.price("PRICE", value)
    assert handler.price_day(PRICE_DATE) == PRICE_DATE


def test_the_guardrail_version_is_an_output(stack: AndurStack, template: Template) -> None:
    outputs = template.find_outputs("GuardrailVersion")
    (output,) = outputs.values()
    assert output["Value"] == {"Ref": stack.get_logical_id(stack.guardrail_version)}
    (excerpt_output,) = template.find_outputs("ExcerptGuardrailRef").values()
    assert excerpt_output["Value"] == {"Ref": stack.get_logical_id(stack.excerpt_guardrail_version)}
    (topic_output,) = template.find_outputs("TopicGuardrailRef").values()
    assert topic_output["Value"] == {"Ref": stack.get_logical_id(stack.topic_guardrail_version)}


def test_a_changed_topic_makes_new_versions_of_the_answer_and_the_topic_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both read `DENIED_TOPICS`, one on the question, one on the answer: a changed
    definition has to reach the versions the function uses, both of them."""
    from infra.stacks import andur_stack

    def descriptions() -> tuple[str, str]:
        other = AndurStack(cdk.App(context=deploy_context()), "TopicStack")
        resources = Template.from_stack(other).to_json()["Resources"]
        return tuple(
            resources[other.get_logical_id(version)]["Properties"]["Description"]
            for version in (other.guardrail_version, other.topic_guardrail_version)
        )

    answer_before, topic_before = descriptions()
    name, definition, examples = andur_stack.DENIED_TOPICS[0]
    changed = ((name, definition + " Changed.", examples), *andur_stack.DENIED_TOPICS[1:])
    monkeypatch.setattr(andur_stack, "DENIED_TOPICS", changed)
    answer_after, topic_after = descriptions()
    assert answer_after != answer_before
    assert topic_after != topic_before


def test_no_role_may_do_more_than_count_on_the_budget_table(template: Template) -> None:
    """One action, on one table. Reading or writing items freely is nobody's job here."""
    actions = [
        statement["Action"]
        for _, statement in all_policy_statements(template)
        if "dynamodb" in json.dumps(statement["Action"])
    ]
    assert actions == ["dynamodb:UpdateItem"]


# --- Workspace keys ------------------------------------------------------


#: The logical ids of the key function as deployed before 2026-10-01. The Delete of each
#: removed key resource reaches the function under these ids; a new id would make a new
#: function, and the old one, still able to delete keys, would answer that Delete.
DEPLOYED_KEY_FUNCTION_IDS = {
    "ApiKeyFunctionRoleC17A4D17": "AWS::IAM::Role",
    "ApiKeyFunction7C99575B": "AWS::Lambda::Function",
    "ApiKeyProviderframeworkonEvent9849A943": "AWS::Lambda::Function",
}


def test_the_stack_creates_no_workspace_key(template: Template) -> None:
    """Keys come from `python -m scripts.workspace add`; no customer is named in the code.
    The custom resources left are the deploy drain, which waits and touches nothing, and
    the copier that puts web/ into the site bucket."""
    custom = sorted(
        resource["Type"]
        for resource in template.to_json()["Resources"].values()
        if resource["Type"].startswith("Custom::")
    )
    assert custom == ["Custom::AndurDeployDrain", "Custom::CDKBucketDeployment"]


def test_the_key_function_is_updated_in_place_not_replaced(template: Template) -> None:
    resources = template.to_json()["Resources"]
    for logical_id, resource_type in DEPLOYED_KEY_FUNCTION_IDS.items():
        assert resources[logical_id]["Type"] == resource_type, logical_id


def test_no_role_may_write_or_delete_a_key(template: Template) -> None:
    """The deploy that drops the key resources runs their Delete with this stack's roles.

    None of them can delete a parameter any more, so not even an old copy of the key
    function could take a key with it. Reading keys stays with the answer function alone.
    """
    ssm_actions = sorted(
        action
        for _, statement in all_policy_statements(template)
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        if action.startswith("ssm:")
    )
    assert ssm_actions == ["ssm:GetParameter"]


def test_the_key_function_role_may_only_write_its_log(
    stack: AndurStack, template: Template
) -> None:
    policies = role_policies(stack, template, stack.node.find_child("ApiKeyFunctionRole"))
    assert set(policies) == {"WriteLogs"}


def test_the_key_function_runs_the_handler_that_deletes_nothing(
    stack: AndurStack, template: Template
) -> None:
    function = resource_of(stack, template, stack.node.find_child("ApiKeyFunction"))
    assert function["Properties"]["Handler"] == "handler.on_event"
    assert "deletes nothing" in function["Properties"]["Description"]


# --- Logs ----------------------------------------------------------------


def test_every_log_group_has_a_retention_and_goes_with_the_stack(template: Template) -> None:
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(log_groups) == 6
    for log_group in log_groups.values():
        assert log_group["Properties"]["RetentionInDays"] == 30
        assert log_group["DeletionPolicy"] == "Delete"


def test_every_function_logs_into_a_log_group_of_the_stack(template: Template) -> None:
    """Otherwise Lambda creates one on first use: no retention, and it outlives cdk destroy."""
    log_group_ids = set(template.find_resources("AWS::Logs::LogGroup"))
    for function in template.find_resources("AWS::Lambda::Function").values():
        assert function["Properties"]["LoggingConfig"]["LogGroup"]["Ref"] in log_group_ids


def test_answer_log_group_has_the_name_lambda_would_give_it(
    stack: AndurStack, template: Template
) -> None:
    properties = resource_of(stack, template, stack.answer_log_group)["Properties"]
    assert properties["LogGroupName"] == f"/aws/lambda/{ANSWER_FUNCTION_NAME}"

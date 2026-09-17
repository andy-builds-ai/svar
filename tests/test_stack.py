"""Assertions against the synthesised CloudFormation template.

These tests never call AWS. `Template.from_stack` renders the stack into the
JSON that CloudFormation would receive, and each test asserts one promise
that template has to keep. They catch a broken stack in a second, before a
deploy spends ten minutes finding out.
"""

import json
from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from app import handler
from infra.stacks.andur_stack import (
    ANSWER_FUNCTION_NAME,
    ANSWER_MEMORY_MB,
    ANSWER_MODEL_ID,
    ANSWER_TIMEOUT_SECONDS,
    API_KEY_PARAMETER_PATH,
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_PERCENT,
    DATA_SOURCE_NAME,
    DISTANCE_METRIC,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_ID,
    KNOWLEDGE_BASE_NAME,
    NON_FILTERABLE_METADATA_KEYS,
    NONCURRENT_VERSION_RETENTION_DAYS,
    NUMBER_OF_RESULTS,
    REPOSITORY_ROOT,
    VECTOR_DATA_TYPE,
    VECTOR_INDEX_NAME,
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


def test_documents_and_their_policy_survive_stack_deletion(template: Template) -> None:
    """Documents are the one thing that cannot be rebuilt — nor their SSL-only rule."""
    assert only_resource(template, "AWS::S3::Bucket")["DeletionPolicy"] == "Retain"
    assert only_resource(template, "AWS::S3::BucketPolicy")["DeletionPolicy"] == "Retain"


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


def test_the_only_wildcard_resource_is_model_listing(stack: AndurStack, template: Template) -> None:
    """Listing models cannot be scoped; nothing else gets to use that excuse."""
    wildcard_actions = [
        statement["Action"]
        for role in (stack.knowledge_base_role, stack.answer_role, stack.node.find_child("ApiKeyFunctionRole"))
        for document in role_policies(stack, template, role).values()
        for statement in document["Statement"]
        if statement["Resource"] == "*"
    ]
    assert wildcard_actions == ["bedrock:ListFoundationModels"]


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


def test_the_only_compute_is_the_answer_function_and_the_key_generator(
    stack: AndurStack, template: Template
) -> None:
    """Three functions: the answer function, the key generator, and the CDK framework that runs it.

    Nothing else runs, so nothing else can cost money while nobody asks.
    """
    template.resource_count_is("AWS::Lambda::Function", 3)
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
        "ApiKeyParameterPath",
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


def test_answer_timeout_covers_the_handlers_worst_case() -> None:
    """A hanging Bedrock call must end as the handler's 502, not as a Lambda timeout.

    And the whole call must fit into the 15 minutes a Function URL call may take.
    """
    assert handler.worst_case_seconds() < ANSWER_TIMEOUT_SECONDS < 900


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
    }
    assert set(variables) == set(handler.ENVIRONMENT_VARIABLES.values())


def test_the_model_id_is_configuration_not_handler_code() -> None:
    source = (REPOSITORY_ROOT / "app" / "handler.py").read_text(encoding="utf-8")
    assert ANSWER_MODEL_ID not in source


def test_answer_role_has_four_permissions_each_on_one_resource(stack: AndurStack, template: Template) -> None:
    policies = role_policies(stack, template, stack.answer_role)
    assert set(policies) == {"ReadApiKeys", "RetrieveFromKnowledgeBase", "InvokeAnswerModel", "WriteLogs"}

    (read_keys,) = policies["ReadApiKeys"]["Statement"]
    assert read_keys["Action"] == "ssm:GetParametersByPath"
    assert joined_last_part(read_keys["Resource"]) == f":parameter{API_KEY_PARAMETER_PATH}"

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


def test_function_url_is_open_only_behind_the_keys(stack: AndurStack, template: Template) -> None:
    """AuthType NONE, so the key check in the handler is the lock — and every key exists first."""
    template.resource_count_is("AWS::Lambda::Url", 1)
    url = resource_of(stack, template, stack.function_url)
    assert url["Properties"]["AuthType"] == "NONE"
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
    assert len(log_groups) == 3
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

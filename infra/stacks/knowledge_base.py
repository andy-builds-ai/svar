"""The documents bucket, the vector store and the Bedrock knowledge base over them."""

import hashlib
import json
from typing import TYPE_CHECKING, Any

from aws_cdk import (
    Aws,
    Duration,
    RemovalPolicy,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
)

if TYPE_CHECKING:
    from infra.stacks.andur_stack import AndurStack


#: Titan Text Embeddings V2. The model id is part of the Bedrock ARN.
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"

EMBEDDING_DIMENSION = 1024

#: One value, two spellings: S3 Vectors wants ``float32``, Bedrock ``FLOAT32``.
VECTOR_DATA_TYPE = "float32"

#: Cosine matches how Titan embeddings are normalised.
DISTANCE_METRIC = "cosine"

WORKSPACE_PREFIX = "workspaces/"

CHUNKING_STRATEGY = "FIXED_SIZE"
CHUNK_MAX_TOKENS = 300
CHUNK_OVERLAP_PERCENT = 13

NON_FILTERABLE_METADATA_KEYS = [
    "AMAZON_BEDROCK_TEXT",
    "AMAZON_BEDROCK_METADATA",
    "source",
    "license",
]

NONCURRENT_VERSION_RETENTION_DAYS = 30


def replacement_safe_name(
    prefix: str, create_only_settings: list[Any], spelled_out: tuple[Any, ...] = ()
) -> str:
    """Build a resource name that changes whenever the resource gets replaced."""
    digest = hashlib.sha256(json.dumps(create_only_settings).encode("utf-8")).hexdigest()[:8]
    return prefix + "-".join([*(str(part) for part in spelled_out), digest])


VECTOR_INDEX_NAME = replacement_safe_name(
    "andur-index-",
    [VECTOR_DATA_TYPE, EMBEDDING_DIMENSION, DISTANCE_METRIC, NON_FILTERABLE_METADATA_KEYS],
    spelled_out=(EMBEDDING_DIMENSION, DISTANCE_METRIC),
)
KNOWLEDGE_BASE_NAME = replacement_safe_name(
    "andur-knowledge-base-",
    [VECTOR_INDEX_NAME, EMBEDDING_MODEL_ID, EMBEDDING_DIMENSION, VECTOR_DATA_TYPE],
)
DATA_SOURCE_NAME = replacement_safe_name(
    "andur-documents-",
    [CHUNKING_STRATEGY, CHUNK_MAX_TOKENS, CHUNK_OVERLAP_PERCENT],
)


def create_documents_bucket(stack: "AndurStack") -> s3.Bucket:
    """The source of truth: the files a workspace owner uploads."""
    bucket = s3.Bucket(
        stack,
        "DocumentsBucket",
        bucket_name=f"andur-documents-{Aws.ACCOUNT_ID}-{Aws.REGION}",
        versioned=True,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        lifecycle_rules=[
            s3.LifecycleRule(
                id="ExpireOldVersions",
                noncurrent_version_expiration=Duration.days(NONCURRENT_VERSION_RETENTION_DAYS),
            )
        ],
        removal_policy=RemovalPolicy.RETAIN,
    )
    if bucket.policy is None:
        raise RuntimeError("enforce_ssl=True should have created a bucket policy")
    bucket.policy.apply_removal_policy(RemovalPolicy.RETAIN)
    return bucket


def create_vector_store(
    stack: "AndurStack",
) -> tuple[s3vectors.CfnVectorBucket, s3vectors.CfnIndex]:
    """The S3 Vectors bucket and the single index the knowledge base writes to."""
    vector_bucket = s3vectors.CfnVectorBucket(
        stack,
        "VectorBucket",
        vector_bucket_name=f"andur-vectors-{Aws.ACCOUNT_ID}-{Aws.REGION}",
    )
    vector_bucket.apply_removal_policy(RemovalPolicy.DESTROY)

    vector_index = s3vectors.CfnIndex(
        stack,
        "VectorIndex",
        # Derived, not fixed: see docs/decisions.md, resource names.
        index_name=VECTOR_INDEX_NAME,
        vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
        data_type=VECTOR_DATA_TYPE,
        dimension=EMBEDDING_DIMENSION,
        distance_metric=DISTANCE_METRIC,
        metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
            non_filterable_metadata_keys=NON_FILTERABLE_METADATA_KEYS,
        ),
    )
    vector_index.apply_removal_policy(RemovalPolicy.DESTROY)

    return vector_bucket, vector_index


def create_knowledge_base_role(
    stack: "AndurStack", documents_bucket: s3.Bucket, vector_index: s3vectors.CfnIndex
) -> iam.Role:
    """The role Bedrock assumes while crawling, embedding and indexing."""
    return iam.Role(
        stack,
        "KnowledgeBaseRole",
        # IAM names are account-wide: the region keeps two stacks apart.
        role_name=f"AmazonBedrockExecutionRoleForKnowledgeBase_Andur_{Aws.REGION}",
        description=("Lets Bedrock Knowledge Bases read Andur documents and write Andur vectors."),
        assumed_by=iam.ServicePrincipal(
            "bedrock.amazonaws.com",
            conditions={
                "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                "ArnLike": {
                    "aws:SourceArn": (
                        f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:"
                        f"{Aws.ACCOUNT_ID}:knowledge-base/*"
                    )
                },
            },
        ),
        inline_policies={
            "ReadDocuments": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="S3ListBucketStatement",
                        actions=["s3:ListBucket"],
                        resources=[documents_bucket.bucket_arn],
                        conditions={"StringEquals": {"aws:ResourceAccount": Aws.ACCOUNT_ID}},
                    ),
                    iam.PolicyStatement(
                        sid="S3GetObjectStatement",
                        actions=["s3:GetObject"],
                        resources=[documents_bucket.arn_for_objects("*")],
                        conditions={"StringEquals": {"aws:ResourceAccount": Aws.ACCOUNT_ID}},
                    ),
                ]
            ),
            "InvokeEmbeddingModel": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="BedrockListModelsStatement",
                        actions=["bedrock:ListFoundationModels"],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        sid="BedrockInvokeModelStatement",
                        actions=["bedrock:InvokeModel"],
                        resources=[embedding_model_arn()],
                    ),
                ]
            ),
            "UseVectorIndex": iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="S3VectorBucketReadAndWritePermission",
                        actions=[
                            "s3vectors:PutVectors",
                            "s3vectors:GetVectors",
                            "s3vectors:DeleteVectors",
                            "s3vectors:QueryVectors",
                            "s3vectors:GetIndex",
                        ],
                        resources=[vector_index.attr_index_arn],
                    )
                ]
            ),
        },
    )


def embedding_model_arn() -> str:
    """Foundation model ARNs carry no account id — the model is AWS-owned."""
    return f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{EMBEDDING_MODEL_ID}"


def create_knowledge_base(
    stack: "AndurStack", vector_index: s3vectors.CfnIndex, role: iam.Role
) -> bedrock.CfnKnowledgeBase:
    """The managed RAG pipeline: embed, store, retrieve."""
    return bedrock.CfnKnowledgeBase(
        stack,
        "KnowledgeBase",
        name=KNOWLEDGE_BASE_NAME,
        description="Andur: internal knowledge assistant, one workspace per company.",
        role_arn=role.role_arn,
        knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
            type="VECTOR",
            vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                embedding_model_arn=embedding_model_arn(),
                embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                    bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                        dimensions=EMBEDDING_DIMENSION,
                        embedding_data_type=VECTOR_DATA_TYPE.upper(),
                    )
                ),
            ),
        ),
        storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
            type="S3_VECTORS",
            s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                index_arn=vector_index.attr_index_arn,
            ),
        ),
    )


def create_data_source(
    stack: "AndurStack", knowledge_base: bedrock.CfnKnowledgeBase, documents_bucket: s3.Bucket
) -> bedrock.CfnDataSource:
    """What to crawl, and how to cut it into chunks."""
    return bedrock.CfnDataSource(
        stack,
        "DocumentsDataSource",
        name=DATA_SOURCE_NAME,
        description=(
            f"All workspaces under the {WORKSPACE_PREFIX} prefix of the documents bucket."
        ),
        knowledge_base_id=knowledge_base.attr_knowledge_base_id,
        data_deletion_policy="DELETE",
        data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
            type="S3",
            s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                bucket_arn=documents_bucket.bucket_arn,
                inclusion_prefixes=[WORKSPACE_PREFIX],
            ),
        ),
        vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
            chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                chunking_strategy=CHUNKING_STRATEGY,
                fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                    max_tokens=CHUNK_MAX_TOKENS,
                    overlap_percentage=CHUNK_OVERLAP_PERCENT,
                ),
            )
        ),
    )

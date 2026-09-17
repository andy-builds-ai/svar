"""The Andur infrastructure stack.

One stack holds everything the retrieval side of Andur needs:

* an S3 bucket for the source documents, one prefix per workspace,
* an S3 Vectors bucket and index that hold the embeddings,
* a Bedrock knowledge base that ties the two together, plus the service
  role it assumes,
* a data source that tells the knowledge base which prefix to crawl.

No compute lives here. The Lambda handler and the web page arrive in later
build steps and will read the stack outputs defined at the bottom.
"""

import hashlib
import json
from typing import Any

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
)
from constructs import Construct

# --- Configuration -------------------------------------------------------
# Kept as module constants so the tests can assert against the same values
# the stack deploys, instead of repeating magic numbers.

#: Titan Text Embeddings V2. The model id is part of the Bedrock ARN.
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"

#: Titan V2 can emit 256, 512 or 1024 dimensions. We pick the largest for
#: retrieval quality; the vector index must use the exact same number.
EMBEDDING_DIMENSION = 1024

#: One value, two spellings: S3 Vectors wants ``float32``, Bedrock ``FLOAT32``.
VECTOR_DATA_TYPE = "float32"

#: Cosine matches how Titan embeddings are normalised.
DISTANCE_METRIC = "cosine"

#: Every document lives under this prefix, followed by the workspace name:
#: ``workspaces/<workspace>/<layer>/<file>``. The knowledge base crawls the
#: whole prefix; a query narrows it down with a ``workspace`` filter.
WORKSPACE_PREFIX = "workspaces/"

#: Fixed-size chunking, sized so that a stored chunk is about a paragraph.
#: Bedrock takes the overlap as a percentage, not a token count: 13 % of 300 is
#: about 39 tokens, a sentence or two, enough that a statement cut at a chunk
#: border still appears whole in one of the two.
#:
#: Chunk size alone does not decide what retrieve returns. For documents with
#: Markdown tables Bedrock returned the whole document, under fixed-size and
#: hierarchical chunking alike (measured 17.09.2026). The fill script therefore
#: uploads each Markdown document split into sections; see scripts/ingest.py
#: and "Retrieval findings" in the README.
CHUNKING_STRATEGY = "FIXED_SIZE"
CHUNK_MAX_TOKENS = 300
CHUNK_OVERLAP_PERCENT = 13

#: Bedrock stores the chunk text and its own source metadata as vector
#: metadata. Both must be declared non-filterable, otherwise they count
#: against the filterable-metadata budget and ingestion fails. ``source``
#: and ``license`` are ours: worth keeping next to a hit, never filtered on.
#: What stays filterable is exactly ``workspace`` and ``layer``.
NON_FILTERABLE_METADATA_KEYS = [
    "AMAZON_BEDROCK_TEXT",
    "AMAZON_BEDROCK_METADATA",
    "source",
    "license",
]

#: How long an overwritten or deleted document stays recoverable. Versioning
#: without an expiry would keep every old version, and bill for it, forever.
NONCURRENT_VERSION_RETENTION_DAYS = 30


def replacement_safe_name(
    prefix: str, create_only_settings: list[Any], spelled_out: tuple[Any, ...] = ()
) -> str:
    """Build a resource name that changes whenever the resource gets replaced.

    Changing a create-only setting makes CloudFormation replace the resource,
    and the replacement is created before the old one is deleted. Under a
    fixed name the new resource collides with the old ("already exists").
    A short digest of the create-only settings gives the replacement a name
    of its own. ``spelled_out`` repeats some settings in plain text for a
    human reading the console. List order counts: CloudFormation compares
    lists in order, so a reordering is a replacement too.

    Only settings passed in here are covered. The structural type literals
    in the stack (``VECTOR``, ``S3_VECTORS``, ``S3``) are not; changing one
    of those rebuilds the configuration anyway and should change ``prefix``.
    """
    digest = hashlib.sha256(json.dumps(create_only_settings).encode("utf-8")).hexdigest()[:8]
    return prefix + "-".join([*(str(part) for part in spelled_out), digest])


#: The three names that follow their configuration. Each list is what the
#: resource's CloudFormation schema marks create-only, as far as this stack
#: sets it. The knowledge base includes the index name, so a new index
#: renames the base as well; a new base gives the data source a fresh
#: namespace, since data source names only need to be unique within a base.
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


class AndurStack(Stack):
    """Storage, vector index and knowledge base for the Andur assistant."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.documents_bucket = self._create_documents_bucket()
        self.vector_bucket, self.vector_index = self._create_vector_store()
        self.knowledge_base_role = self._create_knowledge_base_role(
            self.documents_bucket, self.vector_index
        )
        self.knowledge_base = self._create_knowledge_base(
            self.vector_index, self.knowledge_base_role
        )
        self.data_source = self._create_data_source(self.knowledge_base, self.documents_bucket)

        self._create_outputs()

    # --- Documents -------------------------------------------------------

    def _create_documents_bucket(self) -> s3.Bucket:
        """The source of truth: the files a workspace owner uploads.

        Retained on ``cdk destroy``, together with its bucket policy. Vectors
        can be rebuilt from documents; documents cannot be rebuilt from
        anything, so the stack never deletes them on its own.
        """
        bucket = s3.Bucket(
            self,
            "DocumentsBucket",
            bucket_name=f"andur-documents-{Aws.ACCOUNT_ID}-{Aws.REGION}",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireOldVersions",
                    noncurrent_version_expiration=Duration.days(
                        NONCURRENT_VERSION_RETENTION_DAYS
                    ),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        # `enforce_ssl` creates a separate bucket policy resource, and the
        # removal policy above does not reach it. Without this line
        # `cdk destroy` would keep the bucket but strip its SSL-only rule.
        if bucket.policy is None:
            raise RuntimeError("enforce_ssl=True should have created a bucket policy")
        bucket.policy.apply_removal_policy(RemovalPolicy.RETAIN)
        return bucket

    # --- Vector store ----------------------------------------------------

    def _create_vector_store(self) -> tuple[s3vectors.CfnVectorBucket, s3vectors.CfnIndex]:
        """The S3 Vectors bucket and the single index the knowledge base writes to.

        Both are destroyed with the stack: their contents are derived data,
        reproducible by re-running a sync against the documents bucket.
        """
        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"andur-vectors-{Aws.ACCOUNT_ID}-{Aws.REGION}",
        )
        vector_bucket.apply_removal_policy(RemovalPolicy.DESTROY)

        vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            # Derived, not fixed — see `replacement_safe_name` for why.
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

    # --- Permissions -----------------------------------------------------

    def _create_knowledge_base_role(
        self, documents_bucket: s3.Bucket, vector_index: s3vectors.CfnIndex
    ) -> iam.Role:
        """The role Bedrock assumes while crawling, embedding and indexing.

        Three inline policies, each the narrowest scope the service accepts:
        read the documents bucket, invoke the embedding model, read and write
        this one vector index. Inline rather than attached, so the
        permissions exist the moment the role does — a knowledge base that
        is created before its role can act fails validation.

        Bedrock requires the role name to start with
        ``AmazonBedrockExecutionRoleForKnowledgeBase_``.
        """
        return iam.Role(
            self,
            "KnowledgeBaseRole",
            role_name="AmazonBedrockExecutionRoleForKnowledgeBase_Andur",
            description="Lets Bedrock Knowledge Bases read Andur documents and write Andur vectors.",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    # Only our own account, and only for a knowledge base —
                    # this is what stops the role from being borrowed by
                    # another customer's Bedrock resource (confused deputy).
                    # Any knowledge base of this account qualifies. Naming the
                    # one base would need its id, which exists only after the
                    # base is created — a second deploy just to tighten this.
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
                            # Listing is not resource-scopable; Bedrock reads
                            # the model catalogue when the base is created.
                            actions=["bedrock:ListFoundationModels"],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            sid="BedrockInvokeModelStatement",
                            actions=["bedrock:InvokeModel"],
                            resources=[self.embedding_model_arn],
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

    # --- Knowledge base and data source ----------------------------------

    @property
    def embedding_model_arn(self) -> str:
        """Foundation model ARNs carry no account id — the model is AWS-owned."""
        return f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{EMBEDDING_MODEL_ID}"

    def _create_knowledge_base(
        self, vector_index: s3vectors.CfnIndex, role: iam.Role
    ) -> bedrock.CfnKnowledgeBase:
        """The managed RAG pipeline: embed, store, retrieve.

        Chunking is not configured here; it belongs to the data source.
        """
        return bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=KNOWLEDGE_BASE_NAME,
            description="Andur: internal knowledge assistant, one workspace per company.",
            role_arn=role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=self.embedding_model_arn,
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
                # The index ARN alone identifies bucket and index. Passing the
                # bucket ARN as well would violate the property's oneOf rule.
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=vector_index.attr_index_arn,
                ),
            ),
        )

    def _create_data_source(
        self, knowledge_base: bedrock.CfnKnowledgeBase, documents_bucket: s3.Bucket
    ) -> bedrock.CfnDataSource:
        """What to crawl, and how to cut it into chunks.

        ``DELETE`` removes this data source's vectors from the index when the
        data source goes away. That matters on a replacement: with ``RETAIN``
        the old vectors would stay in the index and the next sync would add
        every chunk a second time. The price is a slower ``cdk destroy``.
        """
        return bedrock.CfnDataSource(
            self,
            "DocumentsDataSource",
            name=DATA_SOURCE_NAME,
            description=f"All workspaces under the {WORKSPACE_PREFIX} prefix of the documents bucket.",
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

    # --- Outputs ---------------------------------------------------------

    def _create_outputs(self) -> None:
        """Everything a later build step or a CLI call needs to address this stack."""
        CfnOutput(
            self,
            "KnowledgeBaseId",
            value=self.knowledge_base.attr_knowledge_base_id,
            description="Pass this to bedrock-agent-runtime retrieve.",
        )
        CfnOutput(
            self,
            "DataSourceId",
            value=self.data_source.attr_data_source_id,
            description="Pass this to bedrock-agent start-ingestion-job.",
        )
        CfnOutput(
            self,
            "DocumentsBucketName",
            value=self.documents_bucket.bucket_name,
            description=(
                f"Upload documents under {WORKSPACE_PREFIX}<workspace>/<layer>/, each with "
                "its sidecar file (docs/metadata.md). Without it there is no workspace filter."
            ),
        )
        CfnOutput(
            self,
            "VectorBucketName",
            # Not `.ref`: the primary identifier of a vector bucket is its
            # ARN, so `ref` would put an ARN under a name.
            value=self.vector_bucket.vector_bucket_name,
            description="S3 Vectors bucket holding the embeddings.",
        )
        CfnOutput(
            self,
            "VectorIndexArn",
            value=self.vector_index.attr_index_arn,
            description="The vector index the knowledge base writes to.",
        )
        CfnOutput(
            self,
            "KnowledgeBaseRoleArn",
            value=self.knowledge_base_role.role_arn,
            description="Service role assumed by Bedrock Knowledge Bases.",
        )

"""The Andur infrastructure stack.

One stack holds everything Andur needs:

* an S3 bucket for the source documents, one prefix per workspace,
* an S3 Vectors bucket and index that hold the embeddings,
* a Bedrock knowledge base that ties the two together, plus the service
  role it assumes,
* a data source that tells the knowledge base which prefix to crawl,
* one key per workspace in Parameter Store, generated during the deploy,
* a DynamoDB table that counts what each key has spent of its budget,
* the answer function (app/handler.py) behind a Function URL.

Nothing here runs while nobody asks: the function runs only while a question
is in flight, the keys are free standard parameters, and the budget table is
billed per request. What an idle month costs is storage, in cents.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from aws_cdk import (
    Aws,
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrock as bedrock,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
    custom_resources as cr,
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
#: and "Retrieval findings" in the README. Setting the foundation-model parser
#: on the data source was measured too and changed nothing for Markdown.
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

# --- Answering (build step 3) ----------------------------------------------

#: The answering model: Nemotron 3 Super (model choice 15.09.2026). This is the
#: one place that names it; the handler reads it from its environment.
ANSWER_MODEL_ID = "nvidia.nemotron-super-3-120b"

#: How many passages retrieve hands to the model per question.
NUMBER_OF_RESULTS = 6

#: Every workspace that gets a key. A workspace without a key cannot be asked,
#: so adding a company means adding it here and deploying.
WORKSPACES = ("quillmoor", "pellwick")

#: Parameter Store path of the keys: one SecureString per workspace, named
#: ``<path>/<workspace>``.
API_KEY_PARAMETER_PATH = "/andur/api-keys"

# --- Key budgets -----------------------------------------------------------
# A key is not a password but an allowance: so many questions, and a window
# that starts with the first one. The counter needs a store outside the
# function, because every request may run in a different instance, and five
# may run at once.

#: One item per key. A fixed name, so a person can type it in a CLI command;
#: it holds no secret, so it may be read.
BUDGET_TABLE_NAME = "andur-key-budgets"

#: The partition key: a SHA-256 of the key, never the key. The table can be
#: read without a key falling out of it.
BUDGET_KEY_ATTRIBUTE = "key_hash"

#: The attribute DynamoDB's TTL watches. An item whose value (epoch seconds)
#: lies in the past is removed, within days rather than at the second.
BUDGET_TTL_ATTRIBUTE = "delete_after"

#: What a key may do unless its own item says otherwise (decided 17.09.2026):
#: ten questions, and 48 hours from the first one.
DEFAULT_QUESTION_LIMIT = 10
DEFAULT_WINDOW_HOURS = 48

#: How long an item is kept, counted from the key's first question. The item
#: is what makes a spent key stay spent, so this is not a cleanup interval
#: but the lifetime of that fact: once it is gone, the key starts over with a
#: full budget. A year is far past the life of the link a key travels with,
#: and a few hundred items cost nothing either way. To end a key for good,
#: delete its parameter and deploy — the new key hashes differently.
DEFAULT_KEEP_DAYS = 365

#: The newest Python runtime this CDK version offers.
LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_14

#: Fixed, so that its log group has a name a person can type. Changing it
#: replaces the function under the new name, which does not collide.
ANSWER_FUNCTION_NAME = "andur-answer"

#: Longer than the handler's worst case (``worst_case_seconds`` in app/aws.py,
#: 135 seconds, checked by a test), so a hanging Bedrock call ends as a 502
#: with a reason. Far below the 15 minutes a Function URL call may take.
ANSWER_TIMEOUT_SECONDS = 180

#: Lambda gives CPU in proportion to memory; 512 MB keeps the cold start with
#: boto3 short, and a question takes a few seconds of it.
ANSWER_MEMORY_MB = 512

#: At most this many requests run at the same time; Lambda answers the rest
#: with 429 before any code runs. The URL is public, so this is the hard cap on
#: Lambda time and with it on model calls per second, whatever arrives —
#: with a valid key or not. What one key may spend in total is the budget
#: below; this is only the rate at which anything at all can arrive.
#:
#: The price is that five concurrent calls from a stranger hold the service
#: up: Lambda answers everyone else with 429, and a demo that costs nothing
#: is unavailable until they stop. Taken on purpose — capping the bill matters
#: more here than keeping a demo reachable, and a WAF in front of the URL is
#: the answer if it ever becomes a real problem.
ANSWER_MAX_CONCURRENCY = 5

#: How long function logs are kept. Without a limit they are kept, and billed,
#: forever.
LOG_RETENTION = logs.RetentionDays.ONE_MONTH

#: The repository root; function code is packaged from folders below it.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Left out of every function package. Without this, a test run that leaves
#: ``__pycache__`` behind would change the package and make `cdk diff` report a
#: code change that is none.
ASSET_EXCLUDES = ["**/__pycache__", "*.pyc", "README.md"]


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

        self.api_keys = self._create_api_keys()
        self.budget_table = self._create_budget_table()
        self.answer_log_group = self._create_log_group(
            "AnswerFunctionLogs", f"/aws/lambda/{ANSWER_FUNCTION_NAME}"
        )
        self.answer_role = self._create_answer_role(
            self.knowledge_base, self.budget_table, self.answer_log_group
        )
        self.answer_function = self._create_answer_function(
            self.knowledge_base, self.budget_table, self.answer_role, self.answer_log_group
        )
        self.function_url = self._create_function_url(self.answer_function, self.api_keys)

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

    # --- Logs ------------------------------------------------------------

    def _create_log_group(self, construct_id: str, name: str | None = None) -> logs.LogGroup:
        """A log group with a retention, deleted with the stack.

        Created by the stack rather than by Lambda on first use: a log group
        Lambda creates has no retention and outlives ``cdk destroy``.
        """
        return logs.LogGroup(
            self,
            construct_id,
            log_group_name=name,
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

    # --- Workspace keys --------------------------------------------------

    def _create_api_keys(self) -> list[CustomResource]:
        """One key per workspace, generated inside AWS while the stack deploys.

        CloudFormation cannot create SecureString parameters, and a key typed
        into the template would sit in cdk.out and in CloudFormation. So a
        small function (infra/api_keys/handler.py) generates each key and
        stores it; the CDK provider framework runs it as a custom resource and
        reports back to CloudFormation. An existing key survives every deploy.
        """
        log_group = self._create_log_group("ApiKeyFunctionLogs")
        role = iam.Role(
            self,
            "ApiKeyFunctionRole",
            description="Lets the Andur key function create and delete workspace keys.",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={
                "ManageApiKeys": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="PutAndDeleteWorkspaceKeys",
                            actions=["ssm:PutParameter", "ssm:DeleteParameter"],
                            resources=[f"{self.api_key_path_arn}/*"],
                        )
                    ]
                ),
                "WriteLogs": self._write_logs_policy(log_group),
            },
        )
        function = lambda_.Function(
            self,
            "ApiKeyFunction",
            description="Generates Andur workspace keys during cdk deploy.",
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
        provider = cr.Provider(
            self,
            "ApiKeyProvider",
            on_event_handler=function,
            log_group=self._create_log_group("ApiKeyProviderLogs"),
        )
        return [
            CustomResource(
                self,
                f"ApiKey-{workspace}",
                service_token=provider.service_token,
                resource_type="Custom::AndurApiKey",
                properties={
                    "ParameterName": f"{API_KEY_PARAMETER_PATH}/{workspace}",
                    "ParameterPath": API_KEY_PARAMETER_PATH,
                },
                # CloudFormation otherwise waits an hour for a custom resource
                # that never answers.
                service_timeout=Duration.minutes(5),
            )
            for workspace in WORKSPACES
        ]

    @property
    def api_key_path_arn(self) -> str:
        """The ARN of the key path; a key is this plus ``/<workspace>``."""
        return f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{API_KEY_PARAMETER_PATH}"

    # --- Key budgets -----------------------------------------------------

    def _create_budget_table(self) -> dynamodb.TableV2:
        """One item per key: what it has spent, of how much, and until when.

        On demand, not provisioned: a public URL has no rate to provision for,
        and a counter that throttles refuses honest questions.

        Choosing on demand gives up the permanent free tier of 25 write and
        25 read units, which DynamoDB grants to provisioned capacity only. It
        is not worth the trade. On demand costs 0.625 USD per million writes
        in us-east-1 (on-demand pricing page, read 17.09.2026), one write per
        question, against roughly 1,000 USD per million questions at the
        answering model — three orders of magnitude apart. One item is a few
        hundred bytes, so a question is that one write unit and nothing else.

        Deleted with the stack. The items are worth exactly as much as the
        keys they belong to, and a ``cdk destroy`` takes those with it.

        ``TableV2`` renders as ``AWS::DynamoDB::GlobalTable`` with a single
        replica in this region. That is the construct's normal shape, not a
        second region: no stream, no replication, one ordinary table.
        """
        return dynamodb.TableV2(
            self,
            "KeyBudgetTable",
            table_name=BUDGET_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name=BUDGET_KEY_ATTRIBUTE, type=dynamodb.AttributeType.STRING
            ),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute=BUDGET_TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.DESTROY,
        )

    @staticmethod
    def _write_logs_policy(log_group: logs.LogGroup) -> iam.PolicyDocument:
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

    # --- Answer function -------------------------------------------------

    @property
    def answer_model_arn(self) -> str:
        return f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{ANSWER_MODEL_ID}"

    def _create_answer_role(
        self,
        knowledge_base: bedrock.CfnKnowledgeBase,
        budget_table: dynamodb.TableV2,
        log_group: logs.LogGroup,
    ) -> iam.Role:
        """The execution role of the answer function: five things, each on one resource.

        Read the workspace keys, count a question against its key's budget,
        retrieve from this knowledge base, invoke the answering model
        (``converse`` is authorised as ``InvokeModel``), write its own log.
        The keys use the AWS managed key ``aws/ssm``, whose key policy already
        lets this account decrypt through Parameter Store, so no KMS statement
        is needed — the deploy of 17.09.2026 read the keys with exactly these
        policies.

        ``UpdateItem`` alone on the budget table: the function counts, and a
        conditional update is the whole of that. It never reads an item by
        itself and never writes one from scratch, so ``GetItem`` and
        ``PutItem`` would be permissions nothing uses.
        """
        return iam.Role(
            self,
            "AnswerFunctionRole",
            description="Lets the Andur answer function read keys, retrieve, invoke the model and log.",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={
                "ReadApiKeys": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="ReadWorkspaceKeys",
                            actions=["ssm:GetParametersByPath"],
                            resources=[self.api_key_path_arn],
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
                        iam.PolicyStatement(
                            sid="InvokeAnswerModelStatement",
                            actions=["bedrock:InvokeModel"],
                            resources=[self.answer_model_arn],
                        )
                    ]
                ),
                "WriteLogs": self._write_logs_policy(log_group),
            },
        )

    def _create_answer_function(
        self,
        knowledge_base: bedrock.CfnKnowledgeBase,
        budget_table: dynamodb.TableV2,
        role: iam.Role,
        log_group: logs.LogGroup,
    ) -> lambda_.Function:
        """The question handler from app/, configured entirely through its environment.

        boto3 is not packaged: the Lambda runtime brings it. The handler logs
        the version on every cold start, so a runtime too old for
        ``bedrock-agent-runtime`` would show there (measured after the deploy
        of 17.09.2026, see app/README.md).
        """
        return lambda_.Function(
            self,
            "AnswerFunction",
            function_name=ANSWER_FUNCTION_NAME,
            description="Andur: answers a question from one workspace's documents.",
            runtime=LAMBDA_RUNTIME,
            architecture=lambda_.Architecture.ARM_64,
            code=lambda_.Code.from_asset(str(REPOSITORY_ROOT / "app"), exclude=ASSET_EXCLUDES),
            handler="handler.handler",
            timeout=Duration.seconds(ANSWER_TIMEOUT_SECONDS),
            memory_size=ANSWER_MEMORY_MB,
            reserved_concurrent_executions=ANSWER_MAX_CONCURRENCY,
            role=role,
            log_group=log_group,
            environment={
                "KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
                "MODEL_ID": ANSWER_MODEL_ID,
                "REGION": Aws.REGION,
                "NUMBER_OF_RESULTS": str(NUMBER_OF_RESULTS),
                "API_KEY_PARAMETER_PATH": API_KEY_PARAMETER_PATH,
                "BUDGET_TABLE_NAME": budget_table.table_name,
                "QUESTION_LIMIT": str(DEFAULT_QUESTION_LIMIT),
                "WINDOW_HOURS": str(DEFAULT_WINDOW_HOURS),
                "KEEP_DAYS": str(DEFAULT_KEEP_DAYS),
            },
        )

    def _create_function_url(
        self, function: lambda_.Function, api_keys: list[CustomResource]
    ) -> lambda_.FunctionUrl:
        """A public HTTPS address for the function; the key check in the handler is the lock.

        ``NONE`` rather than ``AWS_IAM``: IAM auth would need every request
        signed with AWS credentials, which the people asking do not have. The
        URL is created only after every key exists, so it is never reachable
        without one.
        """
        url = function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
        for api_key in api_keys:
            url.node.add_dependency(api_key)
        return url

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
        CfnOutput(
            self,
            "FunctionUrl",
            value=self.function_url.url,
            description="POST a question here with Authorization: Bearer <workspace key>.",
        )
        CfnOutput(
            self,
            "ApiKeyParameterPath",
            value=API_KEY_PARAMETER_PATH,
            description="Parameter Store path of the workspace keys: <path>/<workspace>, SecureString.",
        )
        CfnOutput(
            self,
            "BudgetTableName",
            value=self.budget_table.table_name,
            description="One item per key, found by the SHA-256 of the key: questions used, window, expiry.",
        )

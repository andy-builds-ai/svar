"""The Andur infrastructure stack."""

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
    Token,
    aws_bedrock as bedrock,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_s3vectors as s3vectors,
    custom_resources as cr,
)
from constructs import Construct

from infra.answer_models import PRICE_DATE, answer_model

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


#: A model id runs in the stack's region; an ``eu.`` inference profile id runs
#: in the EU regions that profile routes to, and nowhere else. Its token prices
#: come from infra/answer_models.py.
ANSWER_MODEL_ID = "eu.amazon.nova-pro-v1:0"

EU_PROFILE_PREFIX = "eu."

#: How many passages retrieve hands to the model per question.
NUMBER_OF_RESULTS = 10


GUARDRAIL_NAME = "andur-answer-guardrail"

#: The Classic tier reads denied topics in English, French and Spanish only; it let
#: German tax, legal and medical questions through. The Standard tier reads German
#: and needs a guardrail profile. Both tiers cost the same per text unit.
TOPICS_TIER = "STANDARD"

#: The EU guardrail profile. Called from an EU region, it routes the guardrail's
#: checks to these regions and no others (Bedrock user guide, "Supported Regions
#: for cross-Region guardrail inference").
GUARDRAIL_PROFILE_ID = "eu.guardrail.v1:0"
GUARDRAIL_PROFILE_REGIONS = (
    "eu-central-1",
    "eu-west-1",
    "eu-west-3",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
)

#: Name, definition and examples of each topic, in the order Bedrock reads them; the order
#: changes verdicts too. A broad name such as "Legal advice" refused questions about the
#: business's own contracts and prices. Measure every change with `python -m
#: scripts.topic_probe` before it is deployed (docs/decisions.md).
DENIED_TOPICS = (
    (
        "Legal action",
        "Questions about taking or facing legal action: suing someone or being sued, going to "
        "court, hiring a lawyer, and whether a court would find an act legal, illegal or "
        "enforceable.",
        [
            "Can I sue my landlord over this?",
            "Is this non-compete clause enforceable?",
            "Can I take my neighbour to court over the noise?",
        ],
    ),
    (
        "Tax returns",
        "Questions about a person's own tax return and the tax office: what money they get back "
        "through a tax return, what income they must declare, and how to pay less tax or hide "
        "money from the tax office.",
        [
            "How do I pay less income tax?",
            "Do I have to declare this income?",
            "Do I need to file a tax return this year?",
        ],
    ),
    (
        "Medical advice",
        "Questions about a person's health: what a symptom, pain, rash or injury means, whether "
        "it is serious or needs a doctor, and which medicine, dose, remedy or treatment to take.",
        [
            "What should I take for a headache?",
            "Is this rash serious?",
            "How much ibuprofen can I take?",
            "Was kann ich gegen den Kater einwerfen?",
        ],
    ),
    (
        "Private lives",
        "Questions about the private life of a person: their partner, family, health, home, "
        "wealth, political views or religious beliefs; and questions about celebrities, "
        "politicians or other public figures.",
        [
            "Who is the richest person in the world?",
            "Is our CEO married?",
            "Where does my colleague live?",
            "Welcher Religion gehört mein Kollege an?",
        ],
    ),
)

MASKED_PII_TYPES = ("NAME", "EMAIL", "PHONE", "ADDRESS")

GROUNDING_THRESHOLD = 0.7
RELEVANCE_THRESHOLD = 0.7

BLOCKED_INPUT_MESSAGE = (
    "Svar answers questions about this company's documents. It does not give legal, tax or "
    "medical advice, and it does not answer questions about people's private lives or public "
    "figures."
)
BLOCKED_OUTPUT_MESSAGE = (
    "Svar withheld this answer: the documents do not carry it, or it strays into advice "
    "Svar does not give."
)

EXCERPT_GUARDRAIL_NAME = "andur-excerpt-guardrail"

#: The guardrail that checks the answer for `DENIED_TOPICS`, with its citations taken out: on
#: the answer as the model writes it, `Private lives` withheld short harmless answers whose
#: citations read [1][4] or [1][2][4], and without them none (04.10.2026, docs/decisions.md).
TOPIC_GUARDRAIL_NAME = "andur-topic-guardrail"

#: Whether the answer guardrail on the model call reads the denied topics in the answer too.
#: True for the one deploy that brings in the topic guardrail: in the seconds CloudFormation
#: gives the old code the new environment, the old code would call an answer guardrail that no
#: longer reads the answer's topics, without the topic check it does not know yet. With this
#: True the answer guardrail reads the answer's topics as deployed until now; the new code checks
#: the answer twice
#: (on the model call, with citations, and by the topic guardrail). Set False in a second
#: deploy, once the first is through: then old and new code both know the topic check.
ANSWER_GUARDRAIL_READS_ANSWER_TOPICS = False

#: Required by Bedrock, never shown: masking replaces text, it does not block.
EXCERPT_BLOCKED_MESSAGE = "This excerpt was withheld."


def masking_policy() -> bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty:
    """The masking both guardrails share: `MASKED_PII_TYPES` in the output, never in the input."""
    return bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
        pii_entities_config=[
            bedrock.CfnGuardrail.PiiEntityConfigProperty(
                type=pii_type,
                action="ANONYMIZE",
                input_enabled=False,
                input_action="NONE",
                output_enabled=True,
                output_action="ANONYMIZE",
            )
            for pii_type in MASKED_PII_TYPES
        ]
    )


#: USD per 1,000 tokens or guardrail text units, eu-west-1, standard tier:
#: the AWS Price List API behind aws.amazon.com/bedrock/pricing, read on
#: `PRICE_DATE`. The token prices are the answer model's own.
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

#: The newest Python runtime this CDK version offers.
LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_14

ANSWER_FUNCTION_NAME = "andur-answer"

ANSWER_TIMEOUT_SECONDS = 180

ANSWER_MEMORY_MB = 512

ANSWER_MAX_CONCURRENCY = 5

#: How long a deploy that replaces a guardrail version waits after the answer function has
#: changed, before CloudFormation deletes the old versions: longer than an answer may run, so
#: no answer that started on an old version still needs it (infra/deploy_drain/handler.py).
DEPLOY_DRAIN_SECONDS = ANSWER_TIMEOUT_SECONDS + 30

LOG_RETENTION = logs.RetentionDays.ONE_MONTH

#: The repository root; function code is packaged from folders below it.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

ASSET_EXCLUDES = ["**/__pycache__", "*.pyc", "README.md"]


WEB_FOLDER = REPOSITORY_ROOT / "web"

ASK_PATH = "/ask"

#: The file CloudFront serves for the bare address.
SITE_ROOT_DOCUMENT = "index.html"

ORIGIN_RESPONSE_TIMEOUT_SECONDS = 120

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)

HSTS_MAX_AGE_DAYS = 730


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


class AndurStack(Stack):
    """Everything Andur runs on, in one stack."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        answer_model_id: str = ANSWER_MODEL_ID,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.answer_model_id = answer_model_id
        self.answer_model = answer_model(answer_model_id)

        self.documents_bucket = self._create_documents_bucket()
        self.vector_bucket, self.vector_index = self._create_vector_store()
        self.knowledge_base_role = self._create_knowledge_base_role(
            self.documents_bucket, self.vector_index
        )
        self.knowledge_base = self._create_knowledge_base(
            self.vector_index, self.knowledge_base_role
        )
        self.data_source = self._create_data_source(self.knowledge_base, self.documents_bucket)

        self.key_retirer = self._create_key_retirer()
        self.budget_table = self._create_budget_table()
        self.answer_log_group = self._create_log_group(
            "AnswerFunctionLogs", f"/aws/lambda/{ANSWER_FUNCTION_NAME}"
        )
        self.guardrail, self.guardrail_version = self._create_guardrail()
        self.excerpt_guardrail, self.excerpt_guardrail_version = self._create_excerpt_guardrail()
        self.topic_guardrail, self.topic_guardrail_version = self._create_topic_guardrail()
        self.answer_role = self._create_answer_role(
            self.knowledge_base,
            self.budget_table,
            self.answer_log_group,
            self.guardrail,
            self.excerpt_guardrail,
            self.topic_guardrail,
        )
        self.answer_function = self._create_answer_function(
            self.knowledge_base,
            self.budget_table,
            self.answer_role,
            self.answer_log_group,
            self.guardrail_version,
            self.excerpt_guardrail_version,
            self.topic_guardrail_version,
        )
        self.function_url = self._create_function_url(self.answer_function)
        self.deploy_drain = self._create_deploy_drain(
            self.answer_function,
            [self.guardrail_version, self.excerpt_guardrail_version, self.topic_guardrail_version],
        )

        self.site_bucket = self._create_site_bucket()
        self.distribution = self._create_distribution(self.site_bucket, self.function_url)
        self.invoke_permission = self._allow_cloudfront_to_invoke(
            self.answer_function, self.distribution
        )
        self.site_files = self._upload_the_page(self.site_bucket, self.distribution)

        self._create_outputs()

    # --- Documents -------------------------------------------------------

    def _create_documents_bucket(self) -> s3.Bucket:
        """The source of truth: the files a workspace owner uploads."""
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
                    noncurrent_version_expiration=Duration.days(NONCURRENT_VERSION_RETENTION_DAYS),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        if bucket.policy is None:
            raise RuntimeError("enforce_ssl=True should have created a bucket policy")
        bucket.policy.apply_removal_policy(RemovalPolicy.RETAIN)
        return bucket

    # --- Vector store ----------------------------------------------------

    def _create_vector_store(self) -> tuple[s3vectors.CfnVectorBucket, s3vectors.CfnIndex]:
        """The S3 Vectors bucket and the single index the knowledge base writes to."""
        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"andur-vectors-{Aws.ACCOUNT_ID}-{Aws.REGION}",
        )
        vector_bucket.apply_removal_policy(RemovalPolicy.DESTROY)

        vector_index = s3vectors.CfnIndex(
            self,
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

    # --- Permissions -----------------------------------------------------

    def _create_knowledge_base_role(
        self, documents_bucket: s3.Bucket, vector_index: s3vectors.CfnIndex
    ) -> iam.Role:
        """The role Bedrock assumes while crawling, embedding and indexing."""
        return iam.Role(
            self,
            "KnowledgeBaseRole",
            # IAM names are account-wide: the region keeps two stacks apart.
            role_name=f"AmazonBedrockExecutionRoleForKnowledgeBase_Andur_{Aws.REGION}",
            description=(
                "Lets Bedrock Knowledge Bases read Andur documents and write Andur vectors."
            ),
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
        """The managed RAG pipeline: embed, store, retrieve."""
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
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=vector_index.attr_index_arn,
                ),
            ),
        )

    def _create_data_source(
        self, knowledge_base: bedrock.CfnKnowledgeBase, documents_bucket: s3.Bucket
    ) -> bedrock.CfnDataSource:
        """What to crawl, and how to cut it into chunks."""
        return bedrock.CfnDataSource(
            self,
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

    # --- Logs ------------------------------------------------------------

    def _create_log_group(self, construct_id: str, name: str | None = None) -> logs.LogGroup:
        """A log group with a retention, deleted with the stack."""
        return logs.LogGroup(
            self,
            construct_id,
            log_group_name=name,
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

    # --- Workspace keys --------------------------------------------------

    def _create_key_retirer(self) -> cr.Provider:
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
        log_group = self._create_log_group("ApiKeyFunctionLogs")
        role = iam.Role(
            self,
            "ApiKeyFunctionRole",
            description="Lets the Andur key retirer write its log; nothing else.",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={"WriteLogs": self._write_logs_policy(log_group)},
        )
        function = lambda_.Function(
            self,
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
            self,
            "ApiKeyProvider",
            on_event_handler=function,
            log_group=self._create_log_group("ApiKeyProviderLogs"),
        )

    @property
    def api_key_path_arn(self) -> str:
        """The ARN of the key path; a key is this plus ``/<workspace>``."""
        return (
            f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}"
            f":parameter{API_KEY_PARAMETER_PATH}"
        )

    # --- Key budgets -----------------------------------------------------

    def _create_budget_table(self) -> dynamodb.TableV2:
        """One item per key: what it has spent, of how much, and until when."""
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

    # --- Guardrail -------------------------------------------------------

    def _guardrail_with_version(
        self, construct_id: str, label: str, properties: dict[str, Any]
    ) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
        """A guardrail from ``properties``, and a numbered version that follows every one of
        them."""
        resolved = json.dumps(self.resolve(properties), sort_keys=True)
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
        guardrail = bedrock.CfnGuardrail(self, construct_id, **properties)
        version = bedrock.CfnGuardrailVersion(
            self,
            f"{construct_id}Version",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=f"Andur {label} guardrail, settings {digest}",
        )
        return guardrail, version

    def _guardrail_profile_arn(self, region: str) -> str:
        """The EU guardrail profile in ``region``.

        A stack whose region is set to one outside the profile's regions would send
        its checks across a border; it does not synthesise. Without a set region
        (no ``env``) the region is unknown at synth time and cannot be checked;
        infra/app.py always sets it.
        """
        if not Token.is_unresolved(self.region) and self.region not in GUARDRAIL_PROFILE_REGIONS:
            raise ValueError(
                f"{self.region} is not a source region of {GUARDRAIL_PROFILE_ID}; "
                f"Andur's guardrail stays in {', '.join(GUARDRAIL_PROFILE_REGIONS)}."
            )
        return self.format_arn(
            service="bedrock",
            region=region,
            resource="guardrail-profile",
            resource_name=GUARDRAIL_PROFILE_ID,
        )

    def _topic_policy(self, on_answer: bool) -> bedrock.CfnGuardrail.TopicPolicyConfigProperty:
        """`DENIED_TOPICS` in the Standard tier, read in the question and, if ``on_answer``,
        in the answer too."""
        return bedrock.CfnGuardrail.TopicPolicyConfigProperty(
            topics_config=[
                bedrock.CfnGuardrail.TopicConfigProperty(
                    name=name,
                    definition=definition,
                    examples=examples,
                    type="DENY",
                    input_enabled=True,
                    input_action="BLOCK",
                    output_enabled=on_answer,
                    output_action="BLOCK" if on_answer else "NONE",
                )
                for name, definition, examples in DENIED_TOPICS
            ],
            topics_tier_config=bedrock.CfnGuardrail.TopicsTierConfigProperty(tier_name=TOPICS_TIER),
        )

    def _create_guardrail(self) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
        """The guardrail on every answer, and the numbered version the function uses. Its
        topics read the question, and the answer only while
        `ANSWER_GUARDRAIL_READS_ANSWER_TOPICS`; the topic guardrail reads the answer."""
        return self._guardrail_with_version(
            "AnswerGuardrail",
            "answer",
            {
                "name": GUARDRAIL_NAME,
                "description": (
                    "Andur: refuses advice topics, masks personal details, withholds "
                    "ungrounded answers."
                ),
                "blocked_input_messaging": BLOCKED_INPUT_MESSAGE,
                "blocked_outputs_messaging": BLOCKED_OUTPUT_MESSAGE,
                "topic_policy_config": self._topic_policy(
                    on_answer=ANSWER_GUARDRAIL_READS_ANSWER_TOPICS
                ),
                "cross_region_config": bedrock.CfnGuardrail.GuardrailCrossRegionConfigProperty(
                    guardrail_profile_arn=self._guardrail_profile_arn(self.region)
                ),
                "sensitive_information_policy_config": masking_policy(),
                "contextual_grounding_policy_config": bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(  # noqa: E501
                    filters_config=[
                        bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                            type="GROUNDING",
                            threshold=GROUNDING_THRESHOLD,
                            action="BLOCK",
                            enabled=True,
                        ),
                        bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                            type="RELEVANCE",
                            threshold=RELEVANCE_THRESHOLD,
                            action="BLOCK",
                            enabled=True,
                        ),
                    ]
                ),
            },
        )

    def _create_excerpt_guardrail(self) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
        """The guardrail that masks the sources' excerpts, and its numbered version."""
        return self._guardrail_with_version(
            "ExcerptGuardrail",
            "excerpt",
            {
                "name": EXCERPT_GUARDRAIL_NAME,
                "description": "Andur: masks personal details in the excerpts of the sources.",
                "blocked_input_messaging": EXCERPT_BLOCKED_MESSAGE,
                "blocked_outputs_messaging": EXCERPT_BLOCKED_MESSAGE,
                "sensitive_information_policy_config": masking_policy(),
            },
        )

    def _create_topic_guardrail(self) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
        """The guardrail that checks the answer, without its citations, for the denied topics,
        and its numbered version. The function applies it as OUTPUT; its topics are set on
        both sides, exactly like the guardrail `python -m scripts.topic_probe` measures."""
        return self._guardrail_with_version(
            "TopicGuardrail",
            "topic",
            {
                "name": TOPIC_GUARDRAIL_NAME,
                "description": "Andur: withholds answers that stray into a denied topic.",
                "blocked_input_messaging": BLOCKED_INPUT_MESSAGE,
                "blocked_outputs_messaging": BLOCKED_OUTPUT_MESSAGE,
                "topic_policy_config": self._topic_policy(on_answer=True),
                "cross_region_config": bedrock.CfnGuardrail.GuardrailCrossRegionConfigProperty(
                    guardrail_profile_arn=self._guardrail_profile_arn(self.region)
                ),
            },
        )

    # --- Answer function -------------------------------------------------

    def _invoke_answer_model(self, guardrail_arn: str) -> list[iam.PolicyStatement]:
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
        if not self.answer_model_id.startswith(EU_PROFILE_PREFIX):
            return [
                iam.PolicyStatement(
                    sid="InvokeAnswerModelStatement",
                    actions=["bedrock:InvokeModel"],
                    resources=[
                        f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/"
                        f"{self.answer_model_id}"
                    ],
                    conditions=with_guardrail,
                )
            ]
        profile_arn = (
            f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:inference-profile/"
            f"{self.answer_model_id}"
        )
        model_id = self.answer_model_id.removeprefix(EU_PROFILE_PREFIX)
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

    def _create_answer_role(
        self,
        knowledge_base: bedrock.CfnKnowledgeBase,
        budget_table: dynamodb.TableV2,
        log_group: logs.LogGroup,
        guardrail: bedrock.CfnGuardrail,
        excerpt_guardrail: bedrock.CfnGuardrail,
        topic_guardrail: bedrock.CfnGuardrail,
    ) -> iam.Role:
        """The execution role of the answer function: five inline policies, every statement
        on named resources. Most name one; `ApplyAnswerGuardrail` names nine — the three
        guardrails, and the EU guardrail profile in each of its six regions.

        It names no guardrail version, so a deploy that replaces one leaves the role as it is.
        """
        return iam.Role(
            self,
            "AnswerFunctionRole",
            description=(
                "Lets the Andur answer function read keys, retrieve, invoke the model and log."
            ),
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={
                "ReadApiKeys": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="ReadWorkspaceKeys",
                            actions=["ssm:GetParameter"],
                            resources=[f"{self.api_key_path_arn}/*"],
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
                        *self._invoke_answer_model(guardrail.attr_guardrail_arn),
                        iam.PolicyStatement(
                            sid="ApplyAnswerGuardrail",
                            actions=["bedrock:ApplyGuardrail"],
                            resources=[
                                guardrail.attr_guardrail_arn,
                                excerpt_guardrail.attr_guardrail_arn,
                                topic_guardrail.attr_guardrail_arn,
                                # The answer and topic guardrails run through the EU profile.
                                *(
                                    self._guardrail_profile_arn(region)
                                    for region in GUARDRAIL_PROFILE_REGIONS
                                ),
                            ],
                        ),
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
        guardrail_version: bedrock.CfnGuardrailVersion,
        excerpt_guardrail_version: bedrock.CfnGuardrailVersion,
        topic_guardrail_version: bedrock.CfnGuardrailVersion,
    ) -> lambda_.Function:
        """The question handler from app/, configured entirely through its environment."""
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
                "MODEL_ID": self.answer_model_id,
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
                "TOPIC_GUARDRAIL_ID": topic_guardrail_version.attr_guardrail_id,
                "TOPIC_GUARDRAIL_VERSION": topic_guardrail_version.attr_version,
                "PRICE_INPUT_PER_1K_TOKENS": self.answer_model.input_per_1k,
                "PRICE_OUTPUT_PER_1K_TOKENS": self.answer_model.output_per_1k,
                "PRICE_TOPIC_PER_1K_UNITS": PRICE_TOPIC_PER_1K_UNITS,
                "PRICE_PII_PER_1K_UNITS": PRICE_PII_PER_1K_UNITS,
                "PRICE_GROUNDING_PER_1K_UNITS": PRICE_GROUNDING_PER_1K_UNITS,
                "PRICE_DATE": PRICE_DATE,
            },
        )

    def _create_function_url(self, function: lambda_.Function) -> lambda_.FunctionUrl:
        """The function's own HTTPS address — reachable for CloudFront alone."""
        return function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)

    def _create_deploy_drain(
        self, function: lambda_.Function, versions: list[bedrock.CfnGuardrailVersion]
    ) -> CustomResource:
        """A resource that waits `DEPLOY_DRAIN_SECONDS` after ``function`` has changed, in every
        deploy that replaces one of ``versions``.

        CloudFormation deletes a replaced version only in the cleanup phase, after every
        resource is updated, this one too; so the old versions outlive the answers that
        started on them. A deploy that replaces no version leaves it alone and does not wait.
        """
        log_group = self._create_log_group("DeployDrainFunctionLogs")
        drain = lambda_.Function(
            self,
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
                self,
                "DeployDrainFunctionRole",
                description="Lets the Andur deploy drain write its log; nothing else.",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                inline_policies={"WriteLogs": self._write_logs_policy(log_group)},
            ),
            log_group=log_group,
        )
        provider = cr.Provider(
            self,
            "DeployDrainProvider",
            on_event_handler=drain,
            log_group=self._create_log_group("DeployDrainProviderLogs"),
        )
        resource = CustomResource(
            self,
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

    # --- The page --------------------------------------------------------

    def _create_site_bucket(self) -> s3.Bucket:
        """The bucket the page is served from — private, and reachable only through CloudFront."""
        return s3.Bucket(
            self,
            "SiteBucket",
            bucket_name=f"andur-site-{Aws.ACCOUNT_ID}-{Aws.REGION}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_security_headers(self) -> cloudfront.ResponseHeadersPolicy:
        """The headers every response carries, page and answer alike."""
        return cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            # CloudFront names are account-wide: the region keeps two stacks apart.
            response_headers_policy_name=f"andur-security-headers-{Aws.REGION}",
            comment="Andur: what the page may load, and how it must be reached.",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=CONTENT_SECURITY_POLICY, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(HSTS_MAX_AGE_DAYS),
                    include_subdomains=True,
                    preload=False,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
            ),
        )

    def _create_distribution(
        self, site_bucket: s3.Bucket, function_url: lambda_.FunctionUrl
    ) -> cloudfront.Distribution:
        """One address for everything: the page from the bucket, `ASK_PATH` from the function."""
        headers = self._create_security_headers()
        return cloudfront.Distribution(
            self,
            "SiteDistribution",
            comment="Andur: the page, and /ask on its way to the answer function.",
            default_root_object=SITE_ROOT_DOCUMENT,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    site_bucket,
                    origin_access_control=cloudfront.S3OriginAccessControl(
                        self,
                        "SiteOriginAccessControl",
                        origin_access_control_name=f"andur-site-{Aws.REGION}",
                    ),
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                response_headers_policy=headers,
            ),
            additional_behaviors={
                ASK_PATH: cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin.with_origin_access_control(
                        function_url,
                        origin_access_control=cloudfront.FunctionUrlOriginAccessControl(
                            self,
                            "AnswerOriginAccessControl",
                            origin_access_control_name=f"andur-answer-{Aws.REGION}",
                        ),
                        read_timeout=Duration.seconds(ORIGIN_RESPONSE_TIMEOUT_SECONDS),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=headers,
                )
            },
        )

    def _allow_cloudfront_to_invoke(
        self, function: lambda_.Function, distribution: cloudfront.Distribution
    ) -> lambda_.CfnPermission:
        """The second of the two permissions the CloudFront documentation asks for."""
        return lambda_.CfnPermission(
            self,
            "InvokeFunctionFromCloudFront",
            action="lambda:InvokeFunction",
            function_name=function.function_arn,
            principal="cloudfront.amazonaws.com",
            source_arn=(
                f"arn:{Aws.PARTITION}:cloudfront::{Aws.ACCOUNT_ID}:"
                f"distribution/{distribution.distribution_id}"
            ),
        )

    def _upload_the_page(
        self, site_bucket: s3.Bucket, distribution: cloudfront.Distribution
    ) -> s3_deployment.BucketDeployment:
        """Put web/ into the site bucket as part of the deploy, and clear the caches."""
        return s3_deployment.BucketDeployment(
            self,
            "SiteFiles",
            sources=[s3_deployment.Source.asset(str(WEB_FOLDER), exclude=ASSET_EXCLUDES)],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
            retain_on_delete=False,
            log_group=self._create_log_group("SiteFilesLogs"),
        )

    # --- Outputs ---------------------------------------------------------

    def _create_outputs(self) -> None:
        """Everything a script or a CLI call needs to address this stack."""
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
            "WebUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description=(
                f"The page. Questions go to <WebUrl>{ASK_PATH} with the header X-Andur-Key "
                "and the SHA-256 of the body in x-amz-content-sha256."
            ),
        )
        CfnOutput(
            self,
            "FunctionUrl",
            value=self.function_url.url,
            description=(
                "The function's own address. Set to AWS_IAM: from outside this account "
                "only CloudFront may invoke it, while a role in this account can sign a "
                f"call itself. Ask through <WebUrl>{ASK_PATH}."
            ),
        )
        CfnOutput(
            self,
            "ApiKeyParameterPath",
            value=API_KEY_PARAMETER_PATH,
            description=(
                "Parameter Store path of the workspace keys: <path>/<workspace>, SecureString."
            ),
        )
        CfnOutput(
            self,
            "GuardrailVersion",
            value=self.guardrail_version.ref,
            description=(
                "The guardrail on every answer and the version the function uses, "
                "as <id>|<version>."
            ),
        )
        CfnOutput(
            self,
            "ExcerptGuardrailRef",
            value=self.excerpt_guardrail_version.ref,
            description="The guardrail that masks the sources' excerpts, as <id>|<version>.",
        )
        CfnOutput(
            self,
            "TopicGuardrailRef",
            value=self.topic_guardrail_version.ref,
            description=(
                "The guardrail that checks the answer, without its citations, for the denied "
                "topics, as <id>|<version>."
            ),
        )
        CfnOutput(
            self,
            "BudgetTableName",
            value=self.budget_table.table_name,
            description=(
                "One item per key, found by the SHA-256 of the key: questions used, window, expiry."
            ),
        )

"""The Andur infrastructure stack.

One stack holds everything Andur needs:

* an S3 bucket for the source documents, one prefix per workspace,
* an S3 Vectors bucket and index that hold the embeddings,
* a Bedrock knowledge base that ties the two together, plus the service
  role it assumes,
* a data source that tells the knowledge base which prefix to crawl,
* one key per workspace in Parameter Store, generated during the deploy,
* a DynamoDB table that counts what each key has spent of its budget,
* a Bedrock guardrail on the answering model: refused topics, masked
  personal details, a grounding check; and a second one that masks the
  same details in the excerpts of the sources,
* the answer function (app/handler.py) behind a Function URL,
* a second, private bucket holding the page from web/,
* one CloudFront distribution that serves both: the page from the bucket, and
  /ask from the function URL.

One address for everything. The page and the answer function sit behind the
same distribution, so the page calls /ask on its own origin and there is no
cross-origin request to allow. The function URL is set to AWS_IAM, so an
unsigned call to it is refused: the resource policy grants the CloudFront
service principal alone, and only for this distribution. It is not a wall
against this account — with AWS_IAM, a caller inside the account can be let
through by its own identity policy, so any role here that holds
``lambda:InvokeFunctionUrl`` can sign a call and reach the function past the
distribution. Nobody outside can. The workspace key is checked either way.

Nothing here runs while nobody asks: the function runs only while a question
is in flight, the keys are free standard parameters, the budget table is
billed per request, and CloudFront's permanent free tier covers a demo's
traffic many times over. What an idle month costs is storage, in cents.
"""

import hashlib
import json
from datetime import datetime, timezone
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

# --- Guardrail -------------------------------------------------------------
# One guardrail on the answering model's call, three policies (build step 6).
# Nemotron 3 Super lists Guardrails among its features on the bedrock-runtime
# endpoint (model card in the Bedrock user guide, read 23.09.2026), so the
# guardrail rides on the Converse call instead of a separate ApplyGuardrail.

#: Fixed, so a person can find it in the console. Every setting below may
#: change without replacing it; a change makes a new version instead.
GUARDRAIL_NAME = "andur-answer-guardrail"

#: Topics Andur refuses: (name, definition, examples). The topic filter reads
#: the question and the answer, never the passages (app/handler.py,
#: ``build_content``). A definition is at most 200 characters for the classic
#: tier, an example at most 100 (Bedrock API reference, GuardrailTopicConfig).
#:
#: "People outside the documents" can only be approximated: the filter does
#: not see the documents, so it cannot know who is in them. What it refuses is
#: a question about public figures or about a person's private life; a
#: question for a named colleague's role passes, and an answer about somebody
#: the passages do not mention is what the grounding check below withholds.
DENIED_TOPICS = (
    (
        "Legal advice",
        "Requests for advice on a legal matter: rights, contracts, liability, disputes, lawsuits, "
        "or what the law requires a person or a business to do.",
        ["Can I sue my landlord over this?", "Is this non-compete clause enforceable?",
         "What are my rights if I am dismissed?"],
    ),
    (
        "Tax advice",
        "Requests for advice on taxes: how to file, declare, deduct, reduce or avoid tax, "
        "or how tax law applies to a person or a business.",
        ["Can I deduct my home office from my taxes?", "How do I pay less VAT?",
         "Do I have to declare this income?"],
    ),
    (
        "Medical advice",
        "Requests for advice on health: symptoms, diagnoses, treatments, medicines or doses for a person.",
        ["What should I take for a headache?", "Is this rash serious?", "How much ibuprofen can I take?"],
    ),
    (
        "People outside the documents",
        "Questions about celebrities, politicians or other public figures, or about the private life, "
        "family, health, beliefs or home of a named person.",
        ["Who is the richest person in the world?", "Is our CEO married?", "Where does my colleague live?"],
    ),
)

#: Personal details masked in the answer: the model reads them in the
#: passages and writes them, the guardrail replaces each with its type, such
#: as ``{PHONE}``. The question is not checked (``inputEnabled`` false): a
#: question may name a person, and an input check is billed for nothing.
MASKED_PII_TYPES = ("NAME", "EMAIL", "PHONE", "ADDRESS")

#: Contextual grounding: an answer scoring below either threshold is withheld.
#:
#: The Bedrock user guide sets the range at 0 to 0.99, says that raising a
#: threshold blocks more ungrounded and irrelevant content, and works its
#: example with 0.7 for both (contextual grounding page, read 23.09.2026).
#: AWS names no default. The launch post adds that an application with a
#: low tolerance for inaccurate content may need a high threshold (AWS News
#: Blog, "Guardrails for Amazon Bedrock can now detect hallucinations",
#: read 23.09.2026). Andur's tolerance is low, but its answers are short and
#: cite by number, and a threshold set too high withholds answers that are
#: right. So 0.7, the documented example, as the starting point; every
#: answered log line carries both scores, which is what moves it later.
GROUNDING_THRESHOLD = 0.7
RELEVANCE_THRESHOLD = 0.7

#: What a caller reads instead of a refused question or a withheld answer.
#: At most 500 characters each.
BLOCKED_INPUT_MESSAGE = (
    "Andur answers questions about this company's documents. It does not give legal, tax or "
    "medical advice, and it does not answer questions about people's private lives or public figures."
)
BLOCKED_OUTPUT_MESSAGE = (
    "Andur withheld this answer: the documents do not carry it, or it strays into advice Andur does not give."
)

#: The second guardrail: the excerpts of the sources. An excerpt is the
#: opening of a passage as the documents have it, and the answer's guardrail
#: never sees it. This one masks the same personal details and does nothing
#: else — no topics, so an excerpt about VAT is not refused as tax advice, and
#: no grounding, which has nothing to check an excerpt against. One policy
#: is one price: sensitive information filters only.
EXCERPT_GUARDRAIL_NAME = "andur-excerpt-guardrail"

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

# --- Prices ----------------------------------------------------------------
# What the handler multiplies the token counts and guardrail units by to put
# cost_usd on every answer. USD per 1,000, us-east-1, standard tier. Read off
# the Amazon Bedrock pricing page (aws.amazon.com/bedrock/pricing) on
# 23.09.2026: NVIDIA Nemotron 3 Super 120B A12B, 0.15 USD per million input
# and 0.65 USD per million output tokens; Guardrails, per 1,000 text units of
# up to 1,000 characters, denied topics 0.15 USD, sensitive information
# filters 0.10 USD, contextual grounding checks 0.10 USD. A price changes on
# AWS's side without a deploy here, which is why PRICE_DATE travels with
# every answer: a cost is only as current as the day in front of it.
PRICE_DATE = "2026-09-23"
PRICE_INPUT_PER_1K_TOKENS = "0.00015"
PRICE_OUTPUT_PER_1K_TOKENS = "0.00065"
PRICE_TOPIC_PER_1K_UNITS = "0.15"
PRICE_PII_PER_1K_UNITS = "0.10"
PRICE_GROUNDING_PER_1K_UNITS = "0.10"

#: Every workspace that gets a key. A workspace without a key cannot be asked,
#: so adding a company means adding it here and deploying.
WORKSPACES = ("quillmoor", "pellwick")

#: Parameter Store path of the keys: one SecureString per workspace, named
#: ``<path>/<workspace>``.
API_KEY_PARAMETER_PATH = "/andur/api-keys"

#: The property that makes every deploy ask again whether a key is there.
#:
#: CloudFormation calls a custom resource only when its properties or its
#: service token change. Both are constants for the key resources, so with
#: nothing else in the properties a deploy would walk past the key generator
#: without calling it — and the documented way to end a key (delete its
#: parameter, deploy) would leave the workspace with no key at all rather than
#: a new one. This value is the moment of the synthesis, so it differs on
#: every `cdk synth` and the Update always runs.
#:
#: The price is that `cdk diff` reports this property as changed on every run,
#: and two key functions are invoked per deploy. The call is a no-op while the
#: parameter is there: ``ensure_key`` writes with ``Overwrite=False`` and
#: throws its fresh key away when the store refuses.
DEPLOY_PROPERTY = "SynthesisedAt"

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
#: 171 seconds, checked by a test), so a hanging Bedrock call ends as a 502
#: with a reason. Far below the 15 minutes a Function URL call may take.
ANSWER_TIMEOUT_SECONDS = 180

#: Lambda gives CPU in proportion to memory; 512 MB keeps the cold start with
#: boto3 short, and a question takes a few seconds of it.
ANSWER_MEMORY_MB = 512

#: At most this many requests run at the same time; Lambda answers the rest
#: with 429 before any code runs. The page is open to anyone who finds it, so
#: this is the hard cap on Lambda time and with it on model calls per second,
#: whatever arrives — with a valid key or not. What one key may spend in total
#: is the budget below; this is only the rate at which anything at all can
#: arrive.
#:
#: The price is that five concurrent calls from a stranger hold the service
#: up: Lambda answers everyone else with 429, and a demo that costs nothing
#: is unavailable until they stop. Taken on purpose — capping the bill matters
#: more here than keeping a demo reachable, and a WAF on the distribution is
#: the answer if it ever becomes a real problem. One distribution in front of
#: everything is what makes that a single place to add it.
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

# --- The page (build step 5) -----------------------------------------------
# One address for everything: CloudFront serves the page out of a private
# bucket and forwards one path to the answer function. Same origin for both,
# so the page needs no cross-origin permission, and the function URL needs no
# way in of its own.

#: The folder whose files are the page. Everything in it is uploaded to the
#: site bucket during a deploy, minus `ASSET_EXCLUDES` — so web/README.md is
#: documentation, not a published file.
WEB_FOLDER = REPOSITORY_ROOT / "web"

#: The one path CloudFront does not answer from the bucket. A page loaded
#: from the distribution reaches the function by posting to this path.
ASK_PATH = "/ask"

#: The file CloudFront serves for the bare address.
SITE_ROOT_DOCUMENT = "index.html"

#: How long CloudFront waits for the answer function before it gives the
#: browser a 504 of its own. The quota "Response timeout per origin" is
#: 1-120 seconds and anything above needs an approved increase (CloudFront
#: quotas page, read 17.09.2026), so this is the whole of what an account
#: has without asking. It is shorter than the function's own 180 seconds:
#: an answer that took longer would reach the browser as a 504 while the
#: function ran it to the end. Answers take 0.7 to 3 seconds today.
ORIGIN_RESPONSE_TIMEOUT_SECONDS = 120

#: What the page is allowed to load and to reach, sent with every response.
#:
#: ``default-src 'none'`` is the floor: nothing loads unless a directive below
#: allows it. The page needs exactly three things — its own script, its own
#: stylesheet and a POST to its own origin — plus the empty ``data:`` icon in
#: index.html, which is an image as far as the browser is concerned. No
#: inline script and no inline style, so neither needs an exception; adding
#: one to the page would break the page and not the rule.
#:
#: ``frame-ancestors 'none'`` is what keeps the page out of somebody else's
#: frame, where a key typed into it would be typed into their page.
#: ``form-action 'none'``: the one form is a form for the keyboard's sake —
#: its submit is always intercepted, and nothing here posts a form anywhere.
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

#: How long a browser is told to reach this address over HTTPS only. Two
#: years is what the HSTS preload list asks for; the list itself is not asked
#: for here, since the address is a CloudFront domain and not ours to submit.
HSTS_MAX_AGE_DAYS = 730


def synthesised_at() -> str:
    """The moment of this synthesis, as a value for `DEPLOY_PROPERTY`.

    Read by a human in a `cdk diff`, and different on every run — which is the
    whole point: it is what makes CloudFormation call the key resources.
    """
    return datetime.now(timezone.utc).isoformat()


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
        self.guardrail, self.guardrail_version = self._create_guardrail()
        self.excerpt_guardrail, self.excerpt_guardrail_version = self._create_excerpt_guardrail()
        self.answer_role = self._create_answer_role(
            self.knowledge_base,
            self.budget_table,
            self.answer_log_group,
            self.guardrail,
            self.guardrail_version,
            self.excerpt_guardrail,
        )
        self.answer_function = self._create_answer_function(
            self.knowledge_base,
            self.budget_table,
            self.answer_role,
            self.answer_log_group,
            self.guardrail_version,
            self.excerpt_guardrail_version,
        )
        self.function_url = self._create_function_url(self.answer_function, self.api_keys)

        self.site_bucket = self._create_site_bucket()
        self.distribution = self._create_distribution(self.site_bucket, self.function_url)
        self.invoke_permission = self._allow_cloudfront_to_invoke(
            self.answer_function, self.distribution
        )
        self.site_files = self._upload_the_page(self.site_bucket, self.distribution)

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

        Every deploy calls the function, because `DEPLOY_PROPERTY` changes
        with each synthesis. That is what the documented way to end a key
        rests on: delete the parameter, deploy, and the workspace gets a key
        again — a new one, hashing differently, so the old key's budget item
        no longer belongs to anybody.
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
        stamp = synthesised_at()
        return [
            CustomResource(
                self,
                f"ApiKey-{workspace}",
                service_token=provider.service_token,
                resource_type="Custom::AndurApiKey",
                properties={
                    "ParameterName": f"{API_KEY_PARAMETER_PATH}/{workspace}",
                    "ParameterPath": API_KEY_PARAMETER_PATH,
                    # Without a property that changes, CloudFormation would
                    # never call this resource again (`DEPLOY_PROPERTY`).
                    DEPLOY_PROPERTY: stamp,
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

    # --- Guardrail -------------------------------------------------------

    def _guardrail_with_version(
        self, construct_id: str, label: str, properties: dict[str, Any]
    ) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
        """A guardrail from ``properties``, and a numbered version that follows every one of them.

        The function names a version, never ``DRAFT``: the draft is whatever
        the guardrail's settings are this minute, including an edit made in
        the console. A version is a frozen copy — and CloudFormation creates
        one only when the version resource is created. Its description is
        create-only (CloudFormation template reference,
        AWS::Bedrock::GuardrailVersion, read 23.09.2026), so it carries a
        digest: change a setting and the description changes, the version is
        replaced, and the function gets the new number in the same deploy.

        The digest is taken over ``properties`` themselves, the objects the
        guardrail is built from, resolved to the JSON CloudFormation gets. A
        digest over a hand-picked summary of them missed every field the
        summary left out: a changed type, action or enabled flag reached the
        draft and never the version.
        """
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

    def _create_guardrail(self) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
        """The guardrail on every answer, and the numbered version the function uses."""
        return self._guardrail_with_version(
            "AnswerGuardrail",
            "answer",
            {
                "name": GUARDRAIL_NAME,
                "description": "Andur: refuses advice topics, masks personal details, withholds ungrounded answers.",
                "blocked_input_messaging": BLOCKED_INPUT_MESSAGE,
                "blocked_outputs_messaging": BLOCKED_OUTPUT_MESSAGE,
                "topic_policy_config": bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                    topics_config=[
                        bedrock.CfnGuardrail.TopicConfigProperty(
                            name=name,
                            definition=definition,
                            examples=examples,
                            type="DENY",
                            input_enabled=True,
                            input_action="BLOCK",
                            output_enabled=True,
                            output_action="BLOCK",
                        )
                        for name, definition, examples in DENIED_TOPICS
                    ]
                ),
                "sensitive_information_policy_config": masking_policy(),
                "contextual_grounding_policy_config": bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(
                    filters_config=[
                        bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                            type="GROUNDING", threshold=GROUNDING_THRESHOLD, action="BLOCK", enabled=True
                        ),
                        bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                            type="RELEVANCE", threshold=RELEVANCE_THRESHOLD, action="BLOCK", enabled=True
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

    # --- Answer function -------------------------------------------------

    @property
    def answer_model_arn(self) -> str:
        return f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{ANSWER_MODEL_ID}"

    def _create_answer_role(
        self,
        knowledge_base: bedrock.CfnKnowledgeBase,
        budget_table: dynamodb.TableV2,
        log_group: logs.LogGroup,
        guardrail: bedrock.CfnGuardrail,
        guardrail_version: bedrock.CfnGuardrailVersion,
        excerpt_guardrail: bedrock.CfnGuardrail,
    ) -> iam.Role:
        """The execution role of the answer function: six things, each on one resource.

        Read the workspace keys, count a question against its key's budget,
        retrieve from this knowledge base, invoke the answering model
        (``converse`` is authorised as ``InvokeModel``), apply its guardrail,
        write its own log.

        The model may only be invoked through the guardrail's deployed
        version: the ``bedrock:GuardrailIdentifier`` condition refuses a call
        without it, and applying the guardrail is its own permission on the
        guardrail (Bedrock user guide, "Enforce the use of specific
        guardrails", example 1, read 23.09.2026). A later change that drops
        ``guardrailConfig`` from the call fails with AccessDenied instead of
        answering unguarded. Applying the excerpt guardrail is the same
        permission on the second guardrail; it invokes no model.
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
                            conditions={
                                "StringEquals": {
                                    "bedrock:GuardrailIdentifier": (
                                        f"{guardrail.attr_guardrail_arn}:{guardrail_version.attr_version}"
                                    )
                                }
                            },
                        ),
                        iam.PolicyStatement(
                            sid="ApplyAnswerGuardrail",
                            actions=["bedrock:ApplyGuardrail"],
                            resources=[guardrail.attr_guardrail_arn, excerpt_guardrail.attr_guardrail_arn],
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
                "GUARDRAIL_ID": guardrail_version.attr_guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version.attr_version,
                "EXCERPT_GUARDRAIL_ID": excerpt_guardrail_version.attr_guardrail_id,
                "EXCERPT_GUARDRAIL_VERSION": excerpt_guardrail_version.attr_version,
                "PRICE_INPUT_PER_1K_TOKENS": PRICE_INPUT_PER_1K_TOKENS,
                "PRICE_OUTPUT_PER_1K_TOKENS": PRICE_OUTPUT_PER_1K_TOKENS,
                "PRICE_TOPIC_PER_1K_UNITS": PRICE_TOPIC_PER_1K_UNITS,
                "PRICE_PII_PER_1K_UNITS": PRICE_PII_PER_1K_UNITS,
                "PRICE_GROUNDING_PER_1K_UNITS": PRICE_GROUNDING_PER_1K_UNITS,
                "PRICE_DATE": PRICE_DATE,
            },
        )

    def _create_function_url(
        self, function: lambda_.Function, api_keys: list[CustomResource]
    ) -> lambda_.FunctionUrl:
        """The function's own HTTPS address — reachable for CloudFront alone.

        ``AWS_IAM`` and not ``NONE``: every request must be signed with AWS
        credentials, and the only holder of a permission to invoke this URL is
        the CloudFront service principal, restricted to this stack's
        distribution (`_allow_cloudfront_to_invoke`, and the permission the
        origin access control adds). Called from outside the account the URL
        answers 403, whatever key the call carries. Origin access control
        requires ``AWS_IAM``: "To use OAC, you must specify AWS_IAM as the
        value for the AuthType parameter" (CloudFront developer guide,
        "Restrict access to an AWS Lambda function URL origin", read
        17.09.2026).

        Inside the account it is not a wall. AWS_IAM lets a caller in the
        same account be authorised by its own identity policy, without the
        resource policy saying anything: a role with ``lambda:*``, an
        administrator, the deploy role, a CI profile, can sign a request and
        reach the function directly — past the distribution, and so past
        anything later put in front of it. What that skips is the front door,
        not the lock: the workspace key, its workspace and its budget are
        checked in the function, on every call, wherever it came from.

        The workspace key is no weaker for it — it is still the whole of what
        decides which workspace a question may read. It only travels one layer
        further in, in a header of its own (`KEY_HEADER` in app/handler.py).

        The URL is created only after every key exists, so no question can
        arrive before the workspace it names has a key.
        """
        url = function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)
        for api_key in api_keys:
            url.node.add_dependency(api_key)
        return url

    # --- The page --------------------------------------------------------

    def _create_site_bucket(self) -> s3.Bucket:
        """The bucket the page is served from — private, and reachable only through CloudFront.

        The same shape as the documents bucket minus the two things that only
        matter for documents: no versioning, because every file here is a copy
        of what is in web/ in the repository, and ``DESTROY`` rather than
        ``RETAIN``, because nothing in it is a source of truth. No website
        configuration either: "Amazon S3 website endpoints do not support
        HTTPS or access points" (S3 user guide, "Website endpoints", read
        17.09.2026), and they serve only publicly readable content. So the
        bucket stays a plain private bucket and CloudFront reads it through
        an origin access control.
        """
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
        """The headers every response carries, page and answer alike.

        The distribution is the one place where these cost nothing: no code
        runs for them, and neither origin has to set them — S3 would need
        them stored per object, and the function would have to repeat them in
        every reply. `CONTENT_SECURITY_POLICY` says what the page may load,
        the rest closes the three doors a page that holds a key in
        ``localStorage`` should not leave open: a downgrade to HTTP, a guess
        at a content type, and an address leaking in a Referer header.

        ``override=True`` on each: an origin that sends its own value does
        not get to weaken these.
        """
        return cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
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
        """One address for everything: the page from the bucket, `ASK_PATH` from the function.

        Two origins, each with an origin access control of its own, so neither
        the bucket nor the function URL is reachable without going through
        this distribution. Everything is HTTPS: the page redirects a plain
        HTTP request, `ASK_PATH` refuses one, because a redirect would turn a
        browser's POST into a GET and lose the question with it.

        The `ASK_PATH` behaviour keeps nothing and forwards everything:
        answers differ per question and per key, so caching them would be
        wrong before it was useful, and the request policy has to carry
        ``X-Andur-Key``, ``Content-Type`` and ``x-amz-content-sha256`` through
        to the function. ``ALL_VIEWER_EXCEPT_HOST_HEADER`` is the managed
        policy that passes every viewer header but ``Host`` — which must stay
        CloudFront's own, because the origin access control signs the request
        for the function URL's host.
        """
        headers = self._create_security_headers()
        return cloudfront.Distribution(
            self,
            "SiteDistribution",
            comment="Andur: the page, and /ask on its way to the answer function.",
            default_root_object=SITE_ROOT_DOCUMENT,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                response_headers_policy=headers,
            ),
            additional_behaviors={
                ASK_PATH: cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin.with_origin_access_control(
                        function_url,
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
        """The second of the two permissions the CloudFront documentation asks for.

        Its "Restrict access to an AWS Lambda function URL origin" page grants
        the CloudFront service principal both ``lambda:InvokeFunctionUrl`` and
        ``lambda:InvokeFunction``, each with the distribution as
        ``source-arn``. The origin access control construct adds the first by
        itself (``InvokeFromApiFor<origin>`` in the template); this is the
        second. Both are bound to this one distribution, so no other
        distribution — in this account or any other — can reach the function.

        Created after the distribution and pointing at it: the distribution
        needs only the function URL's domain name, never this permission, so
        there is no cycle.
        """
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
        """Put web/ into the site bucket as part of the deploy, and clear the caches.

        Without the invalidation a changed page would keep being served from
        the edge caches until they expired on their own. One invalidation per
        deploy of ``/*`` is one path against the 1,000 a month that cost
        nothing.

        ``retain_on_delete=False`` is what lets ``cdk destroy`` work: the
        bucket is emptied when this resource goes, and CloudFormation deletes
        it before the bucket it depends on. Without it the bucket would still
        hold the page and its deletion would fail. ``prune`` then also removes
        a file that is no longer in web/.

        The copying is done by a function CDK brings with the construct. Like
        the key generator, it runs during a deploy and never afterwards, so it
        adds nothing to what an idle month costs.
        """
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
            description="Parameter Store path of the workspace keys: <path>/<workspace>, SecureString.",
        )
        CfnOutput(
            self,
            "GuardrailVersion",
            value=self.guardrail_version.ref,
            description="The guardrail on every answer and the version the function uses, as <id>|<version>.",
        )
        CfnOutput(
            self,
            "ExcerptGuardrailRef",
            value=self.excerpt_guardrail_version.ref,
            description="The guardrail that masks the sources' excerpts, as <id>|<version>.",
        )
        CfnOutput(
            self,
            "BudgetTableName",
            value=self.budget_table.table_name,
            description="One item per key, found by the SHA-256 of the key: questions used, window, expiry.",
        )

"""The Andur infrastructure stack: its parts wired together, and its outputs."""

from aws_cdk import (
    CfnOutput,
    Stack,
    Token,
    aws_cloudfront as cloudfront,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
)
from constructs import Construct

from app.page import ASK_PATH
from infra.answer_models import (
    DEFAULT_REGION,
    answer_guardrail_uses_profile,
    answer_model,
    region_models,
)
from infra.stacks.answer_function import (
    ANSWER_FUNCTION_NAME,
    ANSWER_MAX_CONCURRENCY,
    ANSWER_MODEL_ID,
    API_KEY_PARAMETER_PATH,
    create_answer_function,
    create_answer_role,
    create_budget_table,
    create_deploy_drain,
    create_function_url,
    create_key_retirer,
    create_page_layer,
)
from infra.stacks.guardrails import (
    create_answer_guardrail,
    create_excerpt_guardrail,
    create_topic_guardrail,
)
from infra.stacks.knowledge_base import (
    WORKSPACE_PREFIX,
    create_data_source,
    create_documents_bucket,
    create_knowledge_base,
    create_knowledge_base_role,
    create_vector_store,
)
from infra.stacks.shared import create_log_group
from infra.stacks.web import (
    allow_cloudfront_to_invoke,
    create_distribution,
    create_site_bucket,
    upload_the_page,
)


class AndurStack(Stack):
    """Everything Andur runs on, in one stack."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        answer_model_id: str = ANSWER_MODEL_ID,
        topic_model_id: str | None = None,
        answer_guardrail_profile: bool = True,
        answer_max_concurrency: int | None = ANSWER_MAX_CONCURRENCY,
        behind_cloudfront: bool = True,
        **kwargs,
    ) -> None:
        """``topic_model_id`` None: the topic guardrail checks the denied topics, through the
        EU guardrail profile. A model id: that model checks them in the stack's region, and there
        is no topic guardrail. ``answer_guardrail_profile``: whether the answer guardrail runs
        through the profile too; the excerpt guardrail never does. ``behind_cloudfront`` False:
        no CloudFront, no site bucket; the function's URL is open and serves the page too."""
        super().__init__(scope, construct_id, **kwargs)
        # Without a set region (the tests) the prices are the default region's.
        self.price_region = DEFAULT_REGION if Token.is_unresolved(self.region) else self.region
        self.models = region_models(self.price_region)
        self.answer_model_id = answer_model_id
        self.answer_model = answer_model(answer_model_id, self.price_region)
        self.topic_model_id = topic_model_id
        self.topic_model = (
            None if topic_model_id is None else answer_model(topic_model_id, self.price_region)
        )
        self.answer_guardrail_profile = answer_guardrail_uses_profile(
            answer_guardrail_profile, topic_model_id
        )
        self.answer_max_concurrency = answer_max_concurrency
        self.behind_cloudfront = behind_cloudfront

        self.documents_bucket = create_documents_bucket(self)
        self.vector_bucket, self.vector_index = create_vector_store(self)
        self.knowledge_base_role = create_knowledge_base_role(
            self, self.documents_bucket, self.vector_index
        )
        self.knowledge_base = create_knowledge_base(
            self, self.vector_index, self.knowledge_base_role
        )
        self.data_source = create_data_source(self, self.knowledge_base, self.documents_bucket)

        self.key_retirer = create_key_retirer(self)
        self.budget_table = create_budget_table(self)
        self.answer_log_group = create_log_group(
            self, "AnswerFunctionLogs", f"/aws/lambda/{ANSWER_FUNCTION_NAME}"
        )
        self.guardrail, self.guardrail_version = create_answer_guardrail(self)
        self.excerpt_guardrail, self.excerpt_guardrail_version = create_excerpt_guardrail(self)
        self.topic_guardrail, self.topic_guardrail_version = (
            create_topic_guardrail(self) if topic_model_id is None else (None, None)
        )
        guardrail_versions = [
            version
            for version in (
                self.guardrail_version,
                self.excerpt_guardrail_version,
                self.topic_guardrail_version,
            )
            if version is not None
        ]
        self.answer_role = create_answer_role(
            self,
            self.knowledge_base,
            self.budget_table,
            self.answer_log_group,
            self.guardrail,
            self.excerpt_guardrail,
            self.topic_guardrail,
        )
        self.page_layer = None if behind_cloudfront else create_page_layer(self)
        self.answer_function = create_answer_function(
            self,
            self.knowledge_base,
            self.budget_table,
            self.answer_role,
            self.answer_log_group,
            self.guardrail_version,
            self.excerpt_guardrail_version,
            self.topic_guardrail_version,
            self.page_layer,
        )
        self.function_url = create_function_url(self, self.answer_function)
        self.deploy_drain = create_deploy_drain(self, self.answer_function, guardrail_versions)

        self.site_bucket: s3.Bucket | None = None
        self.distribution: cloudfront.Distribution | None = None
        self.invoke_permission: lambda_.CfnPermission | None = None
        self.site_files: s3_deployment.BucketDeployment | None = None
        if behind_cloudfront:
            self.site_bucket = create_site_bucket(self)
            self.distribution = create_distribution(self, self.site_bucket, self.function_url)
            self.invoke_permission = allow_cloudfront_to_invoke(
                self, self.answer_function, self.distribution
            )
            self.site_files = upload_the_page(self, self.site_bucket, self.distribution)

        self._create_outputs()

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
        if self.distribution is not None:
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
        else:
            CfnOutput(
                self,
                "WebUrl",
                value=self.function_url.url,
                description=(
                    "The page, served by the answer function at its own URL in this region, "
                    f"without CloudFront. Questions go to <WebUrl>{ASK_PATH.lstrip('/')} with "
                    "the header X-Andur-Key."
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
        if self.topic_guardrail_version is not None:
            CfnOutput(
                self,
                "TopicGuardrailRef",
                value=self.topic_guardrail_version.ref,
                description=(
                    "The guardrail that checks the answer, without its citations, for the "
                    "denied topics, as <id>|<version>."
                ),
            )
        else:
            CfnOutput(
                self,
                "TopicModelId",
                value=self.topic_model_id,
                description=(
                    "The model that checks the question and the answer for the denied topics, "
                    "in this region."
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

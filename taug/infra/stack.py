"""Taug's stack: what Svar, and later Vard, stand on."""

from aws_cdk import CfnOutput, Stack
from constructs import Construct

from taug.infra.keys import API_KEY_PARAMETER_PATH, create_key_retirer
from taug.infra.knowledge_base import (
    WORKSPACE_PREFIX,
    create_data_source,
    create_documents_bucket,
    create_knowledge_base,
    create_knowledge_base_role,
    create_vector_store,
)

#: The stack's description. It stays the same while the documents, the vectors and the
#: knowledge base move in, because a move may change nothing else in the template.
TAUG_DESCRIPTION = "Taug: documents, vectors, knowledge base and keys, what Andur's parts stand on."


class TaugStack(Stack):
    """Taug's part of Andur: the documents, the vectors, the knowledge base over them, and
    the key retirer. Svar's stack (svar/infra/stack.py) depends on it and reads the knowledge
    base from it.

    The construct ids are the ones these resources had in Andur's one stack, so their
    logical ids are the same and `cdk refactor` moves them instead of building them anew
    (docs/decisions.md)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
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
        self._create_outputs()

    def _create_outputs(self) -> None:
        """Everything a script or a CLI call needs to address Taug's part."""
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
            "ApiKeyParameterPath",
            value=API_KEY_PARAMETER_PATH,
            description=(
                "Parameter Store path of the workspace keys: <path>/<workspace>, SecureString."
            ),
        )

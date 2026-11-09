"""Taug's stack: what Svar, and later Vard, stand on."""

from aws_cdk import Stack
from constructs import Construct

from taug.infra.keys import create_key_retirer

#: The stack's description. It stays the same while the documents, the vectors and the
#: knowledge base move in, because a move may change nothing else in the template.
TAUG_DESCRIPTION = "Taug: documents, vectors, knowledge base and keys, what Andur's parts stand on."


class TaugStack(Stack):
    """Taug's part of Andur. Svar's stack (svar/infra/stack.py) depends on it."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.key_retirer = create_key_retirer(self)

"""The errors that end a request, each with its HTTP status and a sentence for the caller."""

from collections.abc import Mapping

import sentences


class HandlerError(Exception):
    """Ends a request with an HTTP status; the message is one plain sentence.

    The message is English, for the log. ``german`` is the same sentence for a caller
    who asked in German; an error raised before the body is read, when the language
    is not known yet, has none.
    """

    status = 500
    #: Extra response headers this status needs.
    headers: Mapping[str, str] = {}

    def __init__(self, message: str, german: str | None = None) -> None:
        super().__init__(message)
        self.german = german
        #: Whether the question was given back to the key's budget (`refund_question`).
        self.refunded = False

    def public_message(self, language: str = sentences.DEFAULT_LANGUAGE) -> str:
        """What the caller reads, in the language of the request."""
        message = self.german if language == "de" and self.german else str(self)
        return (
            message + sentences.in_language(sentences.REFUNDED_NOTES, language)
            if self.refunded
            else message
        )


class BadRequest(HandlerError):
    status = 400


class Unauthorized(HandlerError):
    """No key, or not the key of the requested workspace."""

    status = 401


class BudgetSpent(HandlerError):
    """A valid key for the right workspace, with nothing left to spend."""

    status = 402


class QuotaReached(HandlerError):
    """A key on a plan that has used its questions for this month or for today.

    Not 402, which says nothing is left for good: this quota comes back by itself
    with the next day or month, and that is what 429 says.
    """

    status = 429


class MethodNotAllowed(HandlerError):
    status = 405
    headers = {"Allow": "POST"}


class ConfigurationError(HandlerError):
    """The function itself is set up wrong: an environment variable, the keys."""

    status = 500

    def public_message(self, language: str = sentences.DEFAULT_LANGUAGE) -> str:
        return sentences.in_language(sentences.NOT_CONFIGURED_MESSAGES, language)


class RetrievalError(HandlerError):
    """The knowledge base could not be asked."""

    status = 502


class LLMError(HandlerError):
    """The answering model could not be reached or gave no usable answer."""

    status = 502


class TopicCheckError(HandlerError):
    """The question or the answer could not be checked for the denied topics, so the question
    is not answered, or the answer not given."""

    status = 502

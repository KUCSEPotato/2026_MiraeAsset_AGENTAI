from app.domain.models import ExecutionErrorCode


class RetrievalError(Exception):
    error_code = ExecutionErrorCode.RETRIEVAL_FAILED


class RetrieverUnavailableError(RetrievalError):
    """Expected backend availability failure."""


class RetrieverNotRegisteredError(RetrievalError):
    error_code = ExecutionErrorCode.RETRIEVER_NOT_REGISTERED


class RDBQueryCompilationError(RetrievalError):
    """A structured RDB step used a non-allow-listed field or operation."""


class GraphQueryCompilationError(RetrievalError):
    """A graph step used a non-allow-listed relation or traversal shape."""

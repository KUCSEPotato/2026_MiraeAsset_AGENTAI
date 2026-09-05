class SemanticParserError(Exception):
    """Expected external parser failure that must fail closed."""

    def __init__(
        self,
        message: str = "semantic parser dependency failed",
        *,
        failure_reason: str = "semantic_parse_dependency_failure",
        http_status: int | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.failure_reason = failure_reason
        self.http_status = http_status
        self.provider_code = provider_code
        self.request_id = request_id
        super().__init__(message)


class SemanticCandidateValidationError(ValueError):
    """Untrusted LLM output failed deterministic semantic validation."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(dict.fromkeys(reasons))
        super().__init__("semantic candidate validation failed")


class SemanticParseSafetyError(ValueError):
    """An incomplete rule parse could not be replaced by a safe complete parse."""

    def __init__(
        self,
        reason: str,
        *,
        parser: str = "llm_fallback",
        rule_latency_ms: float = 0.0,
        llm_latency_ms: float = 0.0,
    ) -> None:
        self.reason = reason
        self.parser = parser
        self.rule_latency_ms = rule_latency_ms
        self.llm_latency_ms = llm_latency_ms
        super().__init__("semantic parsing could not be completed safely")

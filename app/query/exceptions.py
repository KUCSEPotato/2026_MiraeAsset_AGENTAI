class SemanticParserError(Exception):
    """Expected external parser failure that must fail closed."""


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

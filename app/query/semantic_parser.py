from __future__ import annotations

import logging
from time import perf_counter

from app.domain.models import (
    ParseProvenance,
    ParsedQuery,
    ParserSource,
    SemanticCoverageStatus,
)
from app.query.exceptions import (
    SemanticCandidateValidationError,
    SemanticParseSafetyError,
    SemanticParserError,
)
from app.query.llm_parser import (
    PROMPT_VERSION,
    SEMANTIC_SCHEMA_VERSION,
    SemanticParserLLM,
)
from app.query.semantic_models import SemanticParserRequest
from app.query.semantic_validation import LLMSemanticCandidateValidator


logger = logging.getLogger(__name__)


class SemanticParserCoordinator:
    """Rule-first analyzer with one fail-closed LLM fallback attempt."""

    def __init__(
        self,
        *,
        rule_parser,
        llm_parser: SemanticParserLLM | None,
        candidate_validator: LLMSemanticCandidateValidator,
        compact_vocabulary: dict[str, list[str]],
    ) -> None:
        self._rule_parser = rule_parser
        self._llm_parser = llm_parser
        self._candidate_validator = candidate_validator
        self._compact_vocabulary = compact_vocabulary

    async def analyze(self, question: str) -> ParsedQuery:
        rule_started = perf_counter()
        rule_result = await self._rule_parser.analyze(question)
        rule_latency = _milliseconds(rule_started)
        descriptive_fallback = (
            self._llm_parser is not None
            and rule_result.requires_semantic_search
            and bool(rule_result.semantic_terms)
        )
        if _is_complete(rule_result) and not descriptive_fallback:
            parsed = rule_result.model_copy(
                update={
                    "parser_source": ParserSource.RULE,
                    "parse_provenance": ParseProvenance(
                        parser_source=ParserSource.RULE,
                        semantic_schema_version=SEMANTIC_SCHEMA_VERSION,
                        rule_latency_ms=rule_latency,
                        validation_status="not_required",
                    ),
                }
            )
            logger.info(
                "semantic parse complete",
                extra={
                    "parser_path": "RULE",
                    "rule_latency_ms": rule_latency,
                    "constraint_count": len(parsed.semantic_constraints),
                    "unparsed_count": 0,
                },
            )
            return parsed

        if self._llm_parser is None:
            raise SemanticParseSafetyError(
                "llm_fallback_not_configured",
                rule_latency_ms=rule_latency,
            )

        request = SemanticParserRequest(
            original_question=question,
            rule_parse=_rule_hint(rule_result),
            compact_vocabulary=self._compact_vocabulary,
            semantic_schema_version=SEMANTIC_SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
        )
        llm_started = perf_counter()
        try:
            candidate = await self._llm_parser.parse(request)
            llm_latency = _milliseconds(llm_started)
            parsed = self._candidate_validator.validate(
                question,
                rule_result,
                candidate,
                model=self._llm_parser.model_name,
                rule_latency_ms=rule_latency,
                llm_latency_ms=llm_latency,
                prompt_version=PROMPT_VERSION,
                schema_version=SEMANTIC_SCHEMA_VERSION,
            )
        except SemanticParserError as exc:
            raise SemanticParseSafetyError(
                "llm_dependency_failure",
                rule_latency_ms=rule_latency,
                llm_latency_ms=_milliseconds(llm_started),
            ) from exc
        except SemanticCandidateValidationError as exc:
            raise SemanticParseSafetyError(
                "llm_candidate_rejected",
                rule_latency_ms=rule_latency,
                llm_latency_ms=_milliseconds(llm_started),
            ) from exc

        logger.info(
            "semantic parse complete",
            extra={
                "parser_path": "LLM_FALLBACK",
                "rule_latency_ms": rule_latency,
                "llm_latency_ms": parsed.parse_provenance.llm_latency_ms,
                "constraint_count": len(parsed.semantic_constraints),
                "unparsed_count": len(parsed.unparsed_material_spans),
                "validation_status": "accepted",
            },
        )
        return parsed


def _is_complete(parsed: ParsedQuery) -> bool:
    return (
        parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
        and not parsed.unparsed_material_spans
        and not parsed.unsupported_constraint_ids
    )


def _rule_hint(parsed: ParsedQuery) -> dict[str, object]:
    return {
        "intent": parsed.intent.value,
        "recognized_constraints": [
            {
                "raw_text": item.raw_text,
                "start": item.source_span.start,
                "end": item.source_span.end,
                "semantic_type": item.semantic_type.value,
                "status": item.status.value,
            }
            for item in parsed.semantic_constraints
        ],
        "unparsed_material_spans": [
            {
                "raw_text": item.raw_text,
                "start": item.source_span.start,
                "end": item.source_span.end,
            }
            for item in parsed.unparsed_material_spans
        ],
    }


def _milliseconds(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 3)

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
    """Rule-first analyzer with a validated LLM candidate and at most one repair."""

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
        if (_is_complete(rule_result) or _is_understood_unsupported(rule_result)) and not descriptive_fallback:
            parsed = rule_result.model_copy(
                update={
                    "parser_source": ParserSource.RULE,
                    "parse_provenance": ParseProvenance(
                        parser_source=ParserSource.RULE,
                        default_policies=rule_result.parse_provenance.default_policies,
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
        logger.info("semantic fallback required", extra={
            "request_purpose": "semantic_parse", "parser_path": "LLM_FALLBACK",
            "rule_latency_ms": rule_latency,
            "constraint_count": len(rule_result.semantic_constraints),
            "unparsed_count": len(rule_result.unparsed_material_spans),
        })
        llm_started = perf_counter()
        rejected = None
        errors = []
        repair = getattr(self._llm_parser, "repair", None)
        for attempt in range(2):
            candidate = None
            try:
                candidate = (await self._llm_parser.parse(request) if attempt == 0
                             else await repair(request, rejected, errors))
                parsed = self._candidate_validator.validate(
                    question, rule_result, candidate,
                    model=self._llm_parser.model_name,
                    rule_latency_ms=rule_latency,
                    llm_latency_ms=_milliseconds(llm_started),
                    prompt_version=PROMPT_VERSION, schema_version=SEMANTIC_SCHEMA_VERSION,
                )
                parsed = parsed.model_copy(update={"parse_provenance": parsed.parse_provenance.model_copy(
                    update={"llm_calls": attempt + 1, "repair_attempts": attempt},
                )})
                break
            except (SemanticParserError, SemanticCandidateValidationError) as exc:
                if isinstance(exc, SemanticCandidateValidationError):
                    reason = "llm_candidate_rejected"
                    rejected = candidate.model_dump(mode="json", exclude_none=True)
                    errors = exc.reasons
                    logger.warning("semantic parse candidate rejected", extra={
                        "request_purpose": "semantic_parse", "parser_path": "LLM_FALLBACK",
                        "failure_stage": "candidate_validation", "validation_status": "rejected",
                        "candidate_rejection_reasons": errors,
                        "validation_reasons": errors,
                        "validation_reason_count": len(errors),
                        "rule_latency_ms": rule_latency,
                        "llm_latency_ms": _milliseconds(llm_started),
                    })
                else:
                    reason = exc.failure_reason
                    rejected, errors = exc.rejected_candidate, exc.validation_errors
                if attempt == 0 and callable(repair) and reason in {
                    "llm_candidate_rejected", "semantic_parse_response_invalid",
                }:
                    logger.info("semantic repair required", extra={
                        "request_purpose": "semantic_parse", "parser_path": "LLM_REPAIR",
                        "candidate_rejection_reasons": errors,
                    })
                    continue
                raise SemanticParseSafetyError(
                    reason, rule_latency_ms=rule_latency,
                    llm_latency_ms=_milliseconds(llm_started),
                    llm_calls=attempt + 1, repair_attempts=attempt,
                    validation_reasons=errors,
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


def _is_understood_unsupported(parsed: ParsedQuery) -> bool:
    """Known material semantics belong to capability validation, not re-parsing."""
    reasons = {item.unsupported_reason for item in parsed.semantic_constraints
               if item.status.value == "unsupported"}
    return bool(reasons) and not parsed.unparsed_material_spans and reasons <= {
        "dataset_unit_mapping_unverified",
        "historical_metric_series_unavailable",
        "holdings_weight_projection_unavailable",
        "peer_selector_unverified",
        "projection_unavailable",
        "subjective_execution_unsupported",
        "intent_execution_not_implemented",
        "true_ambiguity:comparison_metric_missing",
    }


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
                "payload": item.payload,
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
        "applied_default_policies": [item.model_dump(mode="json") for item in parsed.parse_provenance.default_policies],
    }


def _milliseconds(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 3)

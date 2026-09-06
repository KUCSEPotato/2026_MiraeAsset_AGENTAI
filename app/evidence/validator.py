from collections import defaultdict

from app.domain.models import (
    AnswerabilityReasonCode,
    AnswerabilityStatus,
    ClauseResult,
    ClauseStatus,
    CoverageStatus,
    ConceptCategory,
    Evidence,
    EvidenceBundle,
    FindingSeverity,
    GroundedQuery,
    GroundedField,
    QueryIntent,
    ResolutionStatus,
    SnapshotPolicy,
    StepExecutionStatus,
    ValidationFinding,
    ValidationResult,
)
from app.evidence.quality import FieldQualityProvider
from app.planning.predicates import structured_predicate
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.serialization import structured_query_inputs
from app.data.metric_capabilities import MetricCapabilityRegistry


_MISSING_LITERALS = {"", "null"}


class QualityAwareEvidenceValidator:
    """Make one deterministic answerability decision from evidence facts."""

    def __init__(self, quality_provider: FieldQualityProvider) -> None:
        self._quality_provider = quality_provider

    async def validate(
        self, query: GroundedQuery, evidence: EvidenceBundle,
    ) -> ValidationResult:
        if not query.grounded_requested_fields and not query.parsed_query.selectors:
            return await self._validate_full(query, evidence)
        from app.planning.output_requirements import prepare_outputs
        from app.planning.capabilities import SemanticCapabilityValidator
        from app.planning.semantic_ir import build_semantic_ir
        try:
            prepared = prepare_outputs(query)
            SemanticCapabilityValidator().validate(prepared.query, build_semantic_ir(prepared.query))
        except UnsupportedQuerySemanticsError as exc:
            return ValidationResult(answerable=False,
                reason_codes=[AnswerabilityReasonCode.UNSUPPORTED_CONSTRAINT], reasons=exc.reasons,
                clauses=[ClauseResult(label=item.raw_text, field=item.canonical_field,
                    status=ClauseStatus.UNSUPPORTED, reason="hard_constraint_or_no_executable_output")
                    for item in query.grounded_requested_fields])

        factual = prepared.query
        comparison_requested = query.parsed_query.comparison is not None or query.parsed_query.intent is QueryIntent.COMPARE_PRODUCTS or bool(query.parsed_query.selectors)
        if comparison_requested and not query.grounded_sort:
            factual = factual.model_copy(update={"parsed_query": factual.parsed_query.model_copy(update={
                "comparison": None, "intent": QueryIntent.SEARCH_PRODUCT,
            })})
        result = await self._validate_full(factual, evidence)
        clauses = [item for item in prepared.disclosures if item.kind != "COMPARISON"]
        hard_fields = {item.canonical_field for item in (*query.grounded_filters, *query.grounded_sort)}
        local_codes = {
            AnswerabilityReasonCode.MISSING_REQUIRED_FIELD, AnswerabilityReasonCode.INVALID_SENTINEL,
            AnswerabilityReasonCode.CONFLICTING_EVIDENCE, AnswerabilityReasonCode.INSUFFICIENT_COVERAGE,
        }
        global_failure = any(item.severity is FindingSeverity.BLOCKING and not (
            item.code in local_codes and item.field is not None and item.field not in hard_fields
        ) for item in result.findings)
        expected = {
            item.canonical_id: item.raw_text for item in factual.resolved_entities
            if item.resolution_status is ResolutionStatus.RESOLVED and item.entity_type == "product" and item.canonical_id
        }
        if not expected or query.grounded_sort:
            expected = {item.entity_id: item.entity_id for item in evidence.evidence if item.entity_id}
        if not expected:
            expected = {None: None}
        clauses = [cell for item in clauses for cell in (
            [item.model_copy(update={"entity_id": entity_id, "entity_label": label})
             for entity_id, label in expected.items()] if item.kind == "OUTPUT" else [item]
        )]
        output_fields = list(factual.grounded_requested_fields)
        output_fields.extend(GroundedField(raw_text=item.raw_sort.field, canonical_field=item.canonical_field,
                                            status=item.status) for item in factual.grounded_sort
                             if item.canonical_field not in {field.canonical_field for field in output_fields})
        for field in output_fields:
            for entity_id, label in expected.items():
                relevant = [item for item in result.findings if item.field == field.canonical_field
                            and item.entity_id in {None, entity_id} and item.severity is FindingSeverity.BLOCKING]
                indices = [index for index, item in enumerate(evidence.evidence)
                           if item.field == field.canonical_field and item.entity_id == entity_id
                           and not self._is_missing(item.value) and not self._is_sentinel(item)]
                ambiguous = any(item.code is AnswerabilityReasonCode.CONFLICTING_EVIDENCE for item in relevant)
                status = (ClauseStatus.AMBIGUOUS if ambiguous else ClauseStatus.MISSING if not indices
                          else ClauseStatus.UNSUPPORTED if relevant or global_failure else ClauseStatus.SATISFIED)
                clauses.append(ClauseResult(label=field.raw_text, field=field.canonical_field,
                    entity_id=entity_id, entity_label=label, status=status,
                    reason=None if status is ClauseStatus.SATISFIED else "output_evidence_unavailable",
                    evidence_indices=indices if status is ClauseStatus.SATISFIED else []))
        # Missing entities/selectors apply to every requested output as well.
        for item in list(clauses):
            if item.kind in {"ENTITY", "SELECTOR"}:
                clauses.extend(ClauseResult(label=field.raw_text, field=field.canonical_field,
                    entity_label=item.label, status=item.status, reason=item.reason)
                    for field in query.grounded_requested_fields)

        comparison_completed = False
        if comparison_requested:
            comparison_findings = self._comparison_contract_findings(query, evidence)
            comparison_findings.extend(self._snapshot_findings(_required_fields(query), True, evidence))
            comparison_findings.extend(self._coverage_findings(_required_fields(query), True, False,
                                                               _query_product_type(query), evidence))
            for finding in comparison_findings:
                if finding.code is AnswerabilityReasonCode.INSUFFICIENT_EVIDENCE and finding.field:
                    for clause in clauses:
                        if clause.kind == "OUTPUT" and clause.field == finding.field and clause.entity_id == finding.entity_id:
                            clause.status = ClauseStatus.UNSUPPORTED
                            clause.reason = "invalid_metric_evidence_contract"
                            clause.evidence_indices = []
            # Even older compare payloads without ComparisonSpec must have a
            # current contract and at least two evidenced resolved identities.
            inputs = structured_query_inputs(query)
            reasons = [MetricCapabilityRegistry().comparison_contract(field, inputs)[1]
                       for field in _required_fields(query)]
            comparison_completed = bool(len(expected) >= 2 and not global_failure
                and not prepared.disclosures and not any(reasons)
                and all(item.status is ClauseStatus.SATISFIED for item in clauses)
                and not any(item.severity is FindingSeverity.BLOCKING for item in comparison_findings))
            clauses.append(ClauseResult(kind="COMPARISON", label="상품 간 비교",
                status=ClauseStatus.SATISFIED if comparison_completed else ClauseStatus.UNSUPPORTED,
                reason=None if comparison_completed else "comparison_not_completed"))
            result.findings.extend(comparison_findings)

        has_facts = any(item.kind == "OUTPUT" and item.status is ClauseStatus.SATISFIED for item in clauses)
        partial = any(item.status is not ClauseStatus.SATISFIED for item in clauses)
        state = (AnswerabilityStatus.UNANSWERABLE if global_failure or not has_facts else
                 AnswerabilityStatus.PARTIALLY_ANSWERABLE if partial else AnswerabilityStatus.FULLY_ANSWERABLE)
        codes = list(dict.fromkeys(item.code for item in result.findings))
        if state is not AnswerabilityStatus.UNANSWERABLE:
            codes.insert(0, AnswerabilityReasonCode.ANSWERABLE)
        return ValidationResult(answerable=state is not AnswerabilityStatus.UNANSWERABLE,
            answerability=state, clauses=clauses, comparison_completed=comparison_completed,
            findings=result.findings, reason_codes=codes, reasons=[item.value for item in codes],
            missing_fields=list(dict.fromkeys(item.field for item in clauses
                if item.kind == "OUTPUT" and item.field and item.status is not ClauseStatus.SATISFIED)),
            warnings=result.warnings)

    async def _validate_full(
        self,
        query: GroundedQuery,
        evidence: EvidenceBundle,
    ) -> ValidationResult:
        findings: list[ValidationFinding] = []
        required_fields = _required_fields(query)
        ranking_scope = bool(query.grounded_sort)
        product_type = _query_product_type(query)
        comparison_scope = ranking_scope or (
            query.parsed_query.intent is QueryIntent.COMPARE_PRODUCTS
        )

        findings.extend(self._execution_findings(evidence))
        findings.extend(self._structured_constraint_findings(query, evidence))
        findings.extend(self._ranking_findings(query, evidence))
        findings.extend(self._required_field_findings(required_fields, evidence))
        findings.extend(self._entity_field_findings(query, required_fields, evidence))
        findings.extend(self._sentinel_findings(required_fields, evidence))
        findings.extend(self._entity_findings(query, evidence))
        findings.extend(self._conflict_findings(required_fields, evidence))
        findings.extend(
            self._snapshot_findings(
                required_fields,
                comparison_scope,
                evidence,
            )
        )
        findings.extend(self._observed_at_findings(evidence))
        findings.extend(self._federated_snapshot_findings(evidence))
        findings.extend(self._comparison_contract_findings(query, evidence))
        findings.extend(
            self._coverage_findings(
                [item.canonical_field for item in query.grounded_sort if item.canonical_field] if ranking_scope else required_fields,
                comparison_scope,
                ranking_scope,
                product_type,
                evidence,
            )
        )

        if not self._has_usable_evidence(evidence):
            code = self._no_usable_evidence_code(evidence)
            findings.append(
                ValidationFinding(
                    code=code,
                    severity=FindingSeverity.BLOCKING,
                    message={
                        AnswerabilityReasonCode.ZERO_MATCH: (
                            "The fully compiled structured query matched no entities."
                        ),
                        AnswerabilityReasonCode.INSUFFICIENT_EVIDENCE: (
                            "Records were retrieved but contain no usable evidence."
                        ),
                    }.get(code, "No usable evidence was retrieved."),
                )
            )

        answerable = not any(
            finding.severity is FindingSeverity.BLOCKING
            for finding in findings
        )
        reason_codes = list(
            dict.fromkeys(finding.code for finding in findings)
        )
        if answerable:
            reason_codes.insert(0, AnswerabilityReasonCode.ANSWERABLE)

        missing_fields = list(dict.fromkeys(
            finding.field for finding in findings
            if finding.code is AnswerabilityReasonCode.MISSING_REQUIRED_FIELD
            and finding.field is not None
        ))
        warnings = [
            finding.message
            for finding in findings
            if finding.severity is FindingSeverity.WARNING
        ]
        return ValidationResult(
            answerable=answerable,
            reason_codes=reason_codes,
            findings=findings,
            reasons=[code.value for code in reason_codes],
            missing_fields=missing_fields,
            warnings=warnings,
        )

    @staticmethod
    def _structured_constraint_findings(
        query: GroundedQuery, bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        # A receipt proves execution of the Boolean expression; OR does not
        # prove that every individual branch is true for each returned entity.
        required = {item.canonical_field for item in query.grounded_filters if item.canonical_field}
        try:
            predicate = structured_predicate(query)
            expected_tree = predicate.model_dump(mode="json") if predicate else None
            expected_filters = structured_query_inputs(query)["filters"]
        except UnsupportedQuerySemanticsError:
            return [ValidationFinding(
                code=AnswerabilityReasonCode.UNSUPPORTED_QUERY_SEMANTICS,
                severity=FindingSeverity.BLOCKING,
                message="The predicate expression has no executable contract.",
            )]
        expected_receipts = {
            (item["canonical_field"], item["raw"]["operator"], repr(item["canonical_value"]))
            for item in expected_filters
        }
        for item in bundle.evidence:
            if item.metadata.get("repository_version") != "v2" or item.source_type != "rdb":
                continue
            fields = item.metadata.get("matched_constraints", [])
            matches = item.metadata.get("structured_constraint_matches", [])
            boolean_receipt = (
                expected_tree is not None
                and item.metadata.get("structured_boolean_expression") == expected_tree
                and item.metadata.get("structured_boolean_satisfied") is True
            )
            valid = isinstance(matches, list) and all(
                isinstance(match, dict)
                and isinstance(match.get("canonical_field"), str)
                and bool(match["canonical_field"])
                and isinstance(match.get("operator"), str)
                and match.get("operator") in {"eq", "ne", "in", "gt", "gte", "lt", "lte", "between", "contains"}
                and match.get("value") is not None
                and (
                    match.get("satisfied") is True
                    or (boolean_receipt and match.get("satisfied") is None
                        and match.get("applied_in_expression") is True)
                )
                for match in matches
            )
            if (
                valid
                and isinstance(fields, list)
                and all(isinstance(field, str) for field in fields)
                and required.issubset(fields)
                and sorted(fields) == sorted(match["canonical_field"] for match in matches)
                and expected_receipts == {
                    (match["canonical_field"], match["operator"], repr(match["value"]))
                    for match in matches
                }
                and (not _has_or(expected_tree) or boolean_receipt)
            ):
                continue
            findings.append(ValidationFinding(
                code=AnswerabilityReasonCode.INSUFFICIENT_EVIDENCE,
                severity=FindingSeverity.BLOCKING,
                entity_id=item.entity_id,
                source_ids=[item.source_id],
                message="Executed structured constraint semantics are missing or incomplete.",
            ))
        return findings

    def _entity_field_findings(
        self, query: GroundedQuery, fields: list[str], bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        """One entity's fact cannot satisfy another entity's requested field."""
        selected = {
            item.entity_id for item in bundle.evidence
            if item.entity_id and (item.source_type == "rdb" or item.field in fields)
        }
        if not query.grounded_sort and query.parsed_query.result_limit is None:
            selected.update(
                item.canonical_id for item in query.resolved_entities
                if item.resolution_status is ResolutionStatus.RESOLVED
                and item.entity_type in {"product", "fund_share_class", "sale_lot"}
                and item.canonical_id
            )
        return [ValidationFinding(
            code=AnswerabilityReasonCode.MISSING_REQUIRED_FIELD,
            severity=FindingSeverity.BLOCKING, entity_id=entity_id, field=field,
            source_ids=[item.source_id for item in bundle.evidence if item.entity_id == entity_id],
            message=f"Required evidence field is unavailable for {entity_id}: {field}",
        ) for entity_id in sorted(selected) for field in fields if not any(
            item.entity_id == entity_id and item.field == field
            and not self._is_missing(item.value) and not self._is_sentinel(item)
            for item in bundle.evidence
        )]

    @staticmethod
    def _federated_snapshot_findings(bundle: EvidenceBundle) -> list[ValidationFinding]:
        sources = {item.source_type for item in bundle.evidence}
        if len(sources & {"rdb", "graph", "vector", "bm25"}) < 2:
            return []
        findings: list[ValidationFinding] = []
        for key in ("dataset_snapshot", "generation"):
            values = {
                str(item.dataset_snapshot if key == "dataset_snapshot" else item.metadata.get(key))
                for item in bundle.evidence
                if (item.dataset_snapshot if key == "dataset_snapshot" else item.metadata.get(key))
            }
            if len(values) > 1:
                findings.append(ValidationFinding(
                    code=AnswerabilityReasonCode.SNAPSHOT_MISMATCH,
                    severity=FindingSeverity.BLOCKING,
                    source_ids=list(dict.fromkeys(item.source_id for item in bundle.evidence)),
                    message=f"Federated evidence has inconsistent {key}.",
                    metadata={key: sorted(values)},
                ))
        return findings

    def _comparison_contract_findings(
        self, query: GroundedQuery, bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        if query.parsed_query.comparison is None:
            return []
        inputs = structured_query_inputs(query)
        compared = (inputs.get("comparison") or {}).get("fields", [])
        findings: list[ValidationFinding] = []
        entity_ids = {item.entity_id for item in bundle.evidence if item.entity_id and item.field in compared}
        if not query.grounded_sort and len(entity_ids) < 2:
            findings.append(ValidationFinding(
                code=AnswerabilityReasonCode.INSUFFICIENT_EVIDENCE,
                severity=FindingSeverity.BLOCKING,
                message="Fieldwise comparison requires evidence for at least two entities.",
            ))
        for field in compared:
            contract, reason = MetricCapabilityRegistry().comparison_contract(field, inputs)
            if contract is None:
                findings.append(ValidationFinding(
                    code=AnswerabilityReasonCode.UNSUPPORTED_CONSTRAINT,
                    severity=FindingSeverity.BLOCKING, field=field,
                    message=reason or "No field comparison contract exists.",
                ))
                continue
            for item in bundle.evidence:
                if item.field != field or self._is_missing(item.value):
                    continue
                metadata = item.metadata
                if contract.comparison_kind == "ordered_vocabulary":
                    valid = item.value in contract.ordered_values
                else:
                    valid = (
                        metadata.get("metric_dataset") == contract.dataset
                        and metadata.get("metric_unit") == contract.unit
                        and metadata.get("metric_scale_basis") == contract.scale
                        and (contract.currency is None or metadata.get("metric_currency") == contract.currency)
                        and bool(metadata.get("field_fact_id"))
                        and bool(metadata.get("field_evidence_assertion_ids"))
                        and bool(item.observed_at)
                    )
                if not valid:
                    findings.append(ValidationFinding(
                        code=AnswerabilityReasonCode.INSUFFICIENT_EVIDENCE,
                        severity=FindingSeverity.BLOCKING, entity_id=item.entity_id,
                        field=field, source_ids=[item.source_id],
                        message="Comparison evidence does not satisfy its dataset/unit/scale/provenance contract.",
                    ))
        return findings

    @staticmethod
    def _execution_findings(
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        if bundle.execution_result is None:
            return []
        findings: list[ValidationFinding] = []
        for result in bundle.execution_result.step_results.values():
            if result.status is StepExecutionStatus.FAILED:
                code = AnswerabilityReasonCode.RETRIEVAL_FAILED
                message = "A required retrieval step failed."
            elif result.status is StepExecutionStatus.TIMED_OUT:
                code = AnswerabilityReasonCode.RETRIEVAL_TIMED_OUT
                message = "A required retrieval step timed out."
            elif result.status is StepExecutionStatus.SKIPPED:
                code = AnswerabilityReasonCode.DEPENDENCY_INCOMPLETE
                message = "A step was skipped because a dependency was incomplete."
            else:
                continue
            findings.append(
                ValidationFinding(
                    code=code,
                    severity=FindingSeverity.BLOCKING,
                    source_ids=[result.step_id],
                    message=message,
                    metadata={
                        "step_id": result.step_id,
                        "status": result.status.value,
                        "error_code": (
                            result.error_code.value
                            if result.error_code is not None
                            else None
                        ),
                    },
                )
            )
        return findings

    @staticmethod
    def _ranking_findings(
        query: GroundedQuery,
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        if not query.grounded_sort or bundle.execution_result is None:
            return []
        final_records = bundle.execution_result.records
        if not any(
            item.metadata.get("real_rdb") is True
            or item.metadata.get("transform_operation") == "rank_candidates"
            for item in final_records
        ):
            return []
        if not final_records or any(
            item.metadata.get("ranking_applied") is True
            for item in final_records
        ):
            return []
        return [
            ValidationFinding(
                code=AnswerabilityReasonCode.RANKING_NOT_APPLIED,
                severity=FindingSeverity.BLOCKING,
                source_ids=list(
                    dict.fromkeys(
                        item.source_id for item in bundle.evidence
                    )
                ),
                message="The requested ranking was not applied.",
            )
        ]

    def _required_field_findings(
        self,
        required_fields: list[str],
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        return [
            ValidationFinding(
                code=AnswerabilityReasonCode.MISSING_REQUIRED_FIELD,
                severity=FindingSeverity.BLOCKING,
                field=field,
                source_ids=[
                    item.source_id
                    for item in bundle.evidence
                    if item.field == field
                ],
                message=f"Required evidence field is unavailable: {field}",
            )
            for field in required_fields
            if not self._valid_field_evidence(field, bundle)
        ]

    def _sentinel_findings(
        self,
        required_fields: list[str],
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        for item in bundle.evidence:
            if item.field is None or self._is_missing(item.value):
                continue
            if not self._is_sentinel(item):
                continue
            findings.append(
                ValidationFinding(
                    code=AnswerabilityReasonCode.INVALID_SENTINEL,
                    severity=(
                        FindingSeverity.BLOCKING
                        if item.field in required_fields
                        else FindingSeverity.WARNING
                    ),
                    entity_id=item.entity_id,
                    field=item.field,
                    source_ids=[item.source_id],
                    message=f"Sentinel value is not valid evidence: {item.field}",
                    metadata={"raw_value": item.value},
                )
            )
        return findings

    @staticmethod
    def _entity_findings(
        query: GroundedQuery,
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        unresolved = [
            entity
            for entity in query.resolved_entities
            if entity.resolution_status is ResolutionStatus.UNRESOLVED
        ]
        if unresolved:
            unresolved_alias = [
                item
                for item in unresolved
                if item.resolution_reason == "ENTITY_UNRESOLVED"
            ]
            parse_failed = [
                item
                for item in unresolved
                if item.resolution_reason == "ENTITY_PARSE_FAILED"
            ]
            not_found = [
                item
                for item in unresolved
                if item not in unresolved_alias and item not in parse_failed
            ]
            for code, mentions in (
                (AnswerabilityReasonCode.ENTITY_PARSE_FAILED, parse_failed),
                (AnswerabilityReasonCode.ENTITY_UNRESOLVED, unresolved_alias),
                (AnswerabilityReasonCode.ENTITY_NOT_FOUND, not_found),
            ):
                if not mentions:
                    continue
                findings.append(ValidationFinding(
                    code=code,
                    severity=FindingSeverity.BLOCKING,
                    message="The requested entity could not be resolved.",
                    metadata={"raw_mentions": [item.raw_text for item in mentions]},
                ))
        ambiguous = [
            entity
            for entity in query.resolved_entities
            if entity.resolution_status is ResolutionStatus.AMBIGUOUS
        ]
        if ambiguous:
            findings.append(
                ValidationFinding(
                    code=AnswerabilityReasonCode.AMBIGUOUS_ENTITY,
                    severity=FindingSeverity.BLOCKING,
                    message="The requested entity is ambiguous.",
                    metadata={
                        "raw_mentions": [item.raw_text for item in ambiguous]
                    },
                )
            )

        expected_ids = {
            entity.canonical_id
            for entity in query.resolved_entities
            if entity.resolution_status is ResolutionStatus.RESOLVED
            and entity.canonical_id is not None
            and entity.entity_type == "product"
        }
        mismatched = [
            item
            for item in bundle.evidence
            if item.entity_id is not None
            and expected_ids
            and item.entity_id not in expected_ids
        ]
        if mismatched:
            findings.append(
                ValidationFinding(
                    code=AnswerabilityReasonCode.ENTITY_MISMATCH,
                    severity=FindingSeverity.BLOCKING,
                    source_ids=list(
                        dict.fromkeys(item.source_id for item in mismatched)
                    ),
                    message="Evidence does not match the resolved entity.",
                    metadata={
                        "expected_entity_ids": sorted(expected_ids),
                        "actual_entity_ids": sorted(
                            {
                                item.entity_id
                                for item in mismatched
                                if item.entity_id is not None
                            }
                        ),
                    },
                )
            )
        return findings

    @staticmethod
    def _no_usable_evidence_code(
        bundle: EvidenceBundle,
    ) -> AnswerabilityReasonCode:
        """Keep empty-result semantics distinct from absent or unusable evidence."""
        execution = bundle.execution_result
        if execution is not None:
            successful = [
                result for result in execution.step_results.values()
                if result.status is StepExecutionStatus.SUCCESS
                and (
                    isinstance(result.retrieval_metadata.get("total_matches"), int)
                    or (
                        result.retrieval_metadata.get("counts", {}).get("path_count", 0) > 0
                        and result.retrieval_metadata.get("counts", {}).get("path_total_sum") == 0
                    )
                )
            ]
            if successful and all(
                result.retrieval_metadata.get("total_matches") == 0
                or result.retrieval_metadata.get("counts", {}).get("path_total_sum") == 0
                for result in successful
            ):
                return AnswerabilityReasonCode.ZERO_MATCH
        if bundle.evidence:
            return AnswerabilityReasonCode.INSUFFICIENT_EVIDENCE
        return AnswerabilityReasonCode.NO_EVIDENCE

    def _conflict_findings(
        self,
        required_fields: list[str],
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        grouped: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
        for item in bundle.evidence:
            if (
                item.entity_id is None
                or item.field is None
                or self._is_missing(item.value)
                or self._is_sentinel(item)
                or item.metadata.get("multi_valued_relation") is True
            ):
                continue
            grouped[(item.entity_id, item.field)].append(item)

        findings: list[ValidationFinding] = []
        for (entity_id, field), items in sorted(grouped.items()):
            values = {_normalize_value(item.value) for item in items}
            if len(values) <= 1:
                continue
            findings.append(
                ValidationFinding(
                    code=AnswerabilityReasonCode.CONFLICTING_EVIDENCE,
                    severity=(
                        FindingSeverity.BLOCKING
                        if field in required_fields
                        else FindingSeverity.WARNING
                    ),
                    entity_id=entity_id,
                    field=field,
                    source_ids=list(
                        dict.fromkeys(item.source_id for item in items)
                    ),
                    message=f"Evidence values conflict for {field}.",
                    metadata={"normalized_values": sorted(values)},
                )
            )
        return findings

    def _snapshot_findings(
        self,
        required_fields: list[str],
        comparison_scope: bool,
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        grouped: dict[str, list[Evidence]] = defaultdict(list)
        for item in bundle.evidence:
            if item.field is not None and item.dataset_snapshot is not None:
                grouped[item.field].append(item)

        findings: list[ValidationFinding] = []
        for field, items in sorted(grouped.items()):
            snapshots = {
                item.dataset_snapshot.strip()
                for item in items
                if item.dataset_snapshot.strip()
            }
            if len(snapshots) <= 1:
                continue
            quality = self._quality_provider.get_quality(field)
            if quality.snapshot_policy is SnapshotPolicy.IGNORE:
                continue
            blocks = (
                comparison_scope
                and field in required_fields
                and quality.snapshot_policy
                is SnapshotPolicy.REQUIRE_CONSISTENT_FOR_COMPARISON
            )
            findings.append(
                ValidationFinding(
                    code=AnswerabilityReasonCode.SNAPSHOT_MISMATCH,
                    severity=(
                        FindingSeverity.BLOCKING
                        if blocks
                        else FindingSeverity.WARNING
                    ),
                    field=field,
                    source_ids=list(
                        dict.fromkeys(item.source_id for item in items)
                    ),
                    message=f"Evidence snapshots differ for {field}.",
                    metadata={"snapshots": sorted(snapshots)},
                )
            )
        return findings

    @staticmethod
    def _observed_at_findings(
        bundle: EvidenceBundle,
    ) -> list[ValidationFinding]:
        grouped: dict[str, list[Evidence]] = defaultdict(list)
        for item in bundle.evidence:
            if item.field is not None and item.observed_at is not None:
                grouped[item.field].append(item)

        findings: list[ValidationFinding] = []
        for field, items in sorted(grouped.items()):
            observations = {
                item.observed_at.strip()
                for item in items
                if item.observed_at.strip()
            }
            if len(observations) <= 1:
                continue
            findings.append(
                ValidationFinding(
                    code=AnswerabilityReasonCode.OBSERVATION_TIME_MISMATCH,
                    severity=FindingSeverity.WARNING,
                    field=field,
                    source_ids=list(
                        dict.fromkeys(item.source_id for item in items)
                    ),
                    message=f"Evidence observation times differ for {field}.",
                    metadata={"observed_at": sorted(observations)},
                )
            )
        return findings

    def _coverage_findings(
        self,
        required_fields: list[str],
        comparison_scope: bool,
        ranking_scope: bool,
        product_type: str | None,
        evidence: EvidenceBundle,
    ) -> list[ValidationFinding]:
        if not comparison_scope:
            return []
        findings: list[ValidationFinding] = []
        for field in required_fields:
            if comparison_scope and any(
                contract.get("canonical_field") == field
                and contract.get("sort_capability") is True
                for record in evidence.evidence
                for contract in record.metadata.get("comparison_contracts", [])
                if isinstance(contract, dict)
            ):
                continue
            quality = self._quality_provider.get_quality(field, product_type)
            safety = (
                quality.ranking_safe
                if ranking_scope
                else quality.comparison_safe
            )
            if quality.coverage_status is CoverageStatus.COMPLETE and (
                safety is not False
            ):
                continue
            if safety is True:
                continue
            findings.append(
                ValidationFinding(
                    code=AnswerabilityReasonCode.INSUFFICIENT_COVERAGE,
                    severity=FindingSeverity.BLOCKING,
                    field=field,
                    message=(
                        "Field coverage is insufficient for a global "
                        f"comparison: {field}"
                    ),
                    metadata={
                        "coverage_status": quality.coverage_status.value,
                        "coverage_fraction": quality.coverage_fraction,
                    },
                )
            )
        return findings

    def _has_usable_evidence(self, bundle: EvidenceBundle) -> bool:
        return any(
            (
                not self._is_missing(item.value)
                and not self._is_sentinel(item)
            )
            or not self._is_missing(item.text)
            for item in bundle.evidence
        )

    def _valid_field_evidence(
        self,
        field: str,
        bundle: EvidenceBundle,
    ) -> bool:
        return any(
            item.field == field
            and not self._is_missing(item.value)
            and not self._is_sentinel(item)
            for item in bundle.evidence
        )

    def _is_sentinel(self, item: Evidence) -> bool:
        if item.field is None or item.value is None:
            return False
        quality = self._quality_provider.get_quality(item.field)
        normalized = _normalize_value(item.value)
        return any(
            normalized == _normalize_value(value)
            for value in quality.sentinel_values
        )

    @staticmethod
    def _is_missing(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip().casefold() in _MISSING_LITERALS
        return False


def _required_fields(query: GroundedQuery) -> list[str]:
    return list(
        dict.fromkeys(
            field
            for field in [
                *(
                    item.canonical_field
                    for item in query.grounded_requested_fields
                ),
                *(item.canonical_field for item in query.grounded_sort),
            ]
            if field is not None
        )
    )


def _query_product_type(query: GroundedQuery) -> str | None:
    product_types = list(
        dict.fromkeys(
            item.canonical_concept.value
            for item in query.grounded_concepts
            if item.category is ConceptCategory.PRODUCT_TYPE
            and item.canonical_concept is not None
        )
    )
    return product_types[0] if len(product_types) == 1 else None


def _normalize_value(value: object) -> str:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return str(value).casefold()


def _has_or(tree: dict | None) -> bool:
    return bool(tree and (tree.get("node_type") == "or" or any(
        _has_or(child) for child in tree.get("children", [])
    )))

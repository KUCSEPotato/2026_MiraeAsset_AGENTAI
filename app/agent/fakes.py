from app.domain.models import (
    CanonicalConcept,
    EntityMention,
    EvidenceBundle,
    ExecutionResult,
    FilterOperator,
    FilterSpec,
    GroundedQuery,
    ParsedQuery,
    QueryPlan,
    QueryStep,
    QueryIntent,
    ResolutionStatus,
    ResolvedQuery,
    RetrievalRecord,
    ValidationResult,
)


class FakeQueryAnalyzer:
    async def analyze(self, question: str) -> ParsedQuery:
        normalized = question.upper()
        entities: list[EntityMention] = []
        filters: list[FilterSpec] = []

        if "ETF" in normalized:
            entities.append(
                EntityMention(
                    raw_text="ETF",
                    entity_type="financial_product",
                    confidence=1.0,
                )
            )
        if "미국" in question or "US" in normalized:
            filters.append(
                FilterSpec(
                    field="region",
                    operator=FilterOperator.EQ,
                    value="US",
                )
            )
        if "채권" in question or "BOND" in normalized:
            filters.append(
                FilterSpec(
                    field="asset_type",
                    operator=FilterOperator.EQ,
                    value="Bond",
                )
            )

        return ParsedQuery(
            original_question=question,
            intent=QueryIntent.SEARCH_PRODUCT,
            entities=entities,
            filters=filters,
            requires_semantic_search=False,
        )


class FakeEntityResolver:
    async def resolve(self, query: ParsedQuery) -> ResolvedQuery:
        resolved = [
            mention.model_copy(
                update={
                    "canonical_id": (
                        f"fake-concept:{mention.entity_type}:"
                        f"{mention.raw_text.casefold()}"
                    ),
                    "resolution_status": ResolutionStatus.RESOLVED,
                    "confidence": 1.0,
                }
            )
            for mention in query.entities
        ]
        return ResolvedQuery(parsed_query=query, resolved_entities=resolved)


class FakeOntologyService:
    _concept_mapping = {
        "ETF": CanonicalConcept.FINANCIAL_PRODUCT_ETF,
        "US": CanonicalConcept.REGION_US,
        "Bond": CanonicalConcept.ASSET_TYPE_BOND,
    }

    async def ground(self, query: ResolvedQuery) -> GroundedQuery:
        concepts = [
            self._concept_mapping[entity.raw_text]
            for entity in query.resolved_entities
            if entity.raw_text in self._concept_mapping
        ]
        concepts.extend(
            self._concept_mapping[str(filter_spec.value)]
            for filter_spec in query.parsed_query.filters
            if str(filter_spec.value) in self._concept_mapping
        )
        canonical_fields = {
            filter_spec.field: filter_spec.field
            for filter_spec in query.parsed_query.filters
        }
        return GroundedQuery(
            parsed_query=query.parsed_query,
            resolved_entities=query.resolved_entities,
            canonical_concepts=concepts,
            canonical_fields=canonical_fields,
        )


class FakePlanner:
    async def create_plan(self, query: GroundedQuery) -> QueryPlan:
        filters = (
            [
                {
                    "raw": item.raw_filter.model_dump(mode="json"),
                    "canonical_field": item.canonical_field,
                    "canonical_value": (
                        item.canonical_value.value
                        if item.canonical_value is not None
                        else None
                    ),
                    "grounding_status": item.status.value,
                }
                for item in query.grounded_filters
            ]
            if query.grounded_filters
            else [item.model_dump(mode="json") for item in query.parsed_query.filters]
        )
        return QueryPlan(
            planner="rule",
            steps=[
                QueryStep(
                    step_id="fake-rdb-search-1",
                    source="rdb",
                    operation="search_products",
                    inputs={
                        "filters": filters,
                        "canonical_fields": query.canonical_fields,
                        "fake_execution": True,
                    },
                )
            ],
        )


class FakeExecutor:
    def __init__(self, records: list[RetrievalRecord] | None = None) -> None:
        self._records = records

    async def execute(self, plan: QueryPlan) -> list[RetrievalRecord]:
        if not plan.steps:
            raise ValueError("query plan must contain at least one step")
        records = self._records
        if records is None:
            records = [
                RetrievalRecord(
                    source="rdb",
                    source_id="fake-pipeline-record-001",
                    entity_id="fake-pipeline-record-001",
                    payload={
                        "field": "pipeline_status",
                        "value": "deterministic_test_record",
                        "text": (
                            "M2 pipeline test evidence only; this is not "
                            "financial product data."
                        ),
                    },
                    metadata={"fake": True, "dataset_snapshot": "2026-07-11"},
                )
            ]
        return [record.model_copy(deep=True) for record in records]

    async def execute_with_result(self, plan: QueryPlan) -> ExecutionResult:
        return ExecutionResult(records=await self.execute(plan))


class FakeEvidenceValidator:
    async def validate(
        self,
        query: GroundedQuery,
        evidence: EvidenceBundle,
    ) -> ValidationResult:
        if evidence.evidence:
            return ValidationResult(
                answerable=True,
                reasons=["usable_fake_evidence_available"],
                warnings=["fake_validation_only"],
            )

        missing_fields = evidence.missing_fields.copy()
        if not missing_fields:
            missing_fields = query.parsed_query.requested_fields.copy() or ["evidence"]
        return ValidationResult(
            answerable=False,
            reasons=["no_usable_evidence"],
            missing_fields=missing_fields,
            warnings=["fake_validation_only"],
        )


class FakeAnswerGenerator:
    async def generate(
        self,
        question: str,
        evidence: EvidenceBundle,
        validation: ValidationResult,
    ) -> str:
        return "M2 pipeline test answer generated from validated fake evidence."


class FakeSafeResponseGenerator:
    async def generate(self, validation: ValidationResult) -> str:
        return "제공된 테스트 근거에서 해당 정보를 확인할 수 없습니다."

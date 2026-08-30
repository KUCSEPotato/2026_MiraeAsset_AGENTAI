from app.domain.models import (
    CanonicalConcept,
    ConceptCategory,
    GroundedConcept,
    GroundedField,
    GroundedFilter,
    GroundedQuery,
    GroundedRelation,
    GroundedSort,
    GroundingStatus,
    ResolvedQuery,
    RelationMention,
)
from app.ontology.registry import SemanticRegistry
from app.ontology.constraint_status import grounded_constraint_statuses


class RegistryOntologyService:
    def __init__(self, registry: SemanticRegistry) -> None:
        self._registry = registry

    async def ground(self, query: ResolvedQuery) -> GroundedQuery:
        grounded_concepts: list[GroundedConcept] = []
        grounded_filters: list[GroundedFilter] = []
        grounded_sort: list[GroundedSort] = []
        grounded_requested_fields: list[GroundedField] = []
        grounded_relations: list[GroundedRelation] = []
        canonical_concepts: list[CanonicalConcept] = []
        canonical_fields: dict[str, str] = {}
        unresolved_concepts: list[str] = []

        for raw_product_type in query.parsed_query.product_types:
            concept = self._ground_concept(
                raw_product_type,
                ConceptCategory.PRODUCT_TYPE,
            )
            grounded_concepts.append(concept)
            self._collect_concept(concept, canonical_concepts, unresolved_concepts)

        category_by_filter = {
            "region": ConceptCategory.REGION,
            "asset_type": ConceptCategory.ASSET_TYPE,
            "product_type": ConceptCategory.PRODUCT_TYPE,
        }
        for raw_filter in query.parsed_query.filters:
            canonical_field = self._registry.map_field(raw_filter.field)
            category = category_by_filter.get(raw_filter.field)
            raw_values = (
                raw_filter.value
                if isinstance(raw_filter.value, list)
                else [raw_filter.value]
            )
            canonical_values = [
                self._registry.resolve_concept(str(value), category)
                if category is not None
                else None
                for value in raw_values
            ]
            canonical_value = canonical_values[0] if canonical_values else None
            status = (
                GroundingStatus.RESOLVED
                if canonical_field is not None
                and canonical_values
                and all(value is not None for value in canonical_values)
                else GroundingStatus.UNRESOLVED
            )
            grounded_filters.append(
                GroundedFilter(
                    raw_filter=raw_filter,
                    canonical_field=canonical_field,
                    canonical_value=canonical_value,
                    canonical_values=[
                        value for value in canonical_values if value is not None
                    ],
                    status=status,
                )
            )
            if canonical_field is not None:
                canonical_fields[raw_filter.field] = canonical_field
            for raw_value, concept in zip(
                raw_values, canonical_values, strict=True
            ):
                value_concept = GroundedConcept(
                    raw_text=str(raw_value),
                    category=category or ConceptCategory.SEMANTIC_TERM,
                    canonical_concept=concept,
                    status=(
                        GroundingStatus.RESOLVED
                        if concept is not None
                        else GroundingStatus.UNRESOLVED
                    ),
                )
                grounded_concepts.append(value_concept)
                self._collect_concept(
                    value_concept,
                    canonical_concepts,
                    unresolved_concepts,
                )

        for raw_sort in query.parsed_query.sort:
            canonical_field = self._registry.map_field(raw_sort.field)
            grounded_sort.append(
                GroundedSort(
                    raw_sort=raw_sort,
                    canonical_field=canonical_field,
                    status=(
                        GroundingStatus.RESOLVED
                        if canonical_field is not None
                        else GroundingStatus.UNRESOLVED
                    ),
                )
            )
            if canonical_field is not None:
                canonical_fields[raw_sort.field] = canonical_field

        for raw_field in query.parsed_query.requested_fields:
            canonical_field = self._registry.map_field(raw_field)
            grounded_requested_fields.append(
                GroundedField(
                    raw_text=raw_field,
                    canonical_field=canonical_field,
                    status=(
                        GroundingStatus.RESOLVED
                        if canonical_field is not None
                        else GroundingStatus.UNRESOLVED
                    ),
                )
            )
            if canonical_field is not None:
                canonical_fields[raw_field] = canonical_field

        for raw_term in query.parsed_query.semantic_terms:
            concept = self._ground_concept(
                raw_term,
                ConceptCategory.SEMANTIC_TERM,
            )
            grounded_concepts.append(concept)
            self._collect_concept(concept, canonical_concepts, unresolved_concepts)

        relation_aliases = {
            "운용사": "managedBy",
            "운용하는": "managedBy",
            "발행사": "issuedBy",
            "발행한": "issuedBy",
            "기초지수": "tracks",
            "추종지수": "tracks",
            "벤치마크": "referencesBenchmark",
            "펀드 클래스": "hasClass",
            "표시통화": "denominatedIn",
            "위험등급": "hasRiskGrade",
        }
        for relation in query.parsed_query.relations:
            mention = (
                relation
                if isinstance(relation, RelationMention)
                else RelationMention(raw_text=relation)
            )
            canonical = relation_aliases.get(mention.raw_text.casefold())
            grounded_relations.append(
                GroundedRelation(
                    raw_text=mention.raw_text,
                    canonical_relation=canonical,
                    direction=mention.direction,
                    status=(
                        GroundingStatus.RESOLVED
                        if canonical is not None
                        else GroundingStatus.UNRESOLVED
                    ),
                    constraint_id=mention.constraint_id,
                    subject_type=mention.subject_type,
                    target_raw_text=mention.target_raw_text,
                    target_type=mention.target_type,
                    target_value=mention.target_value,
                    negated=mention.negated,
                    chain_id=mention.chain_id,
                    path_position=mention.path_position,
                )
            )

        semantic_constraints = grounded_constraint_statuses(
            query.parsed_query,
            resolved_entities=query.resolved_entities,
            filters=grounded_filters,
            sorts=grounded_sort,
            requested_fields=grounded_requested_fields,
            relations=grounded_relations,
        )

        return GroundedQuery(
            parsed_query=query.parsed_query,
            resolved_entities=query.resolved_entities,
            canonical_concepts=canonical_concepts,
            canonical_fields=canonical_fields,
            grounded_concepts=grounded_concepts,
            grounded_filters=grounded_filters,
            grounded_sort=grounded_sort,
            grounded_requested_fields=grounded_requested_fields,
            grounded_relations=grounded_relations,
            unresolved_concepts=unresolved_concepts,
            semantic_constraints=semantic_constraints,
        )

    def _ground_concept(
        self,
        raw_text: str,
        category: ConceptCategory,
    ) -> GroundedConcept:
        canonical = self._registry.resolve_concept(raw_text, category)
        return GroundedConcept(
            raw_text=raw_text,
            category=category,
            canonical_concept=canonical,
            status=(
                GroundingStatus.RESOLVED
                if canonical is not None
                else GroundingStatus.UNRESOLVED
            ),
        )

    @staticmethod
    def _collect_concept(
        concept: GroundedConcept,
        canonical_concepts: list[CanonicalConcept],
        unresolved_concepts: list[str],
    ) -> None:
        if concept.canonical_concept is not None:
            if concept.canonical_concept not in canonical_concepts:
                canonical_concepts.append(concept.canonical_concept)
        elif concept.raw_text not in unresolved_concepts:
            unresolved_concepts.append(concept.raw_text)

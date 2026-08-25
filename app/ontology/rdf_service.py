from __future__ import annotations

from app.domain.models import (
    CanonicalConcept, ConceptCategory, GroundedConcept, GroundedField,
    GroundedFilter, GroundedQuery, GroundedRelation, GroundedSort,
    GroundingStatus, RelationMention, ResolvedQuery,
)
from app.ontology.loader import LoadedOntology


class RDFOntologyService:
    def __init__(self, ontology: LoadedOntology) -> None:
        self.ontology = ontology
        self.index = ontology.index

    async def ground(self, query: ResolvedQuery) -> GroundedQuery:
        parsed = query.parsed_query
        concepts: list[CanonicalConcept] = []
        grounded_concepts: list[GroundedConcept] = []
        unresolved: list[str] = []
        for category, values in (
            (ConceptCategory.PRODUCT_TYPE, parsed.product_types),
            (ConceptCategory.SEMANTIC_TERM, parsed.semantic_terms),
        ):
            for raw in values:
                term = self.index.resolve(raw)
                canonical = self._concept(term.canonical_name) if term else None
                status = GroundingStatus.RESOLVED if canonical else GroundingStatus.UNRESOLVED
                grounded_concepts.append(GroundedConcept(raw_text=raw, category=category, canonical_concept=canonical, status=status))
                if canonical:
                    concepts.append(canonical)
                else:
                    unresolved.append(raw)

        grounded_filters = []
        canonical_fields: dict[str, str] = {}
        for item in parsed.filters:
            field_term = self.index.resolve(item.field)
            field = field_term.canonical_name if field_term and field_term.category == "field" else None
            value_term = self.index.resolve(str(item.value))
            value = self._concept(value_term.canonical_name) if value_term else None
            if value:
                concepts.append(value)
            status = GroundingStatus.RESOLVED if field else GroundingStatus.UNRESOLVED
            grounded_filters.append(GroundedFilter(raw_filter=item, canonical_field=field, canonical_value=value, status=status))
            if field:
                canonical_fields[item.field] = field

        grounded_sort = [self._ground_sort(item) for item in parsed.sort]
        requested = [self._ground_field(item) for item in parsed.requested_fields]
        relations = [self._ground_relation(item) for item in parsed.relations]
        return GroundedQuery(
            parsed_query=parsed, resolved_entities=query.resolved_entities,
            canonical_concepts=list(dict.fromkeys(concepts)), canonical_fields=canonical_fields,
            grounded_concepts=grounded_concepts, grounded_filters=grounded_filters,
            grounded_sort=grounded_sort, grounded_requested_fields=requested,
            grounded_relations=relations, unresolved_concepts=unresolved,
            semantic_constraints=parsed.semantic_constraints,
        )

    def _ground_sort(self, item):
        term = self.index.resolve(item.field)
        field = term.canonical_name if term and term.category == "field" else None
        return GroundedSort(raw_sort=item, canonical_field=field, status=GroundingStatus.RESOLVED if field else GroundingStatus.UNRESOLVED)

    def _ground_field(self, raw: str):
        term = self.index.resolve(raw)
        field = term.canonical_name if term and term.category == "field" else None
        return GroundedField(raw_text=raw, canonical_field=field, status=GroundingStatus.RESOLVED if field else GroundingStatus.UNRESOLVED)

    def _ground_relation(self, raw):
        mention = raw if isinstance(raw, RelationMention) else RelationMention(raw_text=str(raw))
        term = self.index.resolve(mention.raw_text)
        relation = self.index.local_name(term.uri) if term and term.category == "relation" else None
        return GroundedRelation(
            raw_text=mention.raw_text, canonical_relation=relation,
            ontology_uri=term.uri if relation else None,
            direction=mention.direction, status=GroundingStatus.RESOLVED if relation else GroundingStatus.UNRESOLVED,
            constraint_id=mention.constraint_id, subject_type=mention.subject_type,
            target_raw_text=mention.target_raw_text, target_type=mention.target_type,
            target_value=mention.target_value, negated=mention.negated,
            chain_id=mention.chain_id, path_position=mention.path_position,
        )

    @staticmethod
    def _concept(value: str) -> CanonicalConcept | None:
        try:
            return CanonicalConcept(value)
        except ValueError:
            return None

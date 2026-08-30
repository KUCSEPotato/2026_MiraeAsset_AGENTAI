from __future__ import annotations

from app.domain.models import (
    CanonicalConcept,
    CanonicalSemanticValue,
    ConceptCategory,
    GroundedConcept,
    GroundedField,
    GroundedFilter,
    GroundedQuery,
    GroundedRelation,
    GroundedSort,
    GroundingStatus,
    RelationMention,
    ResolvedQuery,
    SemanticCapabilityState,
)
from app.ontology.constraint_status import grounded_constraint_statuses
from app.ontology.index import FP, OntologyResolution, OntologyTerm
from app.ontology.loader import LoadedOntology
from app.ontology.registry import StaticSemanticRegistry
from app.ontology.runtime_mapping import (
    DATASET_SNAPSHOT,
    ONTOLOGY_URI,
    ONTOLOGY_VERSION,
    SEMANTIC_MAPPING_VERSION,
    TeamOntologyRuntimeMapping,
)


class RDFOntologyService:
    def __init__(self, ontology: LoadedOntology) -> None:
        self.ontology = ontology
        self.index = ontology.index
        self.runtime_mapping = (
            ontology.index.runtime_mapping
            if ontology.index.runtime_mapping is not None
            else None
        )
        self._legacy_registry = StaticSemanticRegistry()

    @property
    def ontology_files(self):
        """Compatibility view for diagnostics that predate LoadedOntology."""
        return self.ontology.files

    @property
    def is_team_ontology(self) -> bool:
        return self.ontology.version == "team-v1"

    def resolve_alias(
        self,
        raw: str,
        category: ConceptCategory | str | None = None,
    ) -> OntologyResolution:
        resolution = self.index.resolve_alias(raw, category)
        if self.is_team_ontology or resolution.status is not GroundingStatus.UNRESOLVED:
            return resolution
        if not isinstance(category, ConceptCategory):
            try:
                category = ConceptCategory(category)
            except (TypeError, ValueError):
                return resolution
        canonical = self._legacy_registry.resolve_concept(raw, category)
        if canonical is None:
            return resolution
        terms = tuple(
            item
            for item in self.index.terms(category.value)
            if item.semantic_value is canonical
        )
        return (
            OntologyResolution(raw, GroundingStatus.RESOLVED, (terms[0],))
            if len(terms) == 1
            else resolution
        )

    def resolve_concept(
        self,
        raw: str,
        category: ConceptCategory | str,
    ):
        return self.resolve_alias(raw, category).canonical_concept

    def resolve_field(self, raw: str) -> OntologyResolution:
        semantic_slot = {
            "region": "product.region",
            "asset_type": "product.asset_type",
            "product_type": "product.product_type",
            "aum": "product.aum",
            "expense_ratio": "product.expense_ratio",
            "credit_rating": "product.credit_rating",
            "current_sale_available": "product.current_sale_available",
            "listing_country": "product.listing_country",
            "currency": "product.currency",
        }.get(raw, raw)
        resolution = self.index.resolve_alias(semantic_slot, "field")
        if self.is_team_ontology or resolution.status is not GroundingStatus.UNRESOLVED:
            return resolution
        canonical_field = self._legacy_registry.map_field(semantic_slot)
        if canonical_field is None:
            return resolution
        declared = self.index.resolve_alias(canonical_field, "field")
        if declared.status is GroundingStatus.RESOLVED:
            return OntologyResolution(raw, declared.status, declared.terms)
        resource = _LEGACY_FIELD_RESOURCES.get(canonical_field)
        if resource is None:
            return resolution
        uri = str(FP[resource])
        if uri not in self.index.object_properties | self.index.data_properties:
            return resolution
        return OntologyResolution(
            raw,
            GroundingStatus.RESOLVED,
            (
                OntologyTerm(
                    uri=uri,
                    canonical_name=canonical_field,
                    aliases=(raw, canonical_field),
                    category="field",
                    canonical_field=canonical_field,
                ),
            ),
        )

    def map_field(self, raw: str) -> str | None:
        return self.resolve_field(raw).canonical_field

    def resolve_relation(self, raw: str) -> OntologyResolution:
        if isinstance(self.runtime_mapping, TeamOntologyRuntimeMapping):
            mapping = self.runtime_mapping.relation(raw)
            if (
                mapping is not None
                and mapping.capability is not SemanticCapabilityState.ACTIVE
            ):
                return OntologyResolution(raw, GroundingStatus.UNRESOLVED)
        return self.index.resolve_alias(raw, "relation")

    def is_compatible(
        self, subject_type: str, relation: str, object_type: str
    ) -> bool:
        return self.index.is_compatible(subject_type, relation, object_type)

    def get_allowed_relations(self, subject_type: str) -> tuple[str, ...]:
        return self.index.allowed_relations(subject_type)

    async def ground(self, query: ResolvedQuery) -> GroundedQuery:
        parsed = query.parsed_query
        concepts = []
        grounded_concepts: list[GroundedConcept] = []
        unresolved: list[str] = []

        for raw in parsed.product_types:
            is_public_fund_alias = (
                self.is_team_ontology and raw.replace(" ", "") == "공모펀드"
            )
            # Public offering is a classification, not a Product subclass in
            # Team Ontology v1.  The established compatibility phrase therefore
            # grounds compositionally to Fund plus OfferingType.PUBLIC.
            grounded = self._ground_concept(
                "펀드" if is_public_fund_alias else raw,
                ConceptCategory.PRODUCT_TYPE,
            )
            if is_public_fund_alias:
                grounded = grounded.model_copy(update={"raw_text": raw})
            grounded_concepts.append(grounded)
            self._collect(grounded, concepts, unresolved)
            if is_public_fund_alias:
                offering = self._ground_concept(
                    "공모", ConceptCategory.OFFERING_TYPE
                )
                grounded_concepts.append(offering)
                self._collect(offering, concepts, unresolved)

        for raw in parsed.semantic_terms:
            grounded = self._ground_concept(raw, ConceptCategory.SEMANTIC_TERM)
            grounded_concepts.append(grounded)
            # Narrative conditions deliberately remain retrieval text; an
            # unresolved ontology concept must not discard semantic search.
            if grounded.status is GroundingStatus.RESOLVED:
                self._collect(grounded, concepts, unresolved)

        grounded_filters: list[GroundedFilter] = []
        canonical_fields: dict[str, str] = {}
        category_by_field = {
            "region": (
                ConceptCategory.EXPOSURE_REGION
                if self.is_team_ontology
                else ConceptCategory.REGION
            ),
            "asset_type": (
                ConceptCategory.ASSET_CLASS
                if self.is_team_ontology
                else ConceptCategory.ASSET_TYPE
            ),
            "product_type": ConceptCategory.PRODUCT_TYPE,
            "offering_type": ConceptCategory.OFFERING_TYPE,
        }
        for item in parsed.filters:
            field_resolution = self.resolve_field(item.field)
            field = field_resolution.canonical_field
            category = category_by_field.get(item.field)
            raw_values = item.value if isinstance(item.value, list) else [item.value]
            value_resolutions = (
                [self.resolve_alias(str(value), category) for value in raw_values]
                if category is not None
                else []
            )
            values = [
                resolution.canonical_concept
                for resolution in value_resolutions
                if resolution.status is GroundingStatus.RESOLVED
                and resolution.canonical_concept is not None
            ]
            status = field_resolution.status
            if category is not None:
                if any(
                    resolution.status is GroundingStatus.AMBIGUOUS
                    for resolution in value_resolutions
                ):
                    status = GroundingStatus.AMBIGUOUS
                elif len(values) != len(raw_values):
                    status = GroundingStatus.UNRESOLVED
            grounded_filters.append(
                GroundedFilter(
                    raw_filter=item,
                    canonical_field=field,
                    canonical_value=values[0] if values else None,
                    canonical_values=values,
                    status=status,
                )
            )
            if field is not None:
                canonical_fields[item.field] = field
            if category is not None:
                for raw_value, resolution in zip(
                    raw_values, value_resolutions, strict=True
                ):
                    value_concept = GroundedConcept(
                        raw_text=str(raw_value),
                        category=category,
                        canonical_concept=resolution.canonical_concept,
                        status=resolution.status,
                    )
                    grounded_concepts.append(value_concept)
                    self._collect(value_concept, concepts, unresolved)

        grounded_sort = [self._ground_sort(item) for item in parsed.sort]
        grounded_requested = [
            self._ground_field(item) for item in parsed.requested_fields
        ]
        for item in (*grounded_sort, *grounded_requested):
            raw = (
                item.raw_sort.field
                if isinstance(item, GroundedSort)
                else item.raw_text
            )
            if item.canonical_field is not None:
                canonical_fields[raw] = item.canonical_field

        relations = [self._ground_relation(item) for item in parsed.relations]
        constraints = grounded_constraint_statuses(
            parsed,
            resolved_entities=query.resolved_entities,
            filters=grounded_filters,
            sorts=grounded_sort,
            requested_fields=grounded_requested,
            relations=relations,
        )
        return GroundedQuery(
            parsed_query=parsed,
            resolved_entities=query.resolved_entities,
            canonical_concepts=list(dict.fromkeys(concepts)),
            canonical_fields=canonical_fields,
            grounded_concepts=grounded_concepts,
            grounded_filters=grounded_filters,
            grounded_sort=grounded_sort,
            grounded_requested_fields=grounded_requested,
            grounded_relations=relations,
            unresolved_concepts=unresolved,
            semantic_constraints=constraints,
            ontology_uri=(ONTOLOGY_URI if self.is_team_ontology else None),
            ontology_version=(ONTOLOGY_VERSION if self.is_team_ontology else "legacy"),
            semantic_mapping_version=(
                SEMANTIC_MAPPING_VERSION if self.is_team_ontology else None
            ),
            dataset_snapshot=DATASET_SNAPSHOT,
        )

    def _ground_concept(
        self,
        raw: str,
        category: ConceptCategory,
    ) -> GroundedConcept:
        resolution = self.resolve_alias(raw, category)
        return GroundedConcept(
            raw_text=raw,
            category=category,
            canonical_concept=resolution.canonical_concept,
            status=resolution.status,
        )

    def _ground_sort(self, item) -> GroundedSort:
        resolution = self.resolve_field(item.field)
        mapping = self._field_mapping(item.field)
        executable = mapping is None or bool(
            {"sort", "sort_contract"} & mapping.operations
        )
        return GroundedSort(
            raw_sort=item,
            canonical_field=(
                resolution.canonical_field if executable else None
            ),
            status=(
                resolution.status
                if executable
                else GroundingStatus.UNRESOLVED
            ),
        )

    def _ground_field(self, raw: str) -> GroundedField:
        resolution = self.resolve_field(raw)
        mapping = self._field_mapping(raw)
        executable = mapping is None or "project" in mapping.operations
        return GroundedField(
            raw_text=raw,
            canonical_field=(
                resolution.canonical_field if executable else None
            ),
            ontology_uri=resolution.uri if executable else None,
            mapping_version=(
                SEMANTIC_MAPPING_VERSION
                if self.is_team_ontology and executable
                else None
            ),
            status=(
                resolution.status
                if executable
                else GroundingStatus.UNRESOLVED
            ),
        )

    def _ground_relation(self, raw) -> GroundedRelation:
        mention = (
            raw
            if isinstance(raw, RelationMention)
            else RelationMention(raw_text=str(raw))
        )
        resolution = self.resolve_relation(mention.raw_text)
        relation = (
            self.index.local_name(resolution.uri)
            if resolution.status is GroundingStatus.RESOLVED
            and resolution.uri is not None
            else None
        )
        relation_status = resolution.status
        target_value = mention.target_value
        target_category = {
            "RiskGrade": "risk_grade",
            "BondType": "bond_type",
            "MarketScope": "market_scope",
        }.get(mention.target_type or "")
        if (
            target_category is not None
            and target_value is not None
            and isinstance(self.runtime_mapping, TeamOntologyRuntimeMapping)
        ):
            target_mapping = self.runtime_mapping.concept(
                str(target_value), target_category
            )
            semantic = (
                target_mapping.semantic_value()
                if target_mapping is not None
                else None
            )
            if semantic is None:
                relation = None
                relation_status = GroundingStatus.UNRESOLVED
            else:
                target_value = semantic.runtime_key
        return GroundedRelation(
            raw_text=mention.raw_text,
            canonical_relation=relation,
            ontology_uri=resolution.uri,
            direction=mention.direction,
            status=relation_status,
            constraint_id=mention.constraint_id,
            subject_type=mention.subject_type,
            target_raw_text=mention.target_raw_text,
            target_type=mention.target_type,
            target_value=target_value,
            negated=mention.negated,
            chain_id=mention.chain_id,
            path_position=mention.path_position,
        )

    def _field_mapping(self, raw: str):
        if not isinstance(self.runtime_mapping, TeamOntologyRuntimeMapping):
            return None
        return self.runtime_mapping.field(raw)

    @staticmethod
    def _collect(concept, values: list, unresolved: list[str]) -> None:
        if concept.canonical_concept is not None:
            if concept.canonical_concept not in values:
                values.append(concept.canonical_concept)
        elif concept.raw_text not in unresolved:
            unresolved.append(concept.raw_text)


_LEGACY_FIELD_RESOURCES = {
    "product.name": "productName",
    "product.asset_manager": "managedBy",
    "product.issuer": "issuedBy",
    "product.base_index": "tracks",
    "product.currency": "denominatedIn",
    "product.risk_grade": "hasRiskGrade",
}

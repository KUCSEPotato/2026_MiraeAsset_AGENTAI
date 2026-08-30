from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from rdflib import Graph, Namespace, OWL, RDF, RDFS, SKOS, URIRef

from app.domain.models import (
    CanonicalConcept,
    CanonicalSemanticValue,
    ConceptCategory,
    GroundingStatus,
)
from app.ontology.models import OntologyLoadError


ONTOLOGY_NAMESPACE = "https://miraeasset.com/ontology/financial-product#"
FP = Namespace(ONTOLOGY_NAMESPACE)


def normalize_ontology_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    return re.sub(r"[\s_\-]+", "", normalized)


@dataclass(frozen=True)
class OntologyTerm:
    uri: str
    canonical_name: str
    aliases: tuple[str, ...]
    category: str | None = None
    canonical_field: str | None = None
    semantic_value: CanonicalSemanticValue | CanonicalConcept | None = None


@dataclass(frozen=True)
class OntologyResolution:
    raw_text: str
    status: GroundingStatus
    terms: tuple[OntologyTerm, ...] = ()

    @property
    def uri(self) -> str | None:
        return self.terms[0].uri if len(self.terms) == 1 else None

    @property
    def uris(self) -> tuple[str, ...]:
        return tuple(item.uri for item in self.terms)

    @property
    def canonical_concept(self):
        return self.terms[0].semantic_value if len(self.terms) == 1 else None

    @property
    def canonical_field(self) -> str | None:
        return self.terms[0].canonical_field if len(self.terms) == 1 else None


class OntologyIndex:
    """Category-scoped, ambiguity-safe index over one ontology graph."""

    def __init__(
        self,
        graph: Graph,
        runtime_mapping=None,
        *,
        allow_unscoped_domainless: bool = False,
    ) -> None:
        self.graph = graph
        self.runtime_mapping = runtime_mapping
        self._allow_unscoped_domainless = allow_unscoped_domainless
        self.object_properties = frozenset(
            str(item) for item in graph.subjects(RDF.type, OWL.ObjectProperty)
        )
        self.data_properties = frozenset(
            str(item) for item in graph.subjects(RDF.type, OWL.DatatypeProperty)
        )
        self.datatype_properties = self.data_properties
        self.classes = frozenset(
            str(item) for item in graph.subjects(RDF.type, OWL.Class)
        )
        self._parents = self._build_parent_closure()
        self._by_scope: dict[tuple[str | None, str], list[OntologyTerm]] = (
            defaultdict(list)
        )
        self._terms_by_identity: dict[
            tuple[str, str | None, str | None], OntologyTerm
        ] = {}
        self._build_graph_terms()
        if runtime_mapping is not None:
            self._build_runtime_terms(runtime_mapping)
        self._validate_collisions()
        self.field_mappings = {
            term.canonical_field: term.uri
            for term in self._terms_by_identity.values()
            if term.canonical_field is not None
        }

    @staticmethod
    def local_name(uri: str) -> str:
        return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]

    def resolve(
        self,
        value: str,
        category: ConceptCategory | str | None = None,
    ) -> OntologyTerm | None:
        resolution = self.resolve_alias(value, category)
        return (
            resolution.terms[0]
            if resolution.status is GroundingStatus.RESOLVED
            else None
        )

    def resolve_alias(
        self,
        value: str,
        category: ConceptCategory | str | None = None,
    ) -> OntologyResolution:
        key = category.value if isinstance(category, ConceptCategory) else category
        normalized = normalize_ontology_text(value)
        if self.runtime_mapping is not None and key not in {None, "field", "relation"}:
            mapped = self.runtime_mapping.concept(value, key)
            semantic = mapped.semantic_value() if mapped is not None else None
            if semantic is not None and semantic.ontology_uri in _declared_resources(
                self.graph
            ):
                return OntologyResolution(
                    value,
                    GroundingStatus.RESOLVED,
                    (
                        OntologyTerm(
                            uri=semantic.ontology_uri,
                            canonical_name=semantic.canonical_name,
                            aliases=mapped.aliases,
                            category=semantic.category,
                            semantic_value=semantic,
                        ),
                    ),
                )
        candidates = list(self._by_scope.get((key, normalized), ()))
        if not candidates and key in {
            ConceptCategory.REGION.value,
            ConceptCategory.ASSET_TYPE.value,
        }:
            migrated = {
                ConceptCategory.REGION.value: ConceptCategory.EXPOSURE_REGION.value,
                ConceptCategory.ASSET_TYPE.value: ConceptCategory.ASSET_CLASS.value,
            }[key]
            candidates = list(self._by_scope.get((migrated, normalized), ()))
        if key is None:
            candidates = _deduplicate_terms(
                term
                for (scope, alias), terms in self._by_scope.items()
                if alias == normalized and scope is not None
                for term in terms
            )
        else:
            candidates = _deduplicate_terms(candidates)
        if not candidates:
            return OntologyResolution(value, GroundingStatus.UNRESOLVED)
        if len(candidates) > 1:
            return OntologyResolution(
                value, GroundingStatus.AMBIGUOUS, tuple(candidates)
            )
        return OntologyResolution(
            value, GroundingStatus.RESOLVED, (candidates[0],)
        )

    def terms(self, category: str | None = None) -> tuple[OntologyTerm, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._terms_by_identity.values()
                    if category is None or item.category == category
                ),
                key=lambda item: (item.canonical_name, item.uri),
            )
        )

    def parents(self, uri: str) -> set[str]:
        return set(self._parents.get(uri, frozenset()))

    def domains(self, relation: str) -> tuple[str, ...]:
        uri = URIRef(relation) if "://" in relation else FP[relation]
        declared = tuple(str(item) for item in self.graph.objects(uri, RDFS.domain))
        if declared:
            return declared
        if self._allow_unscoped_domainless:
            return ()
        return tuple(
            f"{ONTOLOGY_NAMESPACE}{name}"
            for name in _DOMAINLESS_RUNTIME_POLICY.get(
                self.local_name(str(uri)), ()
            )
        )

    def ranges(self, relation: str) -> tuple[str, ...]:
        uri = URIRef(relation) if "://" in relation else FP[relation]
        return tuple(str(item) for item in self.graph.objects(uri, RDFS.range))

    def is_compatible(
        self,
        subject_type: str,
        relation: str,
        object_type: str,
    ) -> bool:
        relation_uri = URIRef(relation) if "://" in relation else FP[relation]
        if str(relation_uri) not in self.object_properties:
            return False
        domains = self.domains(str(relation_uri))
        ranges = self.ranges(str(relation_uri))
        if not ranges:
            return False
        subject = _as_ontology_uri(subject_type)
        object_ = _as_ontology_uri(object_type)
        subject_ok = (
            self._allow_unscoped_domainless
            if not domains
            else any(
                self._is_class_or_subclass(subject, URIRef(item))
                for item in domains
            )
        )
        return subject_ok and any(
            self._is_class_or_subclass(object_, URIRef(item))
            for item in ranges
        )

    def allowed_relations(self, subject_type: str) -> tuple[str, ...]:
        allowed = []
        for relation in self.object_properties:
            domains = self.domains(relation)
            if (
                (not domains and self._allow_unscoped_domainless)
                or domains and any(
                self._is_class_or_subclass(
                    _as_ontology_uri(subject_type), URIRef(domain)
                )
                for domain in domains
                )
            ):
                allowed.append(relation)
        return tuple(sorted(allowed))

    def _is_class_or_subclass(self, candidate: URIRef, expected: URIRef) -> bool:
        return candidate == expected or str(expected) in self._parents.get(
            str(candidate), frozenset()
        )

    def _build_parent_closure(self) -> dict[str, frozenset[str]]:
        direct = {
            str(cls): {
                str(parent)
                for parent in self.graph.objects(cls, RDFS.subClassOf)
                if isinstance(parent, URIRef)
            }
            for cls in self.graph.subjects(RDF.type, OWL.Class)
        }
        result: dict[str, frozenset[str]] = {}
        for cls in direct:
            visited: set[str] = set()
            pending = list(direct.get(cls, ()))
            while pending:
                parent = pending.pop()
                if parent in visited:
                    continue
                visited.add(parent)
                pending.extend(direct.get(parent, ()))
            result[cls] = frozenset(visited)
        return result

    def _build_graph_terms(self) -> None:
        subjects = set(self.graph.subjects(FP.canonicalName, None))
        subjects.update(self.graph.subjects(FP.canonicalField, None))
        subjects.update(self.graph.subjects(RDFS.label, None))
        subjects.update(self.graph.subjects(SKOS.prefLabel, None))
        subjects.update(self.graph.subjects(SKOS.altLabel, None))
        for subject in subjects:
            if not isinstance(subject, URIRef):
                continue
            category_value = self.graph.value(subject, FP.conceptCategory)
            category = (
                str(category_value)
                if category_value
                else _infer_category(self.graph, subject)
            )
            canonical_literal = self.graph.value(subject, FP.canonicalName)
            canonical_field = self.graph.value(subject, FP.canonicalField)
            canonical = str(
                canonical_literal
                or canonical_field
                or self.local_name(str(subject))
            )
            labels = {canonical, self.local_name(str(subject))}
            for predicate in (
                RDFS.label,
                SKOS.prefLabel,
                SKOS.altLabel,
                FP.alias,
            ):
                labels.update(
                    str(value) for value in self.graph.objects(subject, predicate)
                )
            self._add_term(
                OntologyTerm(
                    uri=str(subject),
                    canonical_name=canonical,
                    aliases=tuple(sorted(labels)),
                    category=category,
                    canonical_field=(
                        str(canonical_field) if canonical_field else None
                    ),
                    semantic_value=_legacy_semantic_value(canonical),
                )
            )

    def _build_runtime_terms(self, mapping) -> None:
        declared = _declared_resources(self.graph)
        for item in mapping.concepts:
            semantic = item.semantic_value()
            if semantic is None or semantic.ontology_uri not in declared:
                continue
            self._add_term(
                OntologyTerm(
                    uri=semantic.ontology_uri,
                    canonical_name=semantic.canonical_name,
                    aliases=tuple(
                        dict.fromkeys(
                            (
                                *item.aliases,
                                item.canonical_name,
                                item.runtime_key,
                                *item.legacy_names,
                            )
                        )
                    ),
                    category=item.category,
                    semantic_value=semantic,
                )
            )
        for item in mapping.fields:
            if item.capability.value != "active":
                continue
            self._add_term(
                OntologyTerm(
                    uri=item.ontology_uri,
                    canonical_name=item.canonical_field,
                    aliases=tuple(
                        dict.fromkeys((*item.aliases, item.canonical_field))
                    ),
                    category="field",
                    canonical_field=item.canonical_field,
                )
            )
        for item in mapping.relations:
            if item.capability.value != "active":
                continue
            self._add_term(
                OntologyTerm(
                    uri=item.ontology_uri,
                    canonical_name=item.canonical_relation,
                    aliases=tuple(
                        dict.fromkeys(
                            (
                                *item.aliases,
                                *item.legacy_names,
                                item.canonical_relation,
                            )
                        )
                    ),
                    category="relation",
                )
            )

    def _add_term(self, term: OntologyTerm) -> None:
        key = _term_identity(term)
        existing = self._terms_by_identity.get(key)
        if existing is not None:
            term = OntologyTerm(
                uri=term.uri,
                canonical_name=term.canonical_name,
                aliases=tuple(sorted(set(existing.aliases) | set(term.aliases))),
                category=term.category or existing.category,
                canonical_field=term.canonical_field or existing.canonical_field,
                semantic_value=term.semantic_value or existing.semantic_value,
            )
        self._terms_by_identity[key] = term
        for alias in term.aliases:
            self._by_scope[
                (term.category, normalize_ontology_text(alias))
            ].append(term)

    def _validate_collisions(self) -> None:
        for (category, _), terms in self._by_scope.items():
            if category is None:
                continue
            unique = _deduplicate_terms(terms)
            if len(unique) <= 1:
                continue
            if all(
                bool(self.graph.value(URIRef(item.uri), FP.allowAmbiguousAlias))
                for item in unique
            ):
                continue
            raise OntologyLoadError(
                "ambiguous ontology alias: "
                + ",".join(sorted(item.uri for item in unique))
            )


def _as_ontology_uri(value: str) -> URIRef:
    return URIRef(value) if "://" in value else FP[value]


def _deduplicate_terms(items: Iterable[OntologyTerm]) -> list[OntologyTerm]:
    return list({_term_identity(item): item for item in items}.values())


def _term_identity(term: OntologyTerm) -> tuple[str, str | None, str | None]:
    runtime_field = (
        term.canonical_field if term.category == "field" else None
    )
    return term.uri, term.category, runtime_field


def _declared_resources(graph: Graph) -> set[str]:
    return {
        str(subject)
        for subject in graph.subjects(RDF.type, None)
        if isinstance(subject, URIRef)
    }


def _infer_category(graph: Graph, subject: URIRef) -> str | None:
    categories = {
        "OfferingType": ConceptCategory.OFFERING_TYPE.value,
        "ExposureRegion": ConceptCategory.EXPOSURE_REGION.value,
        "AssetClass": ConceptCategory.ASSET_CLASS.value,
        "RiskGrade": ConceptCategory.CLASSIFICATION.value,
        "ManagementStyle": ConceptCategory.CLASSIFICATION.value,
        "BondType": ConceptCategory.CLASSIFICATION.value,
    }
    for rdf_type in graph.objects(subject, RDF.type):
        category = categories.get(OntologyIndex.local_name(str(rdf_type)))
        if category is not None:
            return category
    return None


def _legacy_semantic_value(canonical: str) -> CanonicalConcept | None:
    try:
        return CanonicalConcept(canonical)
    except ValueError:
        return None


_DOMAINLESS_RUNTIME_POLICY: dict[str, tuple[str, ...]] = {
    "managedBy": ("ETF", "Fund"),
    "issuedBy": ("ETN", "Bond"),
    "denominatedIn": ("FinancialProduct",),
    "hasAssetClass": ("FinancialProduct", "FundShareClass"),
    "hasExposureRegion": ("FinancialProduct", "FundShareClass"),
    "hasRiskGrade": ("FinancialProduct", "FundShareClass"),
    "hasOfferingType": ("Bond", "Fund", "FundShareClass"),
    "hasDistributionFrequency": ("ExchangeTradedProduct", "FundShareClass"),
    "hasIncomeDistributionType": ("Fund", "FundShareClass"),
    "hasInvestorType": ("Fund", "FundShareClass"),
    "hasMarketScope": ("FinancialProduct", "FundShareClass"),
    "supportedByRecord": ("EvidenceBearingEntity",),
    "primaryStore": ("MetricFamily",),
    "onFailureReason": ("OperationalConstraint",),
    "onFailureStatus": ("OperationalConstraint",),
}

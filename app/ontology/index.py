from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rdflib import Graph, Namespace, OWL, RDF, RDFS, URIRef

FP = Namespace("https://miraeasset.com/ontology/financial-product#")


def normalize_ontology_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    return re.sub(r"[\s_\-]+", "", normalized)


@dataclass(frozen=True)
class OntologyTerm:
    uri: str
    canonical_name: str
    aliases: tuple[str, ...]
    category: str | None = None


class OntologyIndex:
    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.object_properties = frozenset(
            str(item) for item in graph.subjects(RDF.type, OWL.ObjectProperty)
        )
        self.data_properties = frozenset(
            str(item) for item in graph.subjects(RDF.type, OWL.DatatypeProperty)
        )
        self.classes = frozenset(str(item) for item in graph.subjects(RDF.type, OWL.Class))
        self._aliases: dict[str, OntologyTerm] = {}
        for subject in set(graph.subjects(FP.canonicalName, None)):
            canonical = str(graph.value(subject, FP.canonicalName))
            category_value = graph.value(subject, FP.conceptCategory)
            labels = {canonical, self.local_name(str(subject))}
            labels.update(str(value) for value in graph.objects(subject, RDFS.label))
            labels.update(str(value) for value in graph.objects(subject, FP.alias))
            term = OntologyTerm(
                uri=str(subject), canonical_name=canonical,
                aliases=tuple(sorted(labels)),
                category=str(category_value) if category_value else None,
            )
            for label in labels:
                self._aliases[normalize_ontology_text(label)] = term

    @staticmethod
    def local_name(uri: str) -> str:
        return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]

    def resolve(self, value: str) -> OntologyTerm | None:
        return self._aliases.get(normalize_ontology_text(value))

    def terms(self, category: str | None = None) -> tuple[OntologyTerm, ...]:
        unique = {term.uri: term for term in self._aliases.values()}
        return tuple(
            sorted(
                (term for term in unique.values() if category is None or term.category == category),
                key=lambda item: item.canonical_name,
            )
        )

    def is_compatible(self, subject_type: str, relation: str, object_type: str) -> bool:
        relation_uri = URIRef(relation) if "://" in relation else FP[relation]
        domain = self.graph.value(relation_uri, RDFS.domain)
        range_ = self.graph.value(relation_uri, RDFS.range)
        if domain is None or range_ is None:
            return False
        return self._is_class_or_subclass(FP[subject_type], domain) and self._is_class_or_subclass(
            FP[object_type], range_
        )

    def _is_class_or_subclass(self, candidate: URIRef, expected: URIRef) -> bool:
        if candidate == expected:
            return True
        visited: set[URIRef] = set()
        pending = [candidate]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            parents = list(self.graph.objects(current, RDFS.subClassOf))
            if expected in parents:
                return True
            pending.extend(parent for parent in parents if isinstance(parent, URIRef))
        return False

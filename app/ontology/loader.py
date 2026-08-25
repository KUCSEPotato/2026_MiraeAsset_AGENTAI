from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph

from app.ontology.index import FP, OntologyIndex
from app.ontology.models import OntologyLoadError

MANDATORY_ONTOLOGY_FILES = (
    "core.ttl", "products.ttl", "entities.ttl", "observations.ttl", "mappings.ttl"
)


@dataclass(frozen=True)
class LoadedOntology:
    graph: Graph
    index: OntologyIndex
    files: tuple[Path, ...]


class OntologyLoader:
    def __init__(self, root: Path, *, known_canonical_fields: set[str] | frozenset[str] = frozenset()) -> None:
        self.root = root
        self.known_canonical_fields = frozenset(known_canonical_fields)

    def load(self) -> LoadedOntology:
        files = tuple(self.root / name for name in MANDATORY_ONTOLOGY_FILES)
        missing = [path.name for path in files if not path.is_file()]
        if missing:
            raise OntologyLoadError(f"missing mandatory ontology files: {missing}")
        graph = Graph()
        try:
            for path in files:
                graph.parse(path, format="turtle")
        except Exception as exc:
            raise OntologyLoadError(f"failed to parse ontology: {exc}") from exc
        index = OntologyIndex(graph)
        declared_fields = {
            str(value) for value in graph.objects(None, FP.canonicalField)
        }
        unknown = declared_fields - self.known_canonical_fields if self.known_canonical_fields else set()
        if unknown:
            raise OntologyLoadError(f"unknown canonical fields: {sorted(unknown)}")
        return LoadedOntology(graph=graph, index=index, files=files)

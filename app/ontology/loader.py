from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph
from rdflib import OWL, RDF

from app.ontology.index import FP, OntologyIndex
from app.ontology.models import OntologyLoadError
from app.ontology.runtime_mapping import (
    ONTOLOGY_URI,
    ONTOLOGY_VERSION,
    SEMANTIC_MAPPING_VERSION,
    TeamOntologyRuntimeMapping,
)

LEGACY_ONTOLOGY_FILES = (
    "core.ttl", "products.ttl", "entities.ttl", "observations.ttl", "mappings.ttl"
)
TEAM_V1_ONTOLOGY_FILES = ("candidates/new_optical_ontology.ttl",)
V7_ONTOLOGY_FILES = TEAM_V1_ONTOLOGY_FILES
MANDATORY_ONTOLOGY_FILES = LEGACY_ONTOLOGY_FILES


@dataclass(frozen=True)
class LoadedOntology:
    graph: Graph
    index: OntologyIndex
    files: tuple[Path, ...]
    version: str = "legacy"
    ontology_uri: str | None = None
    ontology_version: str | None = None
    semantic_mapping_version: str | None = None


class OntologyLoader:
    def __init__(
        self,
        root: Path,
        *,
        known_canonical_fields: set[str] | frozenset[str] | None = None,
        version: str = "legacy",
    ) -> None:
        self.root = root
        self.known_canonical_fields = (
            None
            if known_canonical_fields is None
            else frozenset(known_canonical_fields)
        )
        aliases = {"team_v1": "team-v1"}
        normalized_version = aliases.get(version, version)
        if normalized_version not in {"legacy", "v7", "team-v1"}:
            raise ValueError(
                "ontology version must be 'legacy', 'v7', or 'team-v1'"
            )
        self.version = normalized_version
        self.load_count = 0
        self._loaded: LoadedOntology | None = None

    def load(self) -> LoadedOntology:
        if self._loaded is not None:
            return self._loaded
        names = (
            TEAM_V1_ONTOLOGY_FILES
            if self.version in {"v7", "team-v1"}
            else LEGACY_ONTOLOGY_FILES
        )
        files = tuple(self.root / name for name in names)
        missing = [path.name for path in files if not path.is_file()]
        if missing:
            raise OntologyLoadError(f"missing mandatory ontology files: {missing}")
        graph = Graph()
        try:
            for path in files:
                graph.parse(path, format="turtle")
        except Exception as exc:
            raise OntologyLoadError(f"failed to parse ontology: {exc}") from exc
        runtime_mapping = (
            TeamOntologyRuntimeMapping() if self.version == "team-v1" else None
        )
        index = OntologyIndex(
            graph,
            runtime_mapping=runtime_mapping,
            allow_unscoped_domainless=self.version == "v7",
        )
        declared_fields = {
            str(value) for value in graph.objects(None, FP.canonicalField)
        }
        unknown = (
            declared_fields - self.known_canonical_fields
            if self.known_canonical_fields is not None
            else set()
        )
        if unknown:
            raise OntologyLoadError(f"unknown canonical fields: {sorted(unknown)}")
        ontology_uri = None
        ontology_version = None
        semantic_mapping_version = None
        if self.version == "team-v1":
            ontology_subjects = list(graph.subjects(RDF.type, OWL.Ontology))
            if len(ontology_subjects) != 1 or str(ontology_subjects[0]) != ONTOLOGY_URI:
                raise OntologyLoadError("unexpected Team Ontology URI")
            actual_version = graph.value(ontology_subjects[0], OWL.versionInfo)
            if str(actual_version) != ONTOLOGY_VERSION:
                raise OntologyLoadError(
                    "unexpected Team Ontology version: " + str(actual_version)
                )
            ontology_uri = ONTOLOGY_URI
            ontology_version = ONTOLOGY_VERSION
            semantic_mapping_version = SEMANTIC_MAPPING_VERSION
        self.load_count += 1
        self._loaded = LoadedOntology(
            graph=graph,
            index=index,
            files=files,
            version=self.version,
            ontology_uri=ontology_uri,
            ontology_version=ontology_version,
            semantic_mapping_version=semantic_mapping_version,
        )
        return self._loaded

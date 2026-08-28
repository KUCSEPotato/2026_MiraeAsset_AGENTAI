import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GraphSettings:
    uri: str | None = None
    user: str = "neo4j"
    password: str = ""
    database: str = "neo4j"
    graph_version: str = "m10-minimal-graph-v1"
    ontology_version: str = "legacy"
    batch_size: int = 1_000
    query_limit: int = 100
    max_depth: int = 2

    @classmethod
    def from_env(cls, *, require_uri: bool = False) -> "GraphSettings":
        uri = os.getenv("NEO4J_URI")
        if require_uri and not uri:
            raise ValueError("NEO4J_URI is required for the real Graph runtime")
        settings = cls(
            uri=uri,
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", ""),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
            graph_version=os.getenv(
                "GRAPH_VERSION", "m10-minimal-graph-v1"
            ),
            ontology_version=os.getenv("ONTOLOGY_VERSION", "legacy"),
            batch_size=int(os.getenv("GRAPH_INGEST_BATCH_SIZE", "1000")),
            query_limit=int(os.getenv("GRAPH_QUERY_LIMIT", "100")),
            max_depth=int(os.getenv("GRAPH_MAX_DEPTH", "2")),
        )
        if settings.ontology_version not in {"legacy", "v7"}:
            raise ValueError("ONTOLOGY_VERSION must be 'legacy' or 'v7'")
        if bool(settings.uri) != bool(settings.password):
            raise ValueError(
                "NEO4J_URI and NEO4J_PASSWORD must be configured together"
            )
        if settings.batch_size <= 0:
            raise ValueError("GRAPH_INGEST_BATCH_SIZE must be positive")
        if settings.query_limit <= 0:
            raise ValueError("GRAPH_QUERY_LIMIT must be positive")
        if not 1 <= settings.max_depth <= 2:
            raise ValueError("GRAPH_MAX_DEPTH must be 1 or 2")
        return settings

    @property
    def configured(self) -> bool:
        return bool(self.uri and self.password)

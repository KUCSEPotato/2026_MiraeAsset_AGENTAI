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
    connection_timeout_seconds: float = 5.0
    connection_acquisition_timeout_seconds: float = 5.0
    max_transaction_retry_seconds: float = 5.0
    v2_graph_projection_version: str = "m10.9-c2.8-canonical-v2-graph-5"
    v2_generation: str = "260824"
    v2_ontology_version: str = "merged-optical-1.4"
    v2_transformer_version: str = "m10.9-c2-kodex-holdings-1"

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
                "GRAPH_VERSION", "m10.7-team-v1-20260829"
            ),
            ontology_version=os.getenv("ONTOLOGY_VERSION", "team-v1"),
            batch_size=int(os.getenv("GRAPH_INGEST_BATCH_SIZE", "1000")),
            query_limit=int(os.getenv("GRAPH_QUERY_LIMIT", "100")),
            max_depth=int(os.getenv("GRAPH_MAX_DEPTH", "2")),
            connection_timeout_seconds=float(
                os.getenv("NEO4J_CONNECTION_TIMEOUT_SECONDS", "5")
            ),
            connection_acquisition_timeout_seconds=float(
                os.getenv("NEO4J_CONNECTION_ACQUISITION_TIMEOUT_SECONDS", "5")
            ),
            max_transaction_retry_seconds=float(
                os.getenv("NEO4J_MAX_TRANSACTION_RETRY_SECONDS", "5")
            ),
            v2_graph_projection_version=os.getenv(
                "CANONICAL_V2_GRAPH_PROJECTION_VERSION",
                "m10.9-c2.8-canonical-v2-graph-5",
            ),
            v2_generation=os.getenv("CANONICAL_V2_GENERATION", "260824"),
            v2_ontology_version=os.getenv(
                "CANONICAL_V2_ONTOLOGY_VERSION", "merged-optical-1.4"
            ),
            v2_transformer_version=os.getenv(
                "CANONICAL_V2_TRANSFORMER_VERSION", "m10.9-c2-kodex-holdings-1"
            ),
        )
        aliases = {"team_v1": "team-v1"}
        normalized_ontology = aliases.get(
            settings.ontology_version, settings.ontology_version
        )
        if normalized_ontology not in {"legacy", "v7", "team-v1"}:
            raise ValueError(
                "ONTOLOGY_VERSION must be 'legacy', 'v7', or 'team-v1'"
            )
        if normalized_ontology != settings.ontology_version:
            settings = cls(
                **{
                    **settings.__dict__,
                    "ontology_version": normalized_ontology,
                }
            )
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
        if min(
            settings.connection_timeout_seconds,
            settings.connection_acquisition_timeout_seconds,
            settings.max_transaction_retry_seconds,
        ) <= 0:
            raise ValueError("Neo4j timeout settings must be positive")
        return settings

    @property
    def configured(self) -> bool:
        return bool(self.uri and self.password)

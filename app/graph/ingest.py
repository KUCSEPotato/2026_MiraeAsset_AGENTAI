from __future__ import annotations

import argparse
import asyncio
import json

from app.data.database import (
    DatabaseSettings,
    create_database_engine,
)
from app.graph.backend import Neo4jGraphBackend
from app.graph.config import GraphSettings
from app.graph.extract import CanonicalGraphExtractor
from app.graph.mapping import GraphMappingRegistry
from pathlib import Path

from app.ontology.loader import OntologyLoader
from app.ontology.canonical_fields import ONTOLOGY_CANONICAL_FIELDS


async def ingest_graph() -> dict:
    database_settings = DatabaseSettings.from_env(require_url=True)
    graph_settings = GraphSettings.from_env(require_uri=True)
    ontology = OntologyLoader(
        Path(__file__).resolve().parents[2] / "ontology",
        known_canonical_fields=ONTOLOGY_CANONICAL_FIELDS,
        version=graph_settings.ontology_version,
    ).load()
    GraphMappingRegistry(ontology.index, version=graph_settings.ontology_version)
    engine = create_database_engine(database_settings)
    backend = Neo4jGraphBackend.connect(graph_settings)
    try:
        await backend.verify_connectivity()
        data = await asyncio.to_thread(
            CanonicalGraphExtractor(
                engine,
                snapshot=database_settings.snapshot_date,
                version=graph_settings.ontology_version,
            ).extract
        )
        metadata = await backend.build(
            data,
            dataset_snapshot=database_settings.snapshot_date,
        )
        return {
            "graph_version": metadata.graph_version,
            "dataset_snapshot": metadata.dataset_snapshot,
            "built_at": metadata.built_at.isoformat(),
            "status": metadata.status,
            "statistics": metadata.statistics,
        }
    finally:
        await backend.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the M10 Neo4j graph from the canonical RDB snapshot."
    )
    parser.parse_args()
    print(json.dumps(asyncio.run(ingest_graph()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

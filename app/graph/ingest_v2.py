"""Build the isolated Neo4j projection from a selected canonical_v2 snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.data.database import DatabaseSettings, create_database_engine
from app.retrieval.rdb_v2 import CanonicalV2SnapshotSelector
from app.graph.config import GraphSettings
from app.graph.v2 import CanonicalV2GraphBackend, CanonicalV2GraphExtractor


async def ingest_graph_v2() -> dict:
    database = DatabaseSettings.from_env()
    settings = GraphSettings.from_env(require_uri=True)
    engine = create_database_engine(database)
    try:
        with engine.connect() as connection:
            selection = CanonicalV2SnapshotSelector(
                snapshot_date=database.snapshot_date,
                generation=database.v2_generation,
                ontology_version=database.v2_ontology_version,
                transformer_version=database.v2_transformer_version,
            ).select(connection)
        extractor = CanonicalV2GraphExtractor(
            engine, snapshot_ids=selection.snapshot_ids,
            snapshot=database.snapshot_date, generation=database.v2_generation,
            ontology_version=database.v2_ontology_version,
            transformer_version=settings.v2_transformer_version,
            projection_version=settings.v2_graph_projection_version,
        )
        data = await asyncio.to_thread(extractor.extract)
        backend = CanonicalV2GraphBackend.connect(settings)
        try:
            await backend.verify_connectivity()
            manifest = await backend.build(
                data, extractor.manifest(data, status="BUILDING")
            )
        finally:
            await backend.close()
        return manifest.model_dump(mode="json")
    finally:
        engine.dispose()


def main() -> None:
    argparse.ArgumentParser(description="Build canonical_v2 Neo4j derived store").parse_args()
    print(json.dumps(asyncio.run(ingest_graph_v2()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

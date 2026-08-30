"""Build the version-isolated canonical_v2 strategy semantic index."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace

from app.data.database import DatabaseSettings, create_database_engine
from app.retrieval.rdb_v2 import CanonicalV2SnapshotSelector
from app.search.config import SearchSettings
from app.search.embedding import DeterministicMultilingualEmbeddingProvider
from app.search.indexer import SemanticIndexBuilder
from app.search.v2 import CanonicalV2StrategyDocumentBuilder, v2_semantic_manifest_factory


async def build_index_v2() -> dict:
    database = DatabaseSettings.from_env()
    settings = SearchSettings.from_env()
    effective = replace(
        settings,
        index_path=settings.v2_index_path,
        index_version=settings.v2_index_version,
    )
    engine = create_database_engine(database)
    try:
        with engine.connect() as connection:
            selection = CanonicalV2SnapshotSelector(
                snapshot_date=database.snapshot_date,
                generation=database.v2_generation,
                ontology_version=database.v2_ontology_version,
                transformer_version=database.v2_transformer_version,
            ).select(connection)
        provider = DeterministicMultilingualEmbeddingProvider(
            model_name=effective.embedding_model, dimension=effective.embedding_dimension,
        )
        result = await SemanticIndexBuilder(
            CanonicalV2StrategyDocumentBuilder(
                engine, snapshot_ids=selection.snapshot_ids,
                snapshot_date=database.snapshot_date,
            ),
            provider,
            effective,
            derived_manifest_factory=v2_semantic_manifest_factory(
                generation=database.v2_generation, snapshot=database.snapshot_date,
                ontology_version=database.v2_ontology_version,
                projection_version=effective.index_version,
            ),
        ).build()
        return result.model_dump(mode="json")
    finally:
        engine.dispose()


def main() -> None:
    argparse.ArgumentParser(description="Build canonical_v2 semantic derived store").parse_args()
    print(json.dumps(asyncio.run(build_index_v2()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

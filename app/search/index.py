import argparse
import asyncio
import json

from app.data.database import DatabaseSettings, create_database_engine
from app.search.config import SearchSettings
from app.search.documents import ForeignETFStrategyDocumentBuilder
from app.search.embedding import DeterministicMultilingualEmbeddingProvider
from app.search.indexer import SemanticIndexBuilder


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the versioned foreign ETF strategy search index."
    )
    parser.add_argument(
        "--source",
        choices=["etf_gl"],
        default="etf_gl",
        help="M9 supports the foreign ETF strategy corpus only.",
    )
    parser.parse_args()

    database_settings = DatabaseSettings.from_env(require_url=False)
    search_settings = SearchSettings.from_env()
    engine = create_database_engine(database_settings)
    provider = DeterministicMultilingualEmbeddingProvider(
        model_name=search_settings.embedding_model,
        dimension=search_settings.embedding_dimension,
    )
    builder = SemanticIndexBuilder(
        ForeignETFStrategyDocumentBuilder(
            engine,
            snapshot_date=database_settings.snapshot_date,
        ),
        provider,
        search_settings,
    )
    try:
        result = asyncio.run(builder.build())
    finally:
        engine.dispose()
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

import asyncio
import json
import time
from importlib.resources import files

from app.data.database import DatabaseSettings
from app.search.config import SearchSettings
from app.search.embedding import DeterministicMultilingualEmbeddingProvider
from app.search.store import SemanticIndexStore


async def evaluate() -> dict:
    database = DatabaseSettings.from_env()
    settings = SearchSettings.from_env()
    store = SemanticIndexStore(settings.index_path)
    store.validate(
        snapshot=database.snapshot_date,
        index_version=settings.index_version,
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
    )
    provider = DeterministicMultilingualEmbeddingProvider(
        model_name=settings.embedding_model,
        dimension=settings.embedding_dimension,
    )
    cases = json.loads(
        files("app.search").joinpath("evaluation_cases.json").read_text("utf-8")
    )
    filters = {
        "source_dataset": ["foreign_etf"],
        "source_field": ["product.strategy_description"],
        "dataset_snapshot": database.snapshot_date,
    }
    results = []
    for case in cases:
        started = time.perf_counter()
        if case["backend"] == "bm25":
            hits = await asyncio.to_thread(
                store.bm25_search,
                case["query"],
                top_k=case["top_k"],
                filters=filters,
            )
        else:
            query_vector = await provider.embed_query(case["query"])
            hits = await asyncio.to_thread(
                store.vector_search,
                query_vector,
                top_k=case["top_k"],
                filters=filters,
            )
        entity_ids = [hit.document.entity_id for hit in hits]
        expected = set(case["expected_entity_ids"])
        results.append(
            {
                "name": case["name"],
                "backend": case["backend"],
                "query": case["query"],
                "passed": bool(expected.intersection(entity_ids)),
                "expected_entity_ids": sorted(expected),
                "top_entity_ids": entity_ids,
                "latency_seconds": time.perf_counter() - started,
            }
        )
    return {
        "passed": all(result["passed"] for result in results),
        "case_count": len(results),
        "results": results,
    }


def main() -> None:
    result = asyncio.run(evaluate())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

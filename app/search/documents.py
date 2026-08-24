from dataclasses import dataclass

from sqlalchemy import Engine, func, select

from app.data.schema import canonical_products, etf_attributes
from app.search.models import SemanticDocument
from app.search.normalization import normalize_text


STRATEGY_SOURCE_FIELD = "product.strategy_description"
STRATEGY_SOURCE_DATASET = "foreign_etf"
_SENTINELS = {
    "-",
    "--",
    "n/a",
    "n.a.",
    "na",
    "none",
    "not available",
    "null",
    "미제공",
    "해당없음",
}


@dataclass(frozen=True)
class DocumentBuildStats:
    source_rows: int
    skipped_missing: int
    skipped_sentinel: int
    duplicate_texts: int


class ForeignETFStrategyDocumentBuilder:
    """Construct searchable documents from canonical RDB rows via SQLAlchemy."""

    def __init__(self, engine: Engine, *, snapshot_date: str) -> None:
        self._engine = engine
        self._snapshot_date = snapshot_date

    @property
    def snapshot_date(self) -> str:
        return self._snapshot_date

    def build(self) -> tuple[list[SemanticDocument], DocumentBuildStats]:
        conditions = (
            canonical_products.c.source_dataset == STRATEGY_SOURCE_DATASET,
            canonical_products.c.dataset_snapshot == self._snapshot_date,
        )
        statement = (
            select(
                canonical_products.c.canonical_product_id,
                canonical_products.c.source_record_key,
                canonical_products.c.source_file,
                canonical_products.c.source_row_number,
                canonical_products.c.product_name,
                canonical_products.c.ticker,
                canonical_products.c.product_type,
                canonical_products.c.region,
                canonical_products.c.asset_type,
                canonical_products.c.dataset_snapshot,
                canonical_products.c.observed_at,
                etf_attributes.c.strategy,
            )
            .select_from(
                canonical_products.join(
                    etf_attributes,
                    (
                        canonical_products.c.canonical_product_id
                        == etf_attributes.c.canonical_product_id
                    )
                    & (
                        canonical_products.c.dataset_snapshot
                        == etf_attributes.c.dataset_snapshot
                    ),
                )
            )
            .where(*conditions)
            .order_by(canonical_products.c.canonical_product_id)
        )
        count_statement = select(func.count()).select_from(canonical_products).where(
            *conditions
        )
        with self._engine.connect() as connection:
            source_rows = int(connection.scalar(count_statement) or 0)
            rows = connection.execute(statement).mappings().all()

        documents: list[SemanticDocument] = []
        missing = source_rows - len(rows)
        sentinel = 0
        normalized_counts: dict[str, int] = {}
        for row in rows:
            raw = row["strategy"]
            if raw is None or not str(raw).strip():
                missing += 1
                continue
            raw_text = str(raw).strip()
            normalized = normalize_text(raw_text)
            if normalized in _SENTINELS:
                sentinel += 1
                continue
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
            entity_id = row["canonical_product_id"]
            documents.append(
                SemanticDocument(
                    document_id=f"{entity_id}:strategy",
                    entity_id=entity_id,
                    source_dataset=STRATEGY_SOURCE_DATASET,
                    source_record_key=row["source_record_key"],
                    source_field=STRATEGY_SOURCE_FIELD,
                    raw_text=raw_text,
                    normalized_text=normalized,
                    product_type=row["product_type"],
                    region=row["region"],
                    asset_type=row["asset_type"],
                    dataset_snapshot=row["dataset_snapshot"],
                    observed_at=row["observed_at"],
                    metadata={
                        "source_file": row["source_file"],
                        "source_row_number": row["source_row_number"],
                        "product_name": row["product_name"],
                        "ticker": row["ticker"],
                        "physical_source_field": "cu_strtegy",
                        "canonical_table_field": "etf_attributes.strategy",
                    },
                )
            )
        duplicates = sum(count - 1 for count in normalized_counts.values() if count > 1)
        return documents, DocumentBuildStats(
            source_rows=source_rows,
            skipped_missing=missing,
            skipped_sentinel=sentinel,
            duplicate_texts=duplicates,
        )

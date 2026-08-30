from typing import Protocol

from sqlalchemy import Engine, distinct, func, select

from app.domain.models import (
    CoverageStatus,
    FieldQualityMetadata,
    SnapshotPolicy,
)
from app.data.schema import field_quality_profiles
from app.data.schema import canonical_products
from app.data.v2_schema import metric_definitions


class FieldQualityProvider(Protocol):
    """Supply field quality facts independently from validation policy."""

    def get_quality(
        self,
        canonical_field: str,
        product_type: str | None = None,
    ) -> FieldQualityMetadata: ...


class StaticFieldQualityProvider:
    """Small M6 fixture, not statistics derived from a real dataset."""

    def __init__(
        self,
        overrides: dict[str, FieldQualityMetadata] | None = None,
    ) -> None:
        self._metadata = {
            "product.aum": FieldQualityMetadata(
                canonical_field="product.aum",
                coverage_status=CoverageStatus.COMPLETE,
                coverage_fraction=1.0,
                ranking_safe=True,
                comparison_safe=True,
            ),
            "product.expense_ratio": FieldQualityMetadata(
                canonical_field="product.expense_ratio",
                coverage_status=CoverageStatus.PARTIAL,
                ranking_safe=False,
                comparison_safe=False,
            ),
            "product.base_index": FieldQualityMetadata(
                canonical_field="product.base_index",
                sentinel_values=[
                    "Index is not provided by Management Company",
                    "Index is not available on Lipper Database",
                ],
                snapshot_policy=SnapshotPolicy.WARN,
            ),
            "product.maturity_date": FieldQualityMetadata(
                canonical_field="product.maturity_date",
                sentinel_values=[0, 99991231],
                snapshot_policy=SnapshotPolicy.WARN,
            ),
        }
        if overrides:
            self._metadata.update(overrides)

    def get_quality(
        self,
        canonical_field: str,
        product_type: str | None = None,
    ) -> FieldQualityMetadata:
        del product_type
        metadata = self._metadata.get(canonical_field)
        if metadata is not None:
            return metadata.model_copy(deep=True)
        return FieldQualityMetadata(canonical_field=canonical_field)


class DatabaseFieldQualityProvider:
    """Read ingestion-derived quality profiles for the active snapshot."""

    def __init__(self, engine: Engine, *, snapshot_date: str) -> None:
        self._engine = engine
        self._snapshot_date = snapshot_date
        self._sentinel_defaults = StaticFieldQualityProvider()

    def get_quality(
        self,
        canonical_field: str,
        product_type: str | None = None,
    ) -> FieldQualityMetadata:
        scope_product_type = product_type or "__all__"
        statement = select(field_quality_profiles).where(
            field_quality_profiles.c.source_dataset == "__all__",
            field_quality_profiles.c.product_type == scope_product_type,
            field_quality_profiles.c.canonical_field == canonical_field,
            field_quality_profiles.c.dataset_snapshot == self._snapshot_date,
        )
        with self._engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
        baseline = self._sentinel_defaults.get_quality(canonical_field)
        if row is None:
            return baseline
        if row["total_count"] <= 0 or row["valid_count"] <= 0:
            coverage_status = CoverageStatus.UNKNOWN
        elif row["valid_count"] == row["total_count"]:
            coverage_status = CoverageStatus.COMPLETE
        else:
            coverage_status = CoverageStatus.PARTIAL
        comparison_safe = (
            coverage_status is CoverageStatus.COMPLETE
            and not row["is_constant"]
        )
        if canonical_field in {"product.aum", "product.nav", "product.price"}:
            conditions = [
                canonical_products.c.dataset_snapshot == self._snapshot_date,
                canonical_products.c.currency.is_not(None),
            ]
            if product_type is not None:
                conditions.append(
                    canonical_products.c.product_type == product_type
                )
            with self._engine.connect() as connection:
                currency_count = connection.scalar(
                    select(func.count(distinct(canonical_products.c.currency))).where(
                        *conditions
                    )
                )
            if currency_count is None or currency_count != 1:
                comparison_safe = False
        return baseline.model_copy(
            update={
                "coverage_status": coverage_status,
                "coverage_fraction": row["coverage_fraction"],
                "ranking_safe": comparison_safe,
                "comparison_safe": comparison_safe,
            }
        )


class CanonicalV2FieldQualityProvider:
    """Read only approved canonical_v2 metric capability contracts."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("canonical_v2 quality provider requires PostgreSQL")
        self._engine = engine

    def get_quality(
        self,
        canonical_field: str,
        product_type: str | None = None,
    ) -> FieldQualityMetadata:
        del product_type
        with self._engine.connect() as connection:
            row = connection.execute(
                select(metric_definitions).where(
                    metric_definitions.c.canonical_field == canonical_field
                )
            ).mappings().first()
        if row is None:
            return FieldQualityMetadata(canonical_field=canonical_field)
        return FieldQualityMetadata(
            canonical_field=canonical_field,
            coverage_status=CoverageStatus.UNKNOWN,
            ranking_safe=bool(row["sort_enabled"]),
            comparison_safe=bool(row["cross_source_comparable"]),
        )

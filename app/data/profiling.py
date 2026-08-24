from collections.abc import Iterable
from typing import Any

from sqlalchemy import Connection, delete, distinct, func, insert, select

from app.data.schema import canonical_products, field_quality_profiles


QUALITY_FIELD_COLUMNS = {
    "product.aum": canonical_products.c.aum,
    "product.expense_ratio": canonical_products.c.expense_ratio,
    "product.region": canonical_products.c.region,
    "product.asset_type": canonical_products.c.asset_type,
    "product.product_type": canonical_products.c.product_type,
    "product.nav": canonical_products.c.nav,
    "product.price": canonical_products.c.price,
    "product.base_index": canonical_products.c.base_index,
}


def rebuild_dataset_profiles(
    connection: Connection,
    *,
    source_dataset: str,
    snapshot: str,
) -> None:
    connection.execute(
        delete(field_quality_profiles).where(
            field_quality_profiles.c.source_dataset == source_dataset,
            field_quality_profiles.c.dataset_snapshot == snapshot,
        )
    )
    product_types = connection.execute(
        select(distinct(canonical_products.c.product_type)).where(
            canonical_products.c.source_dataset == source_dataset,
            canonical_products.c.dataset_snapshot == snapshot,
        )
    ).scalars()
    scopes = ["__all__", *sorted(product_types)]
    rows = [
        _profile(
            connection,
            source_dataset=source_dataset,
            product_type=product_type,
            canonical_field=field,
            snapshot=snapshot,
        )
        for product_type in scopes
        for field in QUALITY_FIELD_COLUMNS
    ]
    connection.execute(insert(field_quality_profiles), rows)


def rebuild_aggregate_profiles(
    connection: Connection,
    *,
    snapshot: str,
) -> None:
    connection.execute(
        delete(field_quality_profiles).where(
            field_quality_profiles.c.source_dataset == "__all__",
            field_quality_profiles.c.dataset_snapshot == snapshot,
        )
    )
    product_types = connection.execute(
        select(distinct(canonical_products.c.product_type)).where(
            canonical_products.c.dataset_snapshot == snapshot
        )
    ).scalars()
    scopes = ["__all__", *sorted(product_types)]
    rows = [
        _profile(
            connection,
            source_dataset="__all__",
            product_type=product_type,
            canonical_field=field,
            snapshot=snapshot,
        )
        for product_type in scopes
        for field in QUALITY_FIELD_COLUMNS
    ]
    connection.execute(insert(field_quality_profiles), rows)


def _profile(
    connection: Connection,
    *,
    source_dataset: str,
    product_type: str,
    canonical_field: str,
    snapshot: str,
) -> dict[str, Any]:
    column = QUALITY_FIELD_COLUMNS[canonical_field]
    conditions = [canonical_products.c.dataset_snapshot == snapshot]
    if source_dataset != "__all__":
        conditions.append(canonical_products.c.source_dataset == source_dataset)
    if product_type != "__all__":
        conditions.append(canonical_products.c.product_type == product_type)
    total_count, valid_count, unique_count = connection.execute(
        select(
            func.count(),
            func.count(column),
            func.count(distinct(column)),
        ).where(*conditions)
    ).one()
    distinct_values: Iterable[Any] = connection.execute(
        select(distinct(column))
        .where(*conditions, column.is_not(None))
        .limit(2)
    ).scalars()
    values = list(distinct_values)
    is_constant = valid_count > 0 and unique_count == 1
    return {
        "source_dataset": source_dataset,
        "product_type": product_type,
        "canonical_field": canonical_field,
        "dataset_snapshot": snapshot,
        "total_count": total_count,
        "valid_count": valid_count,
        "missing_count": total_count - valid_count,
        "coverage_fraction": (
            valid_count / total_count if total_count else None
        ),
        "unique_count": unique_count,
        "constant_value": str(values[0]) if is_constant else None,
        "is_constant": is_constant,
    }

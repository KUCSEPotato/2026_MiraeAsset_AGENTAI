"""Small canonical-product row factory used by integration tests."""

from __future__ import annotations

from typing import Any


def product(
    product_id: str,
    *,
    region: str | None = "Region.US",
    asset_type: str | None = "AssetType.Bond",
    aum: float | None = 100.0,
    expense_ratio: float | None = 0.1,
) -> dict[str, Any]:
    return {
        "canonical_product_id": product_id,
        "dataset_snapshot": "2026-07-11",
        "source_dataset": "foreign_etf",
        "source_record_key": product_id,
        "source_file": "test-fixture",
        "source_row_number": 1,
        "product_type": "FinancialProduct.ETF",
        "product_name": f"Product {product_id}",
        "short_name": product_id,
        "normalized_product_name": product_id.casefold(),
        "normalized_short_name": product_id.casefold(),
        "ticker": None,
        "isin": None,
        "asset_manager": None,
        "issuer": None,
        "asset_type": asset_type,
        "region": region,
        "risk_grade": None,
        "currency": "USD",
        "aum": aum,
        "nav": None,
        "price": None,
        "expense_ratio": expense_ratio,
        "base_index": None,
        "observed_at": "2026-07-11",
    }

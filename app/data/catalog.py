from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Table

from app.data.schema import (
    source_domestic_bonds,
    source_domestic_etfs,
    source_foreign_etfs,
    source_public_funds,
)


@dataclass(frozen=True)
class DatasetSpec:
    prefix: str
    source_dataset: str
    namespace: str
    source_table: Table
    source_key_fields: tuple[str, ...]
    product_key_fields: tuple[str, ...] | None = None
    literal_null_fields: frozenset[str] = frozenset()

    @property
    def product_key_fields_or_source(self) -> tuple[str, ...]:
        return self.product_key_fields or self.source_key_fields

    def source_record_key(self, row: dict[str, Any]) -> str | None:
        values = [row.get(field) for field in self.source_key_fields]
        if any(value is None or str(value).strip() == "" for value in values):
            return None
        return ":".join(str(value).strip() for value in values)

    def canonical_product_id(self, row: dict[str, Any]) -> str | None:
        fields = self.product_key_fields or self.source_key_fields
        values = [row.get(field) for field in fields]
        if any(value is None or str(value).strip() == "" for value in values):
            return None
        key = ":".join(str(value).strip() for value in values)
        return f"{self.namespace}:{key}" if key is not None else None


DATASET_SPECS = (
    DatasetSpec(
        prefix="PRBD01N001",
        source_dataset="domestic_bond",
        namespace="bond_kr",
        source_table=source_domestic_bonds,
        source_key_fields=("pd_no", "pd_exg_mkt", "info_base_dt", "info_seq"),
        product_key_fields=("pd_no",),
    ),
    DatasetSpec(
        prefix="PREF01N001",
        source_dataset="domestic_etf",
        namespace="etf_kr",
        source_table=source_domestic_etfs,
        source_key_fields=("pd_itm_no",),
    ),
    DatasetSpec(
        prefix="PREF02N001",
        source_dataset="foreign_etf",
        namespace="etf_gl",
        source_table=source_foreign_etfs,
        source_key_fields=("pd_itm_no",),
    ),
    DatasetSpec(
        prefix="PRFD01N001",
        source_dataset="public_fund",
        namespace="fund_pub",
        source_table=source_public_funds,
        source_key_fields=("itm_no",),
        product_key_fields=("itm_no",),
        literal_null_fields=frozenset({"zrin_fd_ivst_risk_gcd"}),
    ),
)


@dataclass(frozen=True)
class DatasetFiles:
    spec: DatasetSpec
    data_file: Path
    schema_file: Path
    snapshot_date: str


def discover_dataset_files(root: Path) -> list[DatasetFiles]:
    discovered: list[DatasetFiles] = []
    for spec in DATASET_SPECS:
        data_matches = sorted(root.rglob(f"{spec.prefix}_*_datarows.xlsx"))
        schema_matches = sorted(root.rglob(f"{spec.prefix}_*_schema.xlsx"))
        if len(data_matches) != 1 or len(schema_matches) != 1:
            raise FileNotFoundError(
                f"expected one data and schema workbook for {spec.prefix}"
            )
        snapshot_token = data_matches[0].stem.rsplit("_", 2)[-2]
        if len(snapshot_token) != 8 or not snapshot_token.isdigit():
            raise ValueError(
                f"cannot derive snapshot from {data_matches[0].name}"
            )
        snapshot = (
            f"{snapshot_token[:4]}-{snapshot_token[4:6]}-"
            f"{snapshot_token[6:]}"
        )
        discovered.append(
            DatasetFiles(
                spec=spec,
                data_file=data_matches[0],
                schema_file=schema_matches[0],
                snapshot_date=snapshot,
            )
        )
    return discovered

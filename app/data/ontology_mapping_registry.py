from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.data.catalog import DatasetFiles
from app.data.loader import SourceSchema


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    dataset_code: str
    source_table: str
    source_column: str
    description: str
    source_type: str
    nullable: str
    category: str
    target_class: str
    target_property: str
    property_kind: str
    rdf_type: str
    unit_or_code_scheme: str
    transformation_rule: str
    missing_policy: str
    uncertainty: str

    @property
    def is_observation(self) -> bool:
        return self.category == "기준일이 있는 관측값"

    @property
    def is_identifier(self) -> bool:
        return self.category == "식별자"

    @property
    def is_relation(self) -> bool:
        return self.property_kind == "ObjectProperty" and self.category == "관계"


class MappingRegistryError(ValueError):
    pass


class OntologyColumnMappingRegistry:
    """Exact dataset+table+column registry backed by the reviewed 280-row CSV."""

    def __init__(self, mappings: tuple[ColumnMapping, ...]) -> None:
        self._mappings = mappings
        self._by_key = {
            (item.dataset_code, item.source_table, item.source_column): item
            for item in mappings
        }
        if len(self._by_key) != len(mappings):
            raise MappingRegistryError("duplicate dataset/table/column mapping")

    @classmethod
    def load(cls, path: Path) -> "OntologyColumnMappingRegistry":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        mappings = tuple(
            ColumnMapping(
                dataset_code=row["원본 데이터셋"],
                source_table=row["원본 테이블"],
                source_column=row["원본 칼럼명"],
                description=row["스키마상의 설명"],
                source_type=row["스키마 자료형"],
                nullable=row["스키마 Nullable"],
                category=row["분류"],
                target_class=row["대상 클래스"],
                target_property=row["대상 속성"],
                property_kind=row["속성 구분"],
                rdf_type=row["RDF 자료형"],
                unit_or_code_scheme=row["단위 또는 코드 체계"],
                transformation_rule=row["변환 규칙"],
                missing_policy=row["결측치 처리"],
                uncertainty=row["비고 및 불확실성"],
            )
            for row in rows
        )
        if len(mappings) != 280:
            raise MappingRegistryError(f"expected 280 mappings, got {len(mappings)}")
        return cls(mappings)

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @property
    def mappings(self) -> tuple[ColumnMapping, ...]:
        return self._mappings

    def for_dataset(self, files: DatasetFiles, schema: SourceSchema) -> tuple[ColumnMapping, ...]:
        table = _mapping_table(files.spec.prefix)
        result = tuple(
            self._by_key[(files.spec.prefix, table, column)]
            for column in schema.columns
            if (files.spec.prefix, table, column) in self._by_key
        )
        mapped_columns = tuple(item.source_column for item in result)
        if mapped_columns != schema.columns:
            missing = sorted(set(schema.columns) - set(mapped_columns))
            extra = sorted(set(mapped_columns) - set(schema.columns))
            raise MappingRegistryError(
                f"mapping/schema mismatch for {files.spec.prefix}: missing={missing}, extra={extra}"
            )
        return result


def _mapping_table(prefix: str) -> str:
    return {
        "PRBD01N001": "domestic_bond",
        "PREF01N001": "domestic_etp",
        "PREF02N001": "foreign_etp",
        "PRFD01N001": "fund_share_class",
    }[prefix]

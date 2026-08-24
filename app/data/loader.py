from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


@dataclass(frozen=True)
class SourceSchema:
    columns: tuple[str, ...]
    column_types: dict[str, str]
    declared_key_columns: tuple[str, ...]


def load_source_schema(path: Path) -> SourceSchema:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["Sheet1_Schema"]
        rows = sheet.iter_rows(values_only=True)
        next(rows, None)
        header = next(rows, None)
        if header is None or tuple(header[:3]) != (
            "컬럼명",
            "PK/FK",
            "컬럼타입",
        ):
            raise ValueError(f"invalid schema header: {path}")
        columns: list[str] = []
        types: dict[str, str] = {}
        keys: list[str] = []
        for row in rows:
            column = row[0]
            if not column:
                continue
            name = str(column).strip()
            columns.append(name)
            types[name] = str(row[2] or "text").strip().casefold()
            if row[1] and "PK" in str(row[1]).upper():
                keys.append(name)
        return SourceSchema(tuple(columns), types, tuple(keys))
    finally:
        workbook.close()


def iter_source_rows(
    path: Path,
    schema: SourceSchema,
) -> Iterator[tuple[int, dict[str, Any]]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if "datarows" not in workbook.sheetnames:
            raise ValueError(f"missing datarows sheet: {path}")
        rows = workbook["datarows"].iter_rows(values_only=True)
        header_row = next(rows, None)
        if header_row is None:
            raise ValueError(f"empty data workbook: {path}")
        headers = tuple(str(item).strip() for item in header_row if item is not None)
        if headers != schema.columns:
            missing = sorted(set(schema.columns) - set(headers))
            unexpected = sorted(set(headers) - set(schema.columns))
            raise ValueError(
                f"schema mismatch for {path.name}: "
                f"missing={missing}, unexpected={unexpected}"
            )
        for row_number, values in enumerate(rows, start=2):
            yield row_number, dict(zip(headers, values, strict=False))
    finally:
        workbook.close()

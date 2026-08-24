import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.models import ConceptCategory
from app.ontology.registry import StaticSemanticRegistry


DATE_SENTINELS = {"0", "99991231"}
INDEX_SENTINELS = {
    "Index is not provided by Management Company",
    "Index is not available on Lipper Database",
}


def clean_source_row(
    raw: dict[str, Any],
    *,
    literal_null_fields: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    cleaned: dict[str, Any] = {}
    changed_raw_values: dict[str, Any] = {}
    for field, value in raw.items():
        normalized = value
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                normalized = None
            elif field in literal_null_fields and normalized == "NULL":
                normalized = None
        if normalized != value:
            changed_raw_values[field] = json_value(value)
        cleaned[field] = json_value(normalized)
    return cleaned, changed_raw_values


def json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def normalized_date(
    value: Any,
    *,
    sentinels: set[str] = DATE_SENTINELS,
) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    raw = str(value).strip()
    if raw in sentinels:
        return None, "UNKNOWN_OR_SPECIAL_DATE"
    if isinstance(value, datetime):
        return value.date().isoformat(), None
    if isinstance(value, date):
        return value.isoformat(), None
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}", None
    return raw, None


def normalized_base_index(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw:
        return None, None
    if raw in INDEX_SENTINELS:
        return None, "UNKNOWN_BASE_INDEX"
    return raw, None


def canonical_asset_type(value: Any) -> str | None:
    if value is None:
        return None
    concept = StaticSemanticRegistry().resolve_concept(
        str(value),
        ConceptCategory.ASSET_TYPE,
    )
    return concept.value if concept is not None else None


def canonical_region(value: Any) -> str | None:
    if value is None:
        return None
    concept = StaticSemanticRegistry().resolve_concept(
        str(value),
        ConceptCategory.REGION,
    )
    return concept.value if concept is not None else None


def normalize_lookup_value(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())

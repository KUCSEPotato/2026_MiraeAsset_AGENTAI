import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
from typing import Any

DATE_SENTINELS = {"0", "99991231"}
INDEX_SENTINELS = {
    "Index is not provided by Management Company",
    "Index is not available on Lipper Database",
}
PRBD_SALE_LOT_EVIDENCE_FIELDS = frozenset(
    {
        "after_tax_yield",
        "avg_annual_tax_yield",
        "bdbns_abl_chnl_nm",
        "bdbns_abl_chnl_tcd",
        "buy_yield",
        "corp_after_tax_yield",
        "corp_pretax_yield",
        "depo_equiv_yield_154",
        "depo_equiv_yield_495",
        "pref_tax_yield",
        "sale_yield_base_dt",
        "trade_price",
    }
)


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


def has_prbd_sale_lot_evidence(row: dict[str, Any]) -> bool:
    """True when a PRBD row has concrete sale-condition evidence."""
    return any(
        _has_source_value(row.get(field_name))
        for field_name in PRBD_SALE_LOT_EVIDENCE_FIELDS
    )


def _has_source_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, Decimal) and value.is_nan():
        return False
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped) and stripped.casefold() != "nan"
    return True


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


def canonical_subscription_status(value: Any) -> str | None:
    """Map only the organizer-confirmed FundShareClass subscription states."""
    normalized = str(value).strip() if value is not None else ""
    return {
        "판매중": "OPEN_FOR_SUBSCRIPTION",
        "판매완료": "CLOSED_FOR_SUBSCRIPTION",
    }.get(normalized)


def canonical_mirae_sale_flag(value: Any) -> bool | None:
    """Preserve the distinction between an explicit N and missing evidence."""
    normalized = str(value).strip().upper() if value is not None else ""
    return {"Y": True, "N": False}.get(normalized)


def canonical_asset_type(value: Any) -> str | None:
    if value is None:
        return None
    from app.ontology.runtime_mapping import TeamOntologyRuntimeMapping

    mapping = TeamOntologyRuntimeMapping().concept(str(value), "asset_class")
    semantic = mapping.semantic_value() if mapping is not None else None
    return semantic.runtime_key if semantic is not None else None


def canonical_region(value: Any) -> str | None:
    if value is None:
        return None
    # Authoritative ingestion follows the activated Team vocabulary. This is
    # intentionally separate from the legacy StaticSemanticRegistry so that
    # e.g. "Global Ex US" cannot silently collapse to unrestricted Global.
    from app.ontology.runtime_mapping import TeamOntologyRuntimeMapping

    mapping = TeamOntologyRuntimeMapping().concept(str(value), "exposure_region")
    semantic = mapping.semantic_value() if mapping is not None else None
    return semantic.runtime_key if semantic is not None else None


def canonical_risk_grade(value: Any) -> str | None:
    """Normalize the six reviewed grades; sentinels remain unresolved."""
    if value is None:
        return None
    normalized = normalize_lookup_value(str(value))
    for grade, label in _RISK_GRADE_LABELS.items():
        if normalized in {
            str(grade),
            f"{grade}등급",
            normalize_lookup_value(label),
        }:
            return f"RiskGrade.{grade}"
    return None


_RISK_GRADE_LABELS = {
    1: "매우높은위험(1등급)",
    2: "높은위험(2등급)",
    3: "다소높은위험(3등급)",
    4: "보통위험(4등급)",
    5: "낮은위험(5등급)",
    6: "매우낮은위험(6등급)",
}


def normalize_lookup_value(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())

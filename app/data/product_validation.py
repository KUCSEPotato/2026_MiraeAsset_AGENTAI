from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.data.catalog import DatasetSpec


PLACEHOLDER_NAMES = frozenset({".", "-", "--", "_", "N/A", "NA", "NULL", "NONE"})
NAME_FIELDS = {
    "domestic_bond": "pd_nm",
    "domestic_etf": "pd_nm",
    "foreign_etf": "pd_nm",
    "public_fund": "itm_nm",
}


@dataclass(frozen=True, slots=True)
class ProductValidationFailure:
    code: str
    reason: str
    source_key: str | None
    raw_product_name: str | None
    invalid_fields: tuple[str, ...]


def validate_product_row(spec: DatasetSpec, row: dict[str, Any]) -> ProductValidationFailure | None:
    key = spec.canonical_product_id(row)
    raw_key = _raw_product_key(spec, row)
    name_value = row.get(NAME_FIELDS[spec.source_dataset])
    name = str(name_value).strip() if name_value is not None else ""

    key_error: tuple[str, str] | None = None
    if not raw_key or _all_zero(raw_key):
        key_error = ("INVALID_PRODUCT_KEY", "product key is missing or an all-zero sentinel")
    if spec.source_dataset in {"domestic_bond", "domestic_etf", "public_fund"}:
        if not re.fullmatch(r"[A-Za-z0-9]{12}", raw_key):
            key_error = (
                "INVALID_PRODUCT_KEY_FORMAT",
                f"{spec.source_dataset} product key must be 12 ASCII alphanumeric characters",
            )
    elif spec.source_dataset == "foreign_etf":
        # RICs in the current official source are 2-6 characters and may contain
        # exchange punctuation. They are not ISINs and must not use the KR rule.
        if not re.fullmatch(r"[A-Za-z0-9.\-]{2,32}", raw_key):
            key_error = ("INVALID_RIC_FORMAT", "foreign ETP RIC has an unsupported format")
    name_invalid = (
        not name or name.upper() in PLACEHOLDER_NAMES
        or not any(character.isalnum() for character in name)
    )
    if key_error and name_invalid:
        return _failure(
            "INVALID_PRODUCT_IDENTITY_AND_NAME",
            f"{key_error[1]}; product name is missing or punctuation/placeholder only",
            raw_key, name, (*spec.product_key_fields_or_source, NAME_FIELDS[spec.source_dataset]),
        )
    if key_error:
        return _failure(key_error[0], key_error[1], raw_key, name, spec.product_key_fields_or_source)
    if name_invalid:
        return _failure("INVALID_PRODUCT_NAME", "product name is missing or punctuation/placeholder only", raw_key, name, (NAME_FIELDS[spec.source_dataset],))

    if spec.source_dataset in {"domestic_etf", "foreign_etf"}:
        group = str(row.get("pd_grp_no") or "").strip().upper()
        if group not in {"ETF", "ETN"}:
            return _failure("UNDETERMINED_PRODUCT_TYPE", "pd_grp_no does not determine ETF or ETN", raw_key, name, ("pd_grp_no",))
        if spec.source_dataset == "foreign_etf":
            etn_flag = str(row.get("cu_etn_yn") or "").strip().upper()
            if etn_flag == "Y" and group != "ETN":
                return _failure("CONFLICTING_PRODUCT_TYPE", "cu_etn_yn=Y conflicts with pd_grp_no", raw_key, name, ("pd_grp_no", "cu_etn_yn"))
    if key is None:
        return _failure("INVALID_PRODUCT_ID", "canonical product ID cannot be constructed", raw_key, name, spec.product_key_fields_or_source)
    return None


def _raw_product_key(spec: DatasetSpec, row: dict[str, Any]) -> str:
    return ":".join(str(row.get(field) or "").strip() for field in spec.product_key_fields_or_source)


def _all_zero(value: str) -> bool:
    compact = value.replace(":", "").replace(".", "")
    return len(compact) >= 3 and bool(compact) and set(compact) <= {"0"}


def _failure(
    code: str, reason: str, key: str, name: str,
    invalid_fields: tuple[str, ...],
) -> ProductValidationFailure:
    return ProductValidationFailure(code, reason, key or None, name or None, invalid_fields)

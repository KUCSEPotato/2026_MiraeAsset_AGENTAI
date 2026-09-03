from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.catalog import DatasetFiles
from app.data.cleaning import canonical_subscription_status, json_value
from app.data.mapping import MappedProduct
from app.data.ontology_mapping_registry import ColumnMapping


@dataclass(slots=True)
class EvidenceRows:
    source_record: dict[str, Any]
    assertions: list[dict[str, Any]] = field(default_factory=list)
    identifiers: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    raw_codes: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)


def transform_evidence(
    files: DatasetFiles,
    row_number: int,
    raw: dict[str, Any],
    cleaned: dict[str, Any],
    mapped: MappedProduct,
    mappings: tuple[ColumnMapping, ...],
    *,
    prepare_documents: bool = False,
) -> EvidenceRows:
    product_id = mapped.canonical["canonical_product_id"]
    source_key = mapped.canonical["source_record_key"]
    record_id = _id(files.spec.source_dataset, files.snapshot_date, source_key)
    raw_json = {key: json_value(value) for key, value in raw.items()}
    normalized_json = {key: json_value(value) for key, value in cleaned.items()}
    result = EvidenceRows(
        source_record={
            "source_record_id": record_id,
            "dataset_id": files.spec.source_dataset,
            "dataset_snapshot": files.snapshot_date,
            "source_record_key": source_key,
            "source_row_number": row_number,
            "canonical_product_id": product_id,
            "raw_payload": raw_json,
            "normalized_payload": normalized_json,
            "raw_payload_hash": hashlib.sha256(
                json.dumps(raw_json, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "quality_annotations": mapped.quality_annotations,
        }
    )
    for mapping in mappings:
        value = cleaned.get(mapping.source_column)
        raw_value = raw.get(mapping.source_column)
        quality = _quality(value)
        base = f"{record_id}|{mapping.source_column}"
        assertion = {
                "assertion_id": _id("assertion", base),
                "source_record_id": record_id,
                "canonical_product_id": product_id,
                "source_dataset": files.spec.source_dataset,
                "source_column": mapping.source_column,
                "mapping_category": mapping.category,
                "target_class": mapping.target_class,
                "target_property": mapping.target_property,
                "raw_value": _text(raw_value),
                "normalized_value": _text(value),
                "quality_status": quality,
                "transformation_rule": mapping.transformation_rule,
            }
        # Sparse provenance: absent values remain recoverable from SourceRecord's
        # normalized payload and the complete mapping registry. Storing a row for
        # every empty cell duplicates that evidence at very high cost.
        if quality != "MISSING":
            result.assertions.append(assertion)
        if quality != "VALID":
            continue
        if mapping.is_identifier:
            identifier = _identifier_row(files, mapping, value, product_id, record_id)
            if identifier is not None:
                result.identifiers.append(identifier)
        elif mapping.is_observation:
            result.observations.append(
                _observation_row(files, mapping, value, cleaned, product_id, record_id)
            )
        elif mapping.is_relation:
            relation = _relation_row(
                files, mapping, value, mapped, product_id, record_id
            )
            if relation is not None:
                result.relations.append(relation)
        elif mapping.category == "코드 또는 분류 개념":
            result.raw_codes.append(
                {
                    "raw_code_id": _id("code", base),
                    "source_record_id": record_id,
                    "canonical_product_id": product_id,
                    "source_column": mapping.source_column,
                    "code_value": str(value),
                    "code_scheme": mapping.unit_or_code_scheme or "UNSPECIFIED_RAW_CODE",
                    "quality_status": "SENTINEL" if _is_sentinel(str(value)) else "UNVERIFIED_RAW_CODE",
                }
            )
        if prepare_documents and _is_semantic_text(mapping, value):
            result.documents.append(
                {
                    "document_id": _id("document", base),
                    "canonical_product_id": product_id,
                    "source_record_id": record_id,
                    "source_dataset": files.spec.source_dataset,
                    "source_column": mapping.source_column,
                    "document_type": mapping.target_property,
                    "raw_text": str(value),
                    "dataset_snapshot": files.snapshot_date,
                    "observed_at": _observed_at(files.spec.prefix, mapping.source_column, cleaned),
                }
            )
    if (
        files.spec.prefix == "PREF02N001"
        and str(cleaned.get("cu_index_tracking_yn") or "").strip().upper()
        == "Y"
    ):
        base_index = cleaned.get("cu_base_index")
        if _valid_index(base_index):
            result.relations.append(
                _explicit_relation_row(
                    files,
                    product_id,
                    record_id,
                    "cu_index_tracking_yn+cu_base_index",
                    "tracksIndex",
                    "Index",
                    str(base_index).strip(),
                )
            )
    return result


def _identifier_row(files, mapping, value, product_id, record_id):
    match = re.search(r"identifierType=([^;]+)", mapping.transformation_rule)
    identifier_type = match.group(1).strip() if match else "SOURCE_ID"
    # This identifier describes the portfolio, not the share-class product represented
    # by this row. Preserve it as an assertion until a portfolio identity is available.
    if identifier_type.startswith("REPRESENTATIVE_KSD_ID"):
        return None
    normalized = str(value).strip().upper()
    if _is_sentinel(normalized):
        return None
    namespace = "ISO-6166" if identifier_type == "ISIN" else files.spec.prefix
    validation = "FORMAT_VALID" if identifier_type != "ISIN" else (
        "FORMAT_VALID" if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", normalized) else "INVALID_FORMAT"
    )
    return {
        "identifier_id": _id("identifier", record_id, mapping.source_column, normalized),
        "canonical_product_id": product_id,
        "dataset_snapshot": files.snapshot_date,
        "source_record_id": record_id,
        "source_dataset": files.spec.source_dataset,
        "source_column": mapping.source_column,
        "identifier_type": identifier_type,
        "identifier_value": str(value),
        "normalized_value": normalized,
        "namespace": namespace,
        "is_primary_in_source": mapping.source_column in files.spec.product_key_fields_or_source,
        "validation_status": validation,
    }


def _observation_row(files, mapping, value, row, product_id, record_id):
    try:
        number = Decimal(str(value).replace(",", ""))
        quality = "VALID"
    except (InvalidOperation, ValueError):
        number = None
        quality = "NON_NUMERIC_SOURCE_VALUE"
    unit = mapping.unit_or_code_scheme.strip() or None
    currency = next((row.get(c) for c in ("pd_curr_cd", "curr_cd", "std_curr_cd")
                     if _valid_currency(row.get(c))), None)
    return {
        "observation_id": _id("observation", record_id, mapping.source_column),
        "canonical_product_id": product_id,
        "dataset_snapshot": files.snapshot_date,
        "source_record_id": record_id,
        "source_dataset": files.spec.source_dataset,
        "source_column": mapping.source_column,
        "observation_type": mapping.target_class,
        "metric_type": mapping.target_property,
        "numeric_value": number,
        "raw_value": str(value),
        "unit": unit,
        "currency": str(currency) if currency else None,
        "observed_at": _observed_at(files.spec.prefix, mapping.source_column, row),
        "quality_status": quality,
        "transformation_rule": mapping.transformation_rule,
    }


def _relation_row(files, mapping, value, mapped, product_id, record_id):
    relation = mapping.target_property
    if mapping.source_column == "sale_yn":
        status = canonical_subscription_status(value)
        if status is None:
            return None
        value = status
    if relation == "managedBy/issuedBy":
        # The new source labels this column as management company.  It is valid
        # managedBy evidence for ETF, but not proof of an ETN issuer role.
        if mapped.canonical["product_type"].endswith("ETN"):
            return None
        relation = "managedBy"
    if mapping.source_column in {"cu_base_index", "ref_base_index"}:
        if not _valid_index(value):
            return None
        relation = "hasUnderlyingIndex"
    if relation == "referencesBenchmark":
        relation = "hasBenchmark"
    target_type = mapping.target_class
    target = str(value).strip()
    return _explicit_relation_row(
        files,
        product_id,
        record_id,
        mapping.source_column,
        relation,
        target_type,
        target,
    )


def _explicit_relation_row(
    files: DatasetFiles,
    product_id: str,
    record_id: str,
    source_column: str,
    relation: str,
    target_type: str,
    target: str,
) -> dict[str, Any]:
    return {
        "relation_id": _id(
            "relation", record_id, source_column, relation, target
        ),
        "canonical_product_id": product_id,
        "dataset_snapshot": files.snapshot_date,
        "source_record_id": record_id,
        "source_column": source_column,
        "relation_type": relation,
        "target_type": target_type,
        "target_id": _id(target_type, files.spec.prefix, target),
        "target_label": target,
        "identity_basis": "SOURCE_VALUE_EXACT",
    }


def _observed_at(prefix: str, column: str, row: dict[str, Any]) -> str | None:
    candidates: tuple[str, ...]
    if prefix == "PRBD01N001":
        candidates = (("sale_yield_base_dt",) if "sale_yield" in column else
                      ("crd_grd_dt",) if "crd" in column else
                      ("exg_close_price_base_dt",) if "exg" in column else
                      ("pd_std_info_update", "info_base_dt"))
    elif prefix == "PREF01N001":
        candidates = (("du_vlty_base_dt",) if "vlty" in column else
                      ("pd_dvid_base_dt",) if "dvid" in column else
                      ("du_nav_base_dt", "du_upt_dt"))
    elif prefix == "PREF02N001":
        candidates = (("du_clpr_base_dt",) if "clpr" in column else
                      ("du_nav_base_dt",) if "nav" in column else
                      ("du_upt_dt", "cu_upt_dt"))
    else:
        candidates = ("fd_price_bas_dt", "fd_daily_bas_dt")
    for candidate in candidates:
        value = row.get(candidate)
        if value:
            text = str(value).strip().replace(".", "-").replace("/", "-")
            if text in {"99991231", "9999-12-31", "00000000", "0000-00-00"}:
                continue
            if re.fullmatch(r"\d{8}", text):
                return f"{text[:4]}-{text[4:6]}-{text[6:]}"
            return text
    return None


def _is_semantic_text(mapping: ColumnMapping, value: Any) -> bool:
    return (mapping.source_column == "cu_strtegy" and isinstance(value, str)
            and len(value.strip()) >= 8)


def _quality(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "MISSING"
    return "VALID"


def _is_sentinel(value: str) -> bool:
    value = value.strip()
    return len(value) >= 3 and set(value) <= {"0"}


def _valid_currency(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip().upper()
    return text not in {"000", "CURR_CD_000", "UNKNOWN"}


def _valid_index(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip().casefold()
    if not text:
        return False
    return not any(
        sentinel in text
        for sentinel in (
            "not provided",
            "not available",
            "해당없음",
            "없음",
        )
    )


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()

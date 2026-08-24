import re
from dataclasses import dataclass, field
from typing import Any

from app.data.catalog import DatasetSpec
from app.data.cleaning import (
    as_float,
    canonical_asset_type,
    canonical_region,
    normalize_lookup_value,
    normalized_base_index,
    normalized_date,
)
from app.domain.models import CanonicalConcept


@dataclass
class MappedProduct:
    canonical: dict[str, Any]
    identifiers: list[dict[str, str]] = field(default_factory=list)
    bond_attributes: dict[str, Any] | None = None
    etf_attributes: dict[str, Any] | None = None
    fund: dict[str, Any] | None = None
    fund_class: dict[str, Any] | None = None
    quality_annotations: dict[str, str] = field(default_factory=dict)


def map_product(
    spec: DatasetSpec,
    row: dict[str, Any],
    *,
    source_file: str,
    source_row_number: int,
    snapshot: str,
) -> tuple[MappedProduct | None, str | None]:
    source_key = spec.source_record_key(row)
    canonical_id = spec.canonical_product_id(row)
    if source_key is None or canonical_id is None:
        return None, "MISSING_SOURCE_KEY"
    if spec.source_dataset == "public_fund" and not re.fullmatch(
        r"[A-Za-z0-9]{12}",
        str(row.get("itm_no", "")),
    ):
        return None, "CORRUPT_OR_SHIFTED_FUND_ROW"

    mapper = {
        "domestic_bond": _map_bond,
        "domestic_etf": _map_domestic_etf,
        "foreign_etf": _map_foreign_etf,
        "public_fund": _map_public_fund,
    }[spec.source_dataset]
    mapped, error = mapper(row, canonical_id, snapshot)
    if mapped is None:
        return None, error
    if not mapped.canonical.get("product_name"):
        return None, "MISSING_PRODUCT_NAME"
    mapped.canonical.update(
        {
            "source_dataset": spec.source_dataset,
            "source_record_key": source_key,
            "source_file": source_file,
            "source_row_number": source_row_number,
        }
    )
    mapped.identifiers.append(
        _identifier("source_id", source_key, spec.source_dataset)
    )
    return mapped, None


def _common(
    canonical_id: str,
    snapshot: str,
    *,
    product_type: str,
    product_name: Any,
    short_name: Any = None,
    ticker: Any = None,
    isin: Any = None,
    asset_manager: Any = None,
    issuer: Any = None,
    asset_type: Any = None,
    region: Any = None,
    risk_grade: Any = None,
    currency: Any = None,
    aum: Any = None,
    nav: Any = None,
    price: Any = None,
    expense_ratio: Any = None,
    base_index: Any = None,
    observed_at: Any = None,
) -> dict[str, Any]:
    return {
        "canonical_product_id": canonical_id,
        "dataset_snapshot": snapshot,
        "product_type": product_type,
        "product_name": product_name,
        "short_name": short_name,
        "normalized_product_name": normalize_lookup_value(str(product_name)),
        "normalized_short_name": (
            normalize_lookup_value(str(short_name))
            if short_name is not None
            else None
        ),
        "ticker": ticker,
        "isin": isin,
        "asset_manager": asset_manager,
        "issuer": issuer,
        "asset_type": asset_type,
        "region": region,
        "risk_grade": risk_grade,
        "currency": _canonical_currency(currency),
        "aum": as_float(aum),
        "nav": as_float(nav),
        "price": as_float(price),
        "expense_ratio": as_float(expense_ratio),
        "base_index": base_index,
        "observed_at": observed_at,
    }


def _map_bond(
    row: dict[str, Any],
    canonical_id: str,
    snapshot: str,
) -> tuple[MappedProduct, None]:
    issue_date, issue_quality = normalized_date(row.get("ISU_DT"))
    maturity_date, maturity_quality = normalized_date(row.get("MAT_DT"))
    observed_at, observed_quality = normalized_date(
        row.get("PD_STD_INFO_UPDATE")
    )
    annotations = {
        key: value
        for key, value in {
            "ISU_DT": issue_quality,
            "MAT_DT": maturity_quality,
            "PD_STD_INFO_UPDATE": observed_quality,
        }.items()
        if value is not None
    }
    isin = row.get("PD_NO")
    return (
        MappedProduct(
            canonical=_common(
                canonical_id,
                snapshot,
                product_type=CanonicalConcept.FINANCIAL_PRODUCT_BOND.value,
                product_name=row.get("PD_NM"),
                short_name=row.get("PD_ABRV_NM"),
                isin=isin,
                issuer=row.get("PD_PBCM"),
                asset_type=CanonicalConcept.ASSET_TYPE_BOND.value,
                region=canonical_region(row.get("PD_CTRY_CD")),
                risk_grade=row.get("PD_RISK_GCD"),
                currency=row.get("CURR_CD"),
                price=row.get("EVAL_PRICE"),
                observed_at=observed_at,
            ),
            identifiers=[_identifier("isin", isin, "domestic_bond")],
            bond_attributes={
                "canonical_product_id": canonical_id,
                "dataset_snapshot": snapshot,
                "issue_balance": as_float(row.get("ISU_BAL_AMT")),
                "issue_date": issue_date,
                "maturity_date": maturity_date,
                "buy_yield": as_float(row.get("BUY_YIELD")),
                "major_category": row.get("STD_PD_MCLS_NM"),
                "minor_category": row.get("STD_PD_SCLS_NM"),
            },
            quality_annotations=annotations,
        ),
        None,
    )


def _map_domestic_etf(
    row: dict[str, Any],
    canonical_id: str,
    snapshot: str,
) -> tuple[MappedProduct | None, str | None]:
    product_type = _exchange_product_type(row.get("pd_grp_no"))
    if product_type is None:
        return None, "UNSUPPORTED_PRODUCT_GROUP"
    base_index, base_quality = normalized_base_index(row.get("cu_base_index"))
    _, listing_end_quality = normalized_date(row.get("pd_lste_dt"))
    observed_at, _ = normalized_date(row.get("du_upt_dt"))
    isin = row.get("pd_itm_no")
    ticker = row.get("pd_itm_no_ma")
    annotations = {
        key: value
        for key, value in {
            "cu_base_index": base_quality,
            "pd_lste_dt": listing_end_quality,
        }.items()
        if value is not None
    }
    return (
        MappedProduct(
            canonical=_common(
                canonical_id,
                snapshot,
                product_type=product_type,
                product_name=row.get("pd_nm"),
                short_name=row.get("pd_abrv_nm"),
                ticker=ticker,
                isin=isin,
                asset_manager=row.get("cu_fund_mgmt_co"),
                issuer=row.get("cu_fund_mgmt_co"),
                asset_type=canonical_asset_type(row.get("wu_inv_ast_type")),
                region=canonical_region(row.get("wu_inv_rgn")),
                risk_grade=row.get("pd_risk_nm"),
                currency=row.get("pd_curr_cd"),
                aum=row.get("du_last_aum"),
                nav=row.get("du_last_nav"),
                price=row.get("du_clpr"),
                expense_ratio=row.get("cu_charge_rt"),
                base_index=base_index,
                observed_at=observed_at,
            ),
            identifiers=[
                _identifier("isin", isin, "domestic_etf"),
                _identifier("ticker", ticker, "domestic_etf"),
            ],
            etf_attributes={
                "canonical_product_id": canonical_id,
                "dataset_snapshot": snapshot,
                "strategy": row.get("cu_strtegy"),
                "replication_method": None,
                "leverage_factor": as_float(row.get("cu_lev_fector")),
                "distribution_cycle": row.get("pd_dvid_cycl"),
                "raw_product_group": row.get("pd_grp_no"),
            },
            quality_annotations=annotations,
        ),
        None,
    )


def _map_foreign_etf(
    row: dict[str, Any],
    canonical_id: str,
    snapshot: str,
) -> tuple[MappedProduct | None, str | None]:
    product_type = _exchange_product_type(row.get("pd_grp_no"))
    if product_type is None:
        return None, "UNSUPPORTED_PRODUCT_GROUP"
    base_index, base_quality = normalized_base_index(row.get("cu_base_index"))
    observed_at, _ = normalized_date(row.get("du_upt_dt"))
    isin = row.get("pd_isin_cd")
    ticker = row.get("pd_abrv_nm")
    identifiers = [
        _identifier("isin", isin, "foreign_etf"),
        _identifier("ticker", ticker, "foreign_etf"),
        _identifier("lipper_id", row.get("pd_lipper_id"), "foreign_etf"),
        _identifier("ma_id", row.get("pd_itm_no_ma"), "foreign_etf"),
    ]
    annotations = {"cu_base_index": base_quality} if base_quality else {}
    return (
        MappedProduct(
            canonical=_common(
                canonical_id,
                snapshot,
                product_type=product_type,
                product_name=row.get("pd_nm"),
                short_name=row.get("pd_abrv_nm"),
                ticker=ticker,
                isin=isin,
                asset_manager=row.get("cu_fund_mgmt_co"),
                issuer=row.get("cu_fund_mgmt_co"),
                asset_type=canonical_asset_type(row.get("wu_inv_ast_type")),
                region=canonical_region(row.get("wu_inv_rgn")),
                currency=row.get("pd_curr_cd") or row.get("pd_trd_ccy"),
                aum=row.get("du_last_aum"),
                nav=row.get("du_last_nav"),
                price=row.get("du_clpr"),
                expense_ratio=row.get("cu_charge_rt"),
                base_index=base_index,
                observed_at=observed_at,
            ),
            identifiers=identifiers,
            etf_attributes={
                "canonical_product_id": canonical_id,
                "dataset_snapshot": snapshot,
                "strategy": row.get("cu_strtegy"),
                "replication_method": row.get("cu_index_repl_mthd"),
                "leverage_factor": as_float(row.get("cu_lev_fector")),
                "distribution_cycle": None,
                "raw_product_group": row.get("pd_grp_no"),
            },
            quality_annotations=annotations,
        ),
        None,
    )


def _map_public_fund(
    row: dict[str, Any],
    canonical_id: str,
    snapshot: str,
) -> tuple[MappedProduct, None]:
    source_fund_id = str(row["itm_no"])
    fund_id = f"fund_pub:{source_fund_id}"
    class_code = str(row["prfd_attr_cd"])
    is_public = row.get("prvo_pbff_desc") == "공모"
    product_type = (
        CanonicalConcept.FINANCIAL_PRODUCT_PUBLIC_FUND.value
        if is_public
        else CanonicalConcept.FINANCIAL_PRODUCT_FUND.value
    )
    isin = row.get("std_itm_no") or row.get("itm_no")
    identifiers = [
        _identifier("source_fund_id", source_fund_id, "public_fund"),
        _identifier("isin", isin, "public_fund"),
        _identifier("ksd_id", row.get("ksd_itm_no"), "public_fund"),
        _identifier(
            "representative_ksd_id",
            row.get("rptt_ksd_itm_no"),
            "public_fund",
        ),
        _identifier("ma_id", row.get("mtco_itm_no"), "public_fund"),
        _identifier("fss_id", row.get("fss_itm_no"), "public_fund"),
    ]
    return (
        MappedProduct(
            canonical=_common(
                canonical_id,
                snapshot,
                product_type=product_type,
                product_name=row.get("itm_nm"),
                short_name=row.get("itm_abrv_nm"),
                isin=isin,
                issuer=row.get("or_co_xtn_itt_cd"),
                asset_type=canonical_asset_type(row.get("or_attr_desc")),
                region=canonical_region(row.get("fd_ivst_rgn_desc")),
                risk_grade=row.get("zrin_fd_ivst_risk_gcd"),
                currency=row.get("curr_cd"),
                aum=row.get("fd_nast_suma"),
                base_index=row.get("bmrk_nm"),
            ),
            identifiers=identifiers,
            fund={
                "fund_id": fund_id,
                "dataset_snapshot": snapshot,
                "source_fund_id": source_fund_id,
                "fund_name": row.get("itm_nm"),
                "representative_ksd_id": row.get("rptt_ksd_itm_no"),
            },
            fund_class={
                "canonical_product_id": canonical_id,
                "dataset_snapshot": snapshot,
                "fund_id": fund_id,
                "class_code": class_code,
                "raw_asset_category": row.get("or_attr_desc"),
                "public_private": row.get("prvo_pbff_desc"),
            },
        ),
        None,
    )


def _exchange_product_type(value: Any) -> str | None:
    normalized = str(value).strip().upper() if value is not None else ""
    return {
        "ETF": CanonicalConcept.FINANCIAL_PRODUCT_ETF.value,
        "ETN": CanonicalConcept.FINANCIAL_PRODUCT_ETN.value,
    }.get(normalized)


def _canonical_currency(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized.startswith("CURR_CD_"):
        normalized = normalized.removeprefix("CURR_CD_")
    return normalized or None


def _identifier(
    identifier_type: str,
    value: Any,
    source_dataset: str,
) -> dict[str, str]:
    raw = "" if value is None else str(value).strip()
    return {
        "identifier_type": identifier_type,
        "identifier_value": raw,
        "normalized_value": normalize_lookup_value(raw),
        "source_dataset": source_dataset,
    }

"""Audited M10.9-C1 comparison contracts and organizer cutoff policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal


EVALUATION_DATA_CUTOFF = date(2026, 8, 24)


@dataclass(frozen=True, slots=True)
class ComparisonContract:
    metric: str
    canonical_field: str
    dataset: str
    entity_grain: str
    unit: str | None
    scale: str | None
    currency: str | None
    observation_date_semantics: str
    filter_capability: bool
    sort_capability: bool
    cross_dataset_comparability: bool
    disabled_reason: str | None = None
    source_field: str | None = None
    exact_period: str | None = None
    percentage_representation: str | None = None
    value_basis: str | None = None
    adjustment_semantics: str | None = None
    missing_value_semantics: str = "preserve NULL; never coerce to zero"
    comparison_kind: str = "numeric_metric"
    ordered_values: tuple[str, ...] = ()
    answer_disclosure: str | None = None

    def as_plan_input(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CrossProductComparisonContract:
    metric: str
    participants: tuple[str, ...]
    comparability: Literal["YES", "NO", "PARTIAL"]
    reason: str

    @property
    def runtime_enabled(self) -> bool:
        return self.comparability == "YES"


PREF01_AUM = ComparisonContract(
    "AUM", "product.aum", "PREF01N001", "ExchangeTradedProduct",
    "CURRENCY_AMOUNT", "CURRENCY_UNIT", "KRW",
    "latest du_upt_dt observation in READY snapshot", False, True, False,
    source_field="du_last_aum",
)
PREF02_AUM = ComparisonContract(
    "AUM", "product.aum", "PREF02N001", "ExchangeTradedProduct",
    "CURRENCY_AMOUNT", "CURRENCY_UNIT", "USD",
    "latest du_upt_dt observation in READY snapshot", False, True, False,
    source_field="du_last_aum",
)
PREF01_EXPENSE = ComparisonContract(
    "EXPENSE_RATIO", "product.expense_ratio", "PREF01N001",
    "ExchangeTradedProduct", "RATE_REPRESENTATION_UNVERIFIED", None, None,
    "du_upt_dt source date", False, False, False,
    "PREF01 expense-ratio scale is not defined by the source contract",
    source_field="cu_charge_rt",
)
PREF02_EXPENSE = ComparisonContract(
    "EXPENSE_RATIO", "product.expense_ratio", "PREF02N001",
    "ExchangeTradedProduct", "RATE_REPRESENTATION_UNVERIFIED", None, None,
    "du_upt_dt source date", False, False, False,
    "PREF02 expense-ratio scale is not defined by the source contract",
    source_field="cu_charge_rt",
)


def _pref01_return_contract(
    metric: str,
    canonical_field: str,
    source_field: str,
    period: str,
) -> ComparisonContract:
    return ComparisonContract(
        metric,
        canonical_field,
        "PREF01N001",
        "ExchangeTradedProduct",
        "PERCENT",
        "SOURCE_PERCENT",
        None,
        (
            "du_upt_dt daily source update date in READY snapshot; "
            "undated observations are not rankable"
        ),
        False,
        True,
        False,
        source_field=source_field,
        exact_period=period,
        percentage_representation="percentage points as supplied; no rescaling",
        value_basis="source-defined ETP return basis",
        adjustment_semantics="not stated; comparison restricted to PREF01N001",
    )


PREF01_ONE_DAY_RETURN = _pref01_return_contract(
    "ONE_DAY_RETURN", "product.one_day_return", "du_er_1d", "1D"
)
PREF01_ONE_MONTH_RETURN = _pref01_return_contract(
    "ONE_MONTH_RETURN", "product.one_month_return", "du_er_1m", "1M"
)
PREF01_THREE_MONTH_RETURN = _pref01_return_contract(
    "THREE_MONTH_RETURN", "product.three_month_return", "du_er_3m", "3M"
)
PREF01_SIX_MONTH_RETURN = _pref01_return_contract(
    "SIX_MONTH_RETURN", "product.six_month_return", "du_er_6m", "6M"
)
PREF01_ONE_YEAR_RETURN = _pref01_return_contract(
    "ONE_YEAR_RETURN", "product.one_year_return", "du_er_1y", "1Y"
)
PREF01_YEAR_TO_DATE_RETURN = _pref01_return_contract(
    "YEAR_TO_DATE_RETURN", "product.year_to_date_return", "du_er_ytd", "YTD"
)

PREF01_RETURN_CONTRACTS = {
    item.canonical_field: item
    for item in (
        PREF01_ONE_DAY_RETURN,
        PREF01_ONE_MONTH_RETURN,
        PREF01_THREE_MONTH_RETURN,
        PREF01_SIX_MONTH_RETURN,
        PREF01_ONE_YEAR_RETURN,
        PREF01_YEAR_TO_DATE_RETURN,
    )
}
PREF02_ONE_YEAR_RETURN = ComparisonContract(
    "ONE_YEAR_RETURN", "product.one_year_return", "PREF02N001",
    "ExchangeTradedProduct", None, None, None,
    "no one-year return field exists", False, False, False,
    "PREF02N001 exposes du_er_1d only; ONE_YEAR_RETURN is unavailable",
    exact_period="1Y", percentage_representation="unavailable",
    value_basis="unavailable", adjustment_semantics="unavailable",
)
ISHARES_SCOPED_ONE_YEAR_RETURN = ComparisonContract(
    "ONE_YEAR_RETURN", "product.one_year_return", "ISHARES_US_PERFORMANCE",
    "ExchangeTradedProduct", "PERCENT", "ISHARES_NAV_TOTAL_RETURN_PCT_V1",
    None, "official iShares month-end asOfDate at or before evaluation cutoff",
    False, True, False, source_field="oneYearAnnualized.navSourced",
    exact_period="1Y",
    percentage_representation="percentage points as published; no rescaling",
    value_basis="issuer-published NAV total return",
    adjustment_semantics="accounts for distributions from the fund",
)
PRFD_SHARE_CLASS_ONE_YEAR_RETURN = ComparisonContract(
    "ONE_YEAR_RETURN", "product.one_year_return", "PRFD01N001",
    "FundShareClass", "PERCENT", "SOURCE_PERCENT", None,
    "fd_price_bas_dt return basis date in READY snapshot", False, True, False,
    source_field="fd_yr1_ern_r", exact_period="1Y",
    percentage_representation="percentage points as supplied; no rescaling",
    value_basis="source-defined FundShareClass return basis",
    adjustment_semantics="not stated; no Fund-family promotion",
)
PRBD_CREDIT_RATING = ComparisonContract(
    "CREDIT_RATING_ORDER", "product.credit_rating", "PRBD01N001", "Bond",
    "ORDINAL", "CREDIT_RATING_V1", None,
    "crd_grd_dt, falling back to info_base_dt in READY snapshot",
    True, False, False, source_field="crd_grd",
)
PRBD_PURCHASABLE_BOND = ComparisonContract(
    "ORGANIZER_PURCHASABLE_BOND", "product.current_sale_available",
    "PRBD01N001", "Bond", "BOOLEAN_RULE", "ORGANIZER_RULE_V1", None,
    "2026-08-24 canonical snapshot; exclude explicit lifecycle-end facts",
    True, False, False, source_field=None,
    missing_value_semantics=(
        "absence of a delisted/listing-ended fact is purchasable under organizer rule"
    ),
)

RISK_GRADE_ORDER = ComparisonContract(
    "RISK_GRADE_ORDER",
    "product.risk_grade",
    "PRBD01N001+PREF01N001+PRFD01N001",
    "FinancialProduct",
    "ORDINAL",
    "TEAM_ONTOLOGY_RISK_GRADE_V1",
    None,
    "READY canonical_v2 snapshot classification",
    False,
    True,
    True,
    source_field=(
        "PRBD/PREF01 pd_risk_nm; PRFD zrin_fd_ivst_risk_grd_nm"
    ),
    comparison_kind="ordered_vocabulary",
    # Ascending means lower risk first.  RiskGrade.6 is the ontology's
    # lowest-risk grade, while RiskGrade.1 is the highest-risk grade.
    ordered_values=(
        "RiskGrade.6",
        "RiskGrade.5",
        "RiskGrade.4",
        "RiskGrade.3",
        "RiskGrade.2",
        "RiskGrade.1",
    ),
    answer_disclosure="제공 데이터의 온톨로지 위험등급(낮은 위험 우선) 기준",
)


CROSS_PRODUCT_RETURN_CONTRACTS = (
    CrossProductComparisonContract(
        "ONE_YEAR_RETURN", ("DomesticETF", "ForeignETF"), "PARTIAL",
        "DomesticETF is ready, but PREF02N001 has no one-year return field",
    ),
    CrossProductComparisonContract(
        "ONE_YEAR_RETURN", ("DomesticETF", "ForeignETF", "PublicFund"), "PARTIAL",
        "DomesticETF is ready; PREF02 has no 1Y return and PRFD return is "
        "FundShareClass-grain only",
    ),
    CrossProductComparisonContract(
        "ONE_YEAR_RETURN",
        (
            "KODEX_LONG_ONLY_COMPATIBLE",
            "TIGER_LONG_ONLY_COMPATIBLE",
            "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
        ),
        "NO",
        "PREF01 defines only source 1Y return; NAV/market-price basis and "
        "distribution treatment are not documented sufficiently to compare it "
        "with iShares NAV total return",
    ),
)


class MetricCapabilityRegistry:
    """Resolve a query scope to a deterministic, source-scoped contract."""

    contracts = (
        PREF01_AUM, PREF02_AUM, PREF01_EXPENSE, PREF02_EXPENSE,
        *PREF01_RETURN_CONTRACTS.values(), PREF02_ONE_YEAR_RETURN,
        ISHARES_SCOPED_ONE_YEAR_RETURN, PRFD_SHARE_CLASS_ONE_YEAR_RETURN,
        PRBD_CREDIT_RATING,
        PRBD_PURCHASABLE_BOND,
        RISK_GRADE_ORDER,
    )

    # A default is a reviewed domain policy, not an LLM guess.  Explicit
    # periods always bypass this map because they already ground directly to
    # their concrete canonical field.
    default_comparable_metrics = {
        "product.return": "product.one_year_return",
    }
    return_default_period = "1Y"
    natural_metric_aliases = {
        "수익률": "product.return",
        "return": "product.return",
        "1d 수익률": "product.one_day_return",
        "1일 수익률": "product.one_day_return",
        "1m 수익률": "product.one_month_return",
        "1개월 수익률": "product.one_month_return",
        "3m 수익률": "product.three_month_return",
        "3개월 수익률": "product.three_month_return",
        "6m 수익률": "product.six_month_return",
        "6개월 수익률": "product.six_month_return",
        "1y 수익률": "product.one_year_return",
        "1년 수익률": "product.one_year_return",
        "연 수익률": "product.one_year_return",
        "ytd 수익률": "product.year_to_date_return",
        "연초 이후 수익률": "product.year_to_date_return",
    }

    @classmethod
    def canonical_metric_alias(cls, raw: str) -> str | None:
        return cls.natural_metric_aliases.get(raw.strip().casefold())

    @classmethod
    def default_metric(cls, canonical_field: str | None) -> str | None:
        if canonical_field is None:
            return None
        return cls.default_comparable_metrics.get(canonical_field, canonical_field)

    credit_rating_order = {
        value: rank
        for rank, value in enumerate(
            (
                "C0", "B-", "B+", "BB-", "BB0", "BBB-", "BBB0",
                "BBB+", "A-", "A0", "A+", "AA-", "AA0", "AA+", "AAA",
            ),
            start=1,
        )
    }

    def comparison_contract(
        self, canonical_field: str, inputs: dict[str, Any]
    ) -> tuple[ComparisonContract | None, str | None]:
        universe_input = inputs.get("product_universe") or {}
        universe = tuple(universe_input.get("operands", ()))
        reviewed_domestic_holdings = {
            "KODEX_LONG_ONLY_COMPATIBLE", "TIGER_LONG_ONLY_COMPATIBLE",
        }
        provider_union = bool(universe) and set(universe).issubset(
            reviewed_domestic_holdings
        )
        if canonical_field == "product.expense_ratio":
            return None, "expense_ratio_scale_unverified"
        if canonical_field == "product.risk_grade":
            return RISK_GRADE_ORDER, None
        if canonical_field in PREF01_RETURN_CONTRACTS:
            if universe == ("DomesticETF",) or provider_union:
                return PREF01_RETURN_CONTRACTS[canonical_field], None
            if (
                canonical_field == "product.one_year_return"
                and universe == ("ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",)
            ):
                return ISHARES_SCOPED_ONE_YEAR_RETURN, None
            if set(universe) == {
                "KODEX_LONG_ONLY_COMPATIBLE",
                "TIGER_LONG_ONLY_COMPATIBLE",
                "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
            }:
                return None, "domestic_vs_ishares_return_basis_not_comparable"
            period = PREF01_RETURN_CONTRACTS[canonical_field].exact_period
            if "PublicFund" in universe or "Fund" in universe:
                return None, f"public_fund_return_{period}_not_comparable_at_fund_grain"
            if "ForeignETF" in universe or "ETF" in universe:
                return None, f"foreign_etf_return_{period}_unavailable_or_incompatible"
            return None, f"return_{period}_product_scope_not_verified"
        if canonical_field != "product.aum":
            return None, f"ordered_comparison_not_supported:{canonical_field}"

        listing_country = self._eq_filter(inputs, "product.listing_country")
        currency = self._eq_filter(inputs, "product.currency")
        if universe == ("DomesticETF",) or provider_union or currency == "KRW":
            return PREF01_AUM, None
        if listing_country == "US":
            return PREF02_AUM, None
        return None, "aum_scope_spans_or_cannot_exclude_incompatible_sources"

    def prepare(self, inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        prepared = dict(inputs)
        contracts: list[dict[str, Any]] = []
        unsupported: list[str] = []
        unsupported_reasons: list[str] = []
        cross_product_contracts: list[dict[str, Any]] = []
        sort_items = list(prepared.get("sort", []))
        sort_constraint_ids = prepared.get("sort_constraint_ids", [])
        aliases = {
            "총보수": "product.expense_ratio", "보수율": "product.expense_ratio",
            "운용보수": "product.expense_ratio", "순자산": "product.aum",
            "aum": "product.aum", "운용규모": "product.aum",
            "연 수익률": "product.one_year_return",
            "연수익률": "product.one_year_return",
            "1년 수익률": "product.one_year_return",
            "1년수익률": "product.one_year_return",
            "1y 수익률": "product.one_year_return",
            "1y수익률": "product.one_year_return",
            "1d 수익률": "product.one_day_return",
            "1d수익률": "product.one_day_return",
            "1일 수익률": "product.one_day_return",
            "1일수익률": "product.one_day_return",
            "1m 수익률": "product.one_month_return",
            "1m수익률": "product.one_month_return",
            "1개월 수익률": "product.one_month_return",
            "1개월수익률": "product.one_month_return",
            "3m 수익률": "product.three_month_return",
            "3m수익률": "product.three_month_return",
            "3개월 수익률": "product.three_month_return",
            "3개월수익률": "product.three_month_return",
            "6m 수익률": "product.six_month_return",
            "6m수익률": "product.six_month_return",
            "6개월 수익률": "product.six_month_return",
            "6개월수익률": "product.six_month_return",
            "ytd 수익률": "product.year_to_date_return",
            "ytd수익률": "product.year_to_date_return",
            "연초 이후 수익률": "product.year_to_date_return",
            "연초이후수익률": "product.year_to_date_return",
            "수익률": "product.one_year_return",
            "위험": "product.risk_grade",
            "위험등급": "product.risk_grade",
        }
        for index, item in enumerate(sort_items):
            field = item.get("canonical_field") if isinstance(item, dict) else None
            if field is None and isinstance(item, dict):
                field = aliases.get(str(item.get("raw", {}).get("field", "")).casefold())
            universe_input = prepared.get("product_universe") or {}
            universe = tuple(universe_input.get("operands", ()))
            cross_contract = next(
                (
                    value
                    for value in CROSS_PRODUCT_RETURN_CONTRACTS
                    if field == "product.one_year_return"
                    and value.participants == universe
                ),
                None,
            )
            if cross_contract is not None:
                cross_product_contracts.append(asdict(cross_contract))
            contract, reason = self.comparison_contract(str(field), prepared)
            if contract is None:
                constraint_id = sort_constraint_ids[index] if index < len(sort_constraint_ids) else None
                if constraint_id:
                    unsupported.append(str(constraint_id))
                if reason:
                    unsupported_reasons.append(reason)
            else:
                plan_contract = contract.as_plan_input()
                raw_field = str(item.get("raw", {}).get("field", ""))
                if field in PREF01_RETURN_CONTRACTS or (
                    field == "product.one_year_return"
                    and contract is ISHARES_SCOPED_ONE_YEAR_RETURN
                ):
                    default_applied = raw_field.strip().casefold() in {
                        "수익률", "return"
                    }
                    plan_contract["metric_resolution"] = {
                        "status": (
                            "RESOLVABLE_UNDERSPECIFICATION"
                            if default_applied else "EXPLICIT_PERIOD"
                        ),
                        "policy": (
                            f"RETURN.default_period={self.return_default_period}"
                            if default_applied else "query_explicit_period"
                        ),
                        "requested_phrase": raw_field,
                        "resolved_metric": field,
                        "metric": "RETURN",
                        "period": contract.exact_period,
                        "period_source": (
                            "DEFAULT_POLICY" if default_applied else "EXPLICIT_QUERY"
                        ),
                        **(
                            {
                                "disclosure": (
                                    "기간이 별도로 지정되지 않아 1년 수익률을 "
                                    "기준으로 비교했습니다."
                                )
                            }
                            if default_applied else {}
                        ),
                    }
                contracts.append(plan_contract)

        if prepared.get("top_n") is not None and not sort_items:
            if prepared.get("limit_constraint_id"):
                unsupported.append(str(prepared["limit_constraint_id"]))
            unsupported_reasons.append("top_n_requires_explicit_sort")

        filter_constraint_ids = prepared.get("filter_constraint_ids", [])
        for index, item in enumerate(prepared.get("filters", [])):
            if not isinstance(item, dict):
                continue
            field = item.get("canonical_field")
            if field == "product.current_sale_available":
                contracts.append(PRBD_PURCHASABLE_BOND.as_plan_input())
                continue
            if field != "product.credit_rating":
                continue
            value = item.get("canonical_value")
            if str(value).upper() not in self.credit_rating_order:
                if index < len(filter_constraint_ids) and filter_constraint_ids[index]:
                    unsupported.append(str(filter_constraint_ids[index]))
                unsupported_reasons.append("invalid_credit_rating")
            else:
                contracts.append(PRBD_CREDIT_RATING.as_plan_input())

        contracts = list({str(item["canonical_field"]): item for item in contracts}.values())
        prepared["comparison_contracts"] = contracts
        prepared["cross_product_comparison_contracts"] = cross_product_contracts
        prepared["comparison_unsupported_reasons"] = list(dict.fromkeys(unsupported_reasons))
        prepared["evaluation_data_cutoff"] = EVALUATION_DATA_CUTOFF.isoformat()
        return prepared, list(dict.fromkeys(unsupported))

    @staticmethod
    def _eq_filter(inputs: dict[str, Any], canonical_field: str) -> Any:
        for item in inputs.get("filters", []):
            if not isinstance(item, dict) or item.get("canonical_field") != canonical_field:
                continue
            raw = item.get("raw", {})
            if raw.get("operator") == "eq":
                return item.get("canonical_value", raw.get("value"))
        return None

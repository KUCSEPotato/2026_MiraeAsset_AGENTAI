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
        percentage_representation="percent value as supplied; no rescaling",
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
    percentage_representation="percent value as published; no rescaling",
    value_basis="issuer-published NAV total return",
    adjustment_semantics="accounts for distributions from the fund",
)
PRFD_SHARE_CLASS_ONE_YEAR_RETURN = ComparisonContract(
    "ONE_YEAR_RETURN", "product.one_year_return", "PRFD01N001",
    "FundShareClass", "PERCENT", "SOURCE_PERCENT", None,
    "fd_price_bas_dt return basis date in READY snapshot", False, True, False,
    source_field="fd_yr1_ern_r", exact_period="1Y",
    percentage_representation="percent value as supplied; no rescaling",
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

RISK_GRADE_UNVERIFIED_REASON = "risk_grade_ordering_and_comparability_unverified"

# Source schemas name product risk grades and ZeroIn fund risk grades, but do
# not establish equivalent assessment methods or a shared comparison scale.
# Ontology aliases alone are not an ordering/comparability authorization.
RISK_GRADE_ORDER = ComparisonContract(
    "RISK_GRADE_ORDER",
    "product.risk_grade",
    "PRBD01N001+PREF01N001+PRFD01N001",
    "FinancialProduct",
    None,
    None,
    None,
    "READY canonical_v2 snapshot classification",
    False,
    False,
    False,
    disabled_reason=RISK_GRADE_UNVERIFIED_REASON,
    source_field=(
        "PRBD/PREF01 pd_risk_nm; PRFD zrin_fd_ivst_risk_grd_nm"
    ),
    comparison_kind="source_classification",
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
        "오늘 수익률": "product.one_day_return",
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
        "올해 수익률": "product.year_to_date_return",
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
        if not universe:
            universe = self._resolved_entity_universe(inputs)
        reviewed_domestic_holdings = {
            "KODEX_LONG_ONLY_COMPATIBLE", "TIGER_LONG_ONLY_COMPATIBLE",
        }
        provider_union = bool(universe) and set(universe).issubset(
            reviewed_domestic_holdings
        )
        if canonical_field == "product.expense_ratio":
            return None, "expense_ratio_scale_unverified"
        if canonical_field == "product.risk_grade":
            return None, RISK_GRADE_UNVERIFIED_REASON
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
                raw_field = str(item.get("raw", {}).get("field", ""))
                contracts.append(
                    self._plan_contract(contract, str(field), raw_field)
                )

        requested_constraint_ids = prepared.get(
            "requested_field_constraint_ids", []
        )
        for index, item in enumerate(
            prepared.get("requested_field_details", [])
        ):
            if not isinstance(item, dict):
                continue
            field = str(item.get("canonical_field", ""))
            if field not in PREF01_RETURN_CONTRACTS:
                continue
            contract, reason = self.comparison_contract(field, prepared)
            if contract is None:
                if (
                    index < len(requested_constraint_ids)
                    and requested_constraint_ids[index]
                ):
                    unsupported.append(str(requested_constraint_ids[index]))
                if reason:
                    unsupported_reasons.append(reason)
                continue
            contracts.append(
                self._plan_contract(contract, field, str(item.get("raw", "")))
            )

        if prepared.get("top_n") is not None and not sort_items:
            if prepared.get("limit_constraint_id"):
                unsupported.append(str(prepared["limit_constraint_id"]))
            unsupported_reasons.append("top_n_requires_explicit_sort")

        comparison = prepared.get("comparison")
        if comparison is not None:
            if (
                not isinstance(comparison, dict)
                or comparison.get("mode") != "fieldwise"
                or not isinstance(comparison.get("fields"), list)
                or not comparison["fields"]
                or not all(isinstance(field, str) for field in comparison["fields"])
            ):
                unsupported_reasons.append("invalid_comparison_specification")
            else:
                for field in dict.fromkeys(comparison["fields"]):
                    contract, reason = self.comparison_contract(field, prepared)
                    if contract is None:
                        unsupported_reasons.append(reason or f"comparison_unsupported:{field}")
                    else:
                        contracts.append(contract.as_plan_input())

        filter_constraint_ids = prepared.get("filter_constraint_ids", [])
        for index, item in enumerate(prepared.get("filters", [])):
            if not isinstance(item, dict):
                continue
            field = item.get("canonical_field")
            if field == "product.risk_grade":
                if index < len(filter_constraint_ids) and filter_constraint_ids[index]:
                    unsupported.append(str(filter_constraint_ids[index]))
                unsupported_reasons.append(RISK_GRADE_UNVERIFIED_REASON)
                continue
            if field == "product.current_sale_available":
                contracts.append(PRBD_PURCHASABLE_BOND.as_plan_input())
                continue
            if field != "product.credit_rating":
                continue
            value = item.get("canonical_value")
            values = value if isinstance(value, list) else [value]
            if not values or any(str(item).upper() not in self.credit_rating_order for item in values):
                if index < len(filter_constraint_ids) and filter_constraint_ids[index]:
                    unsupported.append(str(filter_constraint_ids[index]))
                unsupported_reasons.append("invalid_credit_rating")
            else:
                contracts.append(PRBD_CREDIT_RATING.as_plan_input())

        # Multiple operators can consume one field.  Preserve the first
        # contract (including period disclosure), reject semantic disagreement.
        by_field: dict[str, dict[str, Any]] = {}
        for contract in contracts:
            field = str(contract["canonical_field"])
            previous = by_field.get(field)
            if previous is not None:
                if _contract_semantics(previous) != _contract_semantics(contract):
                    unsupported_reasons.append(f"conflicting_comparison_contracts:{field}")
            else:
                by_field[field] = contract
        contracts = list(by_field.values())
        prepared["comparison_contracts"] = contracts
        prepared["cross_product_comparison_contracts"] = cross_product_contracts
        prepared["comparison_unsupported_reasons"] = list(dict.fromkeys(unsupported_reasons))
        prepared["evaluation_data_cutoff"] = EVALUATION_DATA_CUTOFF.isoformat()
        return prepared, list(dict.fromkeys(unsupported))

    def _plan_contract(
        self,
        contract: ComparisonContract,
        canonical_field: str,
        raw_field: str,
    ) -> dict[str, Any]:
        plan_contract = contract.as_plan_input()
        if canonical_field not in PREF01_RETURN_CONTRACTS and not (
            canonical_field == "product.one_year_return"
            and contract is ISHARES_SCOPED_ONE_YEAR_RETURN
        ):
            return plan_contract
        default_applied = raw_field.strip().casefold() in {"수익률", "return"}
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
            "resolved_metric": canonical_field,
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
        return plan_contract

    @staticmethod
    def _resolved_entity_universe(inputs: dict[str, Any]) -> tuple[str, ...]:
        entity_ids = inputs.get("entity_ids", [])
        if (
            isinstance(entity_ids, list)
            and entity_ids
            and all(
                isinstance(entity_id, str)
                and entity_id.casefold().startswith("etf_kr:")
                for entity_id in entity_ids
            )
        ):
            return ("DomesticETF",)
        return ()

    def verified_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Recompute authorization at execution, independent of plan ledgers."""
        prepared, unsupported = self.prepare(inputs)
        reasons = prepared["comparison_unsupported_reasons"]
        if unsupported or reasons:
            raise ValueError("unsupported comparison: " + ",".join([*reasons, *unsupported]))
        supplied = inputs.get("comparison_contracts", [])
        if not isinstance(supplied, list):
            raise ValueError("comparison contracts must be a list")
        expected = {item["canonical_field"]: item for item in prepared["comparison_contracts"]}
        seen: set[str] = set()
        for contract in supplied:
            if not isinstance(contract, dict):
                raise ValueError("comparison contract must be structured")
            field = contract.get("canonical_field")
            if not isinstance(field, str) or field in seen or field not in expected:
                raise ValueError("unknown or duplicate comparison contract")
            seen.add(field)
            canonical = expected[field]
            # Metadata disclosures do not authorize a unit, scale or source.
            if _contract_semantics(contract) != _contract_semantics(canonical):
                raise ValueError(f"unverified comparison contract:{field}")
        return prepared

    @staticmethod
    def _eq_filter(inputs: dict[str, Any], canonical_field: str) -> Any:
        expression = inputs.get("boolean_expression")
        represented: set[str] = set()

        def guaranteed(node) -> set[str]:
            if not isinstance(node, dict):
                return set()
            if node.get("node_type") == "predicate":
                identifier = node.get("constraint_id")
                if isinstance(identifier, str):
                    represented.add(identifier)
                    return {identifier}
                return set()
            children = [guaranteed(child) for child in node.get("children", [])]
            if not children:
                return set()
            if node.get("node_type") == "and":
                return set.union(*children)
            if node.get("node_type") == "or":
                return set.intersection(*children)
            return set()

        unconditional = guaranteed(expression)
        identifiers = inputs.get("filter_constraint_ids", [])
        for index, item in enumerate(inputs.get("filters", [])):
            if not isinstance(item, dict) or item.get("canonical_field") != canonical_field:
                continue
            identifier = identifiers[index] if index < len(identifiers) else None
            if identifier in represented and identifier not in unconditional:
                continue
            raw = item.get("raw", {})
            if raw.get("operator") == "eq":
                return item.get("canonical_value", raw.get("value"))
        return None


def _contract_semantics(value: Any) -> Any:
    """JSON and in-process plans carry the same tuple/list contract values."""
    if isinstance(value, dict):
        return {
            key: _contract_semantics(item)
            for key, item in value.items()
            if key != "metric_resolution"
        }
    if isinstance(value, (list, tuple)):
        return [_contract_semantics(item) for item in value]
    return value

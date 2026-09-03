from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Iterable

from app.domain.models import (
    CanonicalSemanticValue,
    ConceptCategory,
    SemanticCapabilityState,
)
from app.ontology.index import normalize_ontology_text


ONTOLOGY_URI = "https://miraeasset.com/ontology/financial-product"
ONTOLOGY_NAMESPACE = f"{ONTOLOGY_URI}#"
ONTOLOGY_VERSION = "merged-optical-1.4"
SEMANTIC_MAPPING_VERSION = "team-v1-runtime-2026-09-03.1"
DATASET_SNAPSHOT = "2026-08-24"

BOND_TYPE_RESOURCES = {
    "할부금융채": "BOND_TYPE_INSTALLMENT_FINANCE",
    "일반특수법인채": "BOND_TYPE_GENERAL_SPECIAL_CORPORATE",
    "일반회사채": "BOND_TYPE_GENERAL_CORPORATE",
    "신용카드채": "BOND_TYPE_CREDIT_CARD",
    "지역개발채": "BOND_TYPE_REGIONAL_DEVELOPMENT",
    "유동화회사채": "BOND_TYPE_ASSET_BACKED_CORPORATE",
    "MBS": "BOND_TYPE_MBS",
    "특수은행채": "BOND_TYPE_SPECIAL_BANK",
    "Conduit회사채": "BOND_TYPE_CONDUIT",
    "시설대여채(리스)": "BOND_TYPE_FACILITY_LEASE",
    "일반은행채": "BOND_TYPE_GENERAL_BANK",
    "금융지주회사채": "BOND_TYPE_FINANCIAL_HOLDING",
    "도시철도공채": "BOND_TYPE_URBAN_RAIL",
    "국고채권": "BOND_TYPE_KOREAN_TREASURY",
    "일반지방공사채": "BOND_TYPE_LOCAL_PUBLIC_CORPORATION",
    "투자매매.중개채": "BOND_TYPE_BROKER_DEALER",
    "지방공사보상채권": "BOND_TYPE_LOCAL_CORPORATION_COMPENSATION",
    "국민주택1종": "BOND_TYPE_NATIONAL_HOUSING_1",
    "특수보상채권": "BOND_TYPE_SPECIAL_COMPENSATION",
    "모집지방채": "BOND_TYPE_SUBSCRIPTION_LOCAL",
    "보험회사채": "BOND_TYPE_INSURANCE_COMPANY",
    "국민주택2종": "BOND_TYPE_NATIONAL_HOUSING_2",
    "기타금융회사채": "BOND_TYPE_OTHER_FINANCE_COMPANY",
    "부동산투자회사채": "BOND_TYPE_REAL_ESTATE_INVESTMENT_COMPANY",
    "통화안정채권": "BOND_TYPE_MONETARY_STABILIZATION",
    "기타금융투자전업회사채": "BOND_TYPE_OTHER_FINANCIAL_INVESTMENT",
    "증권금융채(특수금융)": "BOND_TYPE_SECURITIES_FINANCE",
    "기업인수목적회사채": "BOND_TYPE_SPAC",
    "외국환평형기금": "BOND_TYPE_FOREIGN_EXCHANGE_STABILIZATION",
    "집합투자회사채": "BOND_TYPE_COLLECTIVE_INVESTMENT_COMPANY",
    "재정증권": "BOND_TYPE_TREASURY_BILL",
    "유동화수익증권": "BOND_TYPE_ASSET_BACKED_BENEFICIARY_CERTIFICATE",
}


@dataclass(frozen=True, slots=True)
class ConceptMapping:
    canonical_name: str
    category: str
    runtime_key: str
    aliases: tuple[str, ...]
    ontology_resource: str | None
    capability: SemanticCapabilityState = SemanticCapabilityState.ACTIVE
    legacy_names: tuple[str, ...] = ()

    @property
    def ontology_uri(self) -> str | None:
        if self.ontology_resource is None:
            return None
        return f"{ONTOLOGY_NAMESPACE}{self.ontology_resource}"

    def semantic_value(self) -> CanonicalSemanticValue | None:
        if (
            self.ontology_uri is None
            or self.capability is not SemanticCapabilityState.ACTIVE
        ):
            return None
        return CanonicalSemanticValue(
            ontology_uri=self.ontology_uri,
            canonical_name=self.canonical_name,
            category=self.category,
            runtime_key=self.runtime_key,
            mapping_version=SEMANTIC_MAPPING_VERSION,
            capability=self.capability,
            legacy_names=self.legacy_names,
        )


@dataclass(frozen=True, slots=True)
class FieldMapping:
    canonical_field: str
    ontology_resource: str
    aliases: tuple[str, ...]
    storage_backend: str
    physical_mapping: str
    operations: frozenset[str]
    capability: SemanticCapabilityState = SemanticCapabilityState.ACTIVE
    source_unit: str | None = None
    normalized_unit: str | None = None
    scale: str | None = None
    nullable_policy: str = "preserve_null"
    sentinel_policy: str = "reject_known_sentinels"

    @property
    def ontology_uri(self) -> str:
        return f"{ONTOLOGY_NAMESPACE}{self.ontology_resource}"


@dataclass(frozen=True, slots=True)
class RelationMapping:
    canonical_relation: str
    ontology_resource: str
    aliases: tuple[str, ...]
    edge_type: str
    capability: SemanticCapabilityState = SemanticCapabilityState.ACTIVE
    legacy_names: tuple[str, ...] = ()

    @property
    def ontology_uri(self) -> str:
        return f"{ONTOLOGY_NAMESPACE}{self.ontology_resource}"


@dataclass(frozen=True, slots=True)
class MetricFamilyMapping:
    metric_family: str
    semantic_keys: tuple[str, ...]
    canonical_field: str
    physical_mapping: str
    capability: SemanticCapabilityState

    @property
    def ontology_uri(self) -> str:
        return f"{ONTOLOGY_NAMESPACE}{self.metric_family}"


class TeamOntologyRuntimeMapping:
    """Single M10.7 translation boundary for the 2026-08-24 release.

    Entries with ``ontology_resource=None`` intentionally describe recognized
    legacy meanings for which merged-optical-1.3 has no declared controlled
    individual.  They can be reported as capabilities but cannot be grounded
    to an invented ontology URI.
    """

    def __init__(self) -> None:
        self.concepts = _concept_mappings()
        self.fields = _field_mappings()
        self.relations = _relation_mappings()
        self.metrics = _metric_mappings()
        self._concept_aliases = _index_aliases(
            self.concepts,
            lambda item: (*item.aliases, item.canonical_name, item.runtime_key,
                          *item.legacy_names),
            lambda item: item.category,
        )
        self._field_aliases = _flat_aliases(
            self.fields,
            lambda item: (*item.aliases, item.canonical_field),
        )
        self._relation_aliases = _flat_aliases(
            self.relations,
            lambda item: (
                *item.aliases,
                item.canonical_relation,
                item.ontology_resource,
                *item.legacy_names,
            ),
        )

    def concept(
        self,
        raw: str,
        category: ConceptCategory | str,
    ) -> ConceptMapping | None:
        key = category.value if isinstance(category, ConceptCategory) else category
        compatibility = {
            ConceptCategory.REGION.value: ConceptCategory.EXPOSURE_REGION.value,
            ConceptCategory.ASSET_TYPE.value: ConceptCategory.ASSET_CLASS.value,
        }
        key = compatibility.get(key, key)
        return self._concept_aliases.get((key, normalize_ontology_text(raw)))

    def field(self, raw: str) -> FieldMapping | None:
        return self._field_aliases.get(normalize_ontology_text(raw))

    def relation(self, raw: str) -> RelationMapping | None:
        return self._relation_aliases.get(normalize_ontology_text(raw))

    def metric(self, semantic_key: str) -> MetricFamilyMapping | None:
        normalized = normalize_ontology_text(semantic_key)
        return next(
            (
                item
                for item in self.metrics
                if any(
                    normalize_ontology_text(key) == normalized
                    for key in item.semantic_keys
                )
            ),
            None,
        )

    def unsupported_concept(self, raw: str) -> ConceptMapping | None:
        normalized = normalize_ontology_text(raw)
        return next(
            (
                item
                for item in self.concepts
                if item.capability is not SemanticCapabilityState.ACTIVE
                and normalized
                in {
                    normalize_ontology_text(value)
                    for value in (*item.aliases, item.canonical_name)
                }
            ),
            None,
        )


def _concept_mappings() -> tuple[ConceptMapping, ...]:
    active = SemanticCapabilityState.ACTIVE
    prospective = SemanticCapabilityState.PROSPECTIVE
    unsupported = SemanticCapabilityState.UNSUPPORTED_BY_CURRENT_SNAPSHOT
    return (
        ConceptMapping("FinancialProduct.ETF", "product_type", "FinancialProduct.ETF", ("ETF", "상장지수펀드"), "ETF"),
        ConceptMapping("FinancialProduct.ETN", "product_type", "FinancialProduct.ETN", ("ETN", "상장지수증권"), "ETN"),
        ConceptMapping("FinancialProduct.Bond", "product_type", "FinancialProduct.Bond", ("Bond", "채권"), "Bond"),
        ConceptMapping(
            "FinancialProduct.Fund",
            "product_type",
            "FinancialProduct.Fund",
            ("Fund", "펀드"),
            "Fund",
        ),
        # PublicFund is not a Team Ontology product subclass.  The compatibility
        # key remains physical only; grounding is Fund + OfferingType.PUBLIC.
        ConceptMapping(
            "FinancialProduct.Fund",
            "product_type",
            "FinancialProduct.PublicFund",
            ("공모펀드", "공모 펀드"),
            "Fund",
            active,
            ("FinancialProduct.PublicFund",),
        ),
        ConceptMapping(
            "FundShareClass",
            "classification",
            "FundShareClass",
            ("펀드 클래스", "판매 클래스"),
            "FundShareClass",
        ),
        ConceptMapping("OfferingType.PUBLIC", "offering_type", "OfferingType.PUBLIC", ("공모", "Public"), "PUBLIC", active, ("FinancialProduct.PublicFund", "공모펀드")),
        ConceptMapping("OfferingType.PRIVATE", "offering_type", "OfferingType.PRIVATE", ("사모", "Private"), "PRIVATE"),
        ConceptMapping("SubscriptionStatus.OPEN_FOR_SUBSCRIPTION", "subscription_status", "SubscriptionStatus.OPEN_FOR_SUBSCRIPTION", ("판매중", "가입 가능", "추가매수 가능"), "OPEN_FOR_SUBSCRIPTION"),
        ConceptMapping("SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION", "subscription_status", "SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION", ("판매완료", "가입 종료", "추가매수 종료"), "CLOSED_FOR_SUBSCRIPTION"),
        ConceptMapping("AssetClass.Equity", "asset_class", "AssetType.Equity", ("Equity", "주식", "주식형"), "ASSET_EQUITY", active, ("AssetType.Equity",)),
        ConceptMapping("AssetClass.Bond", "asset_class", "AssetType.Bond", ("Bond", "채권", "채권형"), "ASSET_BOND", active, ("AssetType.Bond",)),
        ConceptMapping("AssetClass.Commodity", "asset_class", "AssetType.Commodity", ("Commodity", "원자재"), "ASSET_COMMODITY", active, ("AssetType.Commodity",)),
        ConceptMapping("AssetClass.Mixed", "asset_class", "AssetType.Mixed", ("Mixed Assets", "혼합자산", "채권혼합", "주식혼합"), "ASSET_MIXED", active, ("AssetType.Mixed",)),
        ConceptMapping("AssetClass.MoneyMarket", "asset_class", "AssetType.MoneyMarket", ("Money Market", "단기자금", "MMF"), "ASSET_MONEY_MARKET", active, ("AssetType.MoneyMarket",)),
        ConceptMapping("AssetClass.Currency", "asset_class", "AssetType.Currency", ("Currency", "통화"), "ASSET_CURRENCY", active, ("AssetType.Currency",)),
        ConceptMapping("AssetClass.RealEstate", "asset_class", "AssetType.RealEstate", ("Real Estate", "부동산"), "ASSET_REAL_ESTATE", active, ("AssetType.RealEstate",)),
        ConceptMapping("AssetClass.Alternative", "asset_class", "AssetType.Alternative", ("Alternatives", "대체투자"), "ASSET_ALTERNATIVE", active, ("AssetType.Alternative",)),
        ConceptMapping("AssetClass.Other", "asset_class", "AssetType.Other", ("Other", "기타"), "ASSET_OTHER", active, ("AssetType.Other",)),
        ConceptMapping("ExposureRegion.UnitedStates", "exposure_region", "Region.US", ("미국", "USA", "United States", "United States of America"), "REGION_UNITED_STATES", active, ("Region.US",)),
        ConceptMapping("ExposureRegion.Korea", "exposure_region", "Region.KR", ("한국", "국내", "Korea"), "REGION_KOREA", active, ("Region.KR",)),
        ConceptMapping("ExposureRegion.Japan", "exposure_region", "Region.JP", ("일본", "Japan"), "REGION_JAPAN", active, ("Region.JP",)),
        ConceptMapping("ExposureRegion.China", "exposure_region", "Region.CN", ("중국", "China"), "REGION_CHINA", active, ("Region.CN",)),
        ConceptMapping("ExposureRegion.India", "exposure_region", "Region.IN", ("인도", "India"), "REGION_INDIA", active, ("Region.IN",)),
        ConceptMapping("ExposureRegion.Global", "exposure_region", "Region.Global", ("글로벌", "Global"), "REGION_GLOBAL", active, ("Region.Global",)),
        ConceptMapping("ExposureRegion.Asia", "exposure_region", "Region.Asia", ("아시아", "Asia"), "REGION_ASIA", active, ("Region.Asia",)),
        ConceptMapping("MarketScope.Domestic", "market_scope", "MarketScope.Domestic", ("국내", "Domestic"), "MARKET_DOMESTIC"),
        ConceptMapping("MarketScope.Overseas", "market_scope", "MarketScope.Overseas", ("해외", "Overseas"), "MARKET_OVERSEAS"),
        ConceptMapping("MarketScope.Mixed", "market_scope", "MarketScope.Mixed", ("국내외혼합", "Domestic and Overseas"), "MARKET_MIXED"),
        ConceptMapping("RiskGrade.1", "risk_grade", "RiskGrade.1", ("1", "1등급", "매우높은위험(1등급)", "매우 높은 위험", "매우높은위험"), "RISK_GRADE_1"),
        ConceptMapping("RiskGrade.2", "risk_grade", "RiskGrade.2", ("2", "2등급", "높은위험(2등급)", "높은 위험", "높은위험"), "RISK_GRADE_2"),
        ConceptMapping("RiskGrade.3", "risk_grade", "RiskGrade.3", ("3", "3등급", "다소높은위험(3등급)", "다소 높은 위험", "다소높은위험"), "RISK_GRADE_3"),
        ConceptMapping("RiskGrade.4", "risk_grade", "RiskGrade.4", ("4", "4등급", "보통위험(4등급)", "보통 위험", "보통위험"), "RISK_GRADE_4"),
        ConceptMapping("RiskGrade.5", "risk_grade", "RiskGrade.5", ("5", "5등급", "낮은위험(5등급)", "낮은 위험", "낮은위험"), "RISK_GRADE_5"),
        ConceptMapping("RiskGrade.6", "risk_grade", "RiskGrade.6", ("6", "6등급", "매우낮은위험(6등급)", "매우 낮은 위험", "매우낮은위험"), "RISK_GRADE_6"),
        *(
            ConceptMapping(
                f"BondType.{raw}",
                "bond_type",
                f"BondType.{raw}",
                (raw,),
                resource,
            )
            for raw, resource in BOND_TYPE_RESOURCES.items()
        ),
        ConceptMapping("SaleLot", "classification", "SaleLot", ("판매 LOT", "판매 로트"), "SaleLot"),
        ConceptMapping("TradingChannel", "classification", "TradingChannel", ("거래 채널",), "TradingChannel"),
        ConceptMapping("InterestRateType", "classification", "InterestRateType", ("금리 유형",), "InterestRateType"),
        ConceptMapping("InterestPaymentType", "classification", "InterestPaymentType", ("이자 지급 유형",), "InterestPaymentType"),
        ConceptMapping("ShareClassFeeType", "classification", "ShareClassFeeType", ("클래스 보수 유형",), "ShareClassFeeType"),
    )


def _field_mappings() -> tuple[FieldMapping, ...]:
    active = SemanticCapabilityState.ACTIVE
    prospective = SemanticCapabilityState.PROSPECTIVE
    exact_ops = frozenset({"filter", "project"})
    project = frozenset({"project"})
    contract_sort = frozenset({"project", "sort_contract"})
    return (
        FieldMapping("product.name", "productName", ("상품명", "이름"), "rdb", "canonical_products.product_name", exact_ops),
        FieldMapping("product.short_name", "shortName", ("단축명", "약칭"), "rdb", "canonical_products.short_name", exact_ops),
        FieldMapping("product.ticker", "Identifier", ("티커", "ticker"), "rdb", "canonical_products.ticker", exact_ops),
        FieldMapping("product.isin", "Identifier", ("ISIN", "표준코드"), "rdb", "canonical_products.isin", exact_ops),
        FieldMapping("product.asset_manager", "managedBy", ("운용사",), "rdb+graph", "canonical_products.asset_manager/MANAGED_BY", exact_ops),
        FieldMapping("product.issuer", "issuedBy", ("발행사",), "rdb+graph", "canonical_products.issuer/ISSUED_BY", exact_ops),
        FieldMapping("product.product_type", "FinancialProduct", ("상품유형", "product_type"), "rdb", "canonical_products.product_type", exact_ops),
        FieldMapping("product.region", "hasExposureRegion", ("지역", "투자지역", "region"), "rdb+graph", "canonical_products.region/HAS_EXPOSURE_REGION", exact_ops),
        FieldMapping("product.asset_type", "hasAssetClass", ("자산유형", "자산군", "asset_type"), "rdb+graph", "canonical_products.asset_type/HAS_ASSET_CLASS", exact_ops),
        FieldMapping("product.risk_grade", "hasRiskGrade", ("위험등급",), "rdb+graph", "canonical_products.risk_grade/HAS_RISK_GRADE", exact_ops),
        FieldMapping("product.offering_type", "hasOfferingType", ("공모", "사모", "offering_type"), "rdb", "canonical_products.offering_type/HAS_OFFERING_TYPE", exact_ops),
        FieldMapping("product.currency", "denominatedIn", ("통화", "표시통화"), "rdb+graph", "canonical_products.currency/DENOMINATED_IN", exact_ops),
        # Projection is safe, but cross-source comparisons are not.  The new
        # generation mixes currencies and does not provide an FX normalization
        # contract, so filter/sort stay disabled in Team mode.
        FieldMapping("product.aum", "SizeMetric", ("순자산", "AUM", "운용규모"), "rdb", "canonical_v2.metric_observations", contract_sort, active, "row currency", "currency amount", "currency unit", "preserve_null", "reject_cross_currency_comparison"),
        FieldMapping("product.expense_ratio", "CostMetric", ("총보수", "보수율", "운용보수"), "rdb", "canonical_v2.metric_observations", project, active, "source scale unverified", None, None),
        FieldMapping("product.one_year_return", "PerformanceMetric", ("1년 수익률", "1년수익률", "연 수익률", "연수익률", "ONE_YEAR_RETURN"), "rdb", "canonical_v2.metric_observations.ONE_YEAR_RETURN", contract_sort, active, "source percent", "percent", "SOURCE_PERCENT", "preserve_null", "exclude_missing_from_rankable_set"),
        FieldMapping("product.nav", "NAVMetric", ("NAV", "기준가격"), "rdb", "canonical_products.nav", project, active, "row currency", None, None),
        FieldMapping("product.price", "PriceMetric", ("가격", "종가"), "rdb", "canonical_products.price", project, active, "row currency", None, None),
        FieldMapping("product.base_index", "hasUnderlyingIndex", ("기초지수", "추종지수"), "rdb+graph", "canonical_products.base_index/TRACKS_INDEX", exact_ops),
        FieldMapping("product.observed_at", "observedAt", ("관측일", "기준일"), "rdb", "canonical_products.observed_at", exact_ops),
        FieldMapping("product.strategy_description", "investmentStrategyDescription", ("전략", "투자전략"), "vector_bm25", "etf_attributes.strategy", project),
        FieldMapping("product.credit_rating", "hasCreditRating", ("신용등급", "credit_rating"), "rdb", "canonical_v2.metric_observations", frozenset({"filter", "project", "ordered_comparison"}), active, "ordinal", "ordinal", "CREDIT_RATING_V1"),
        FieldMapping("product.current_sale_available", "OperationalConstraint", ("현재 판매 가능", "current_sale_available", "구매 가능"), "rdb", "organizer bond lifecycle exclusion rule", frozenset({"filter"}), active, "organizer rule", "boolean", "ORGANIZER_RULE_V1"),
        FieldMapping("product.subscription_status", "subscriptionStatus", ("가입 상태", "추가매수 상태", "subscription_status"), "rdb", "canonical_v2.entity_classifications", exact_ops),
        FieldMapping("product.is_sold_by_mirae_asset", "isSoldByMiraeAsset", ("미래에셋 판매 대상", "당사판매여부"), "rdb", "canonical_v2.canonical_scalar_facts", frozenset({"filter"})),
        FieldMapping("product.current_fund_subscription_eligible", "OperationalConstraint", ("현재 미래에셋 가입 가능", "추가매수 가능", "미래에셋 판매 중"), "rdb", "derived public+open subscription+Mirae sale rule", frozenset({"filter"}), active, "derived organizer rule", "boolean", "FUND_SUBSCRIPTION_RULE_V1"),
        FieldMapping("product.latest_fund_price_available", "OperationalConstraint", ("최신 기준가 있음",), "rdb", "derived latest PRFD PRICE observation rule", frozenset({"filter"}), active, "derived freshness rule", "boolean", "FUND_PRICE_FRESHNESS_V1"),
        FieldMapping("product.listing_country", "listedInCountry", ("상장국가", "listing_country"), "rdb+graph", "LISTED_IN_COUNTRY", exact_ops),
        FieldMapping("product.maturity", "maturityOrFirstCallDate", ("만기", "만기일"), "rdb", "bond_attributes.maturity_date", project, prospective, "date", "date"),
        FieldMapping("product.return", "PerformanceMetric", ("수익률",), "rdb", "metric_observations.return", project, prospective, "percent", None),
        FieldMapping("product.yield", "YieldMetric", ("수익률", "채권수익률"), "rdb", "metric_observations.yield", project, prospective, "percent", None),
    )


def _relation_mappings() -> tuple[RelationMapping, ...]:
    active = SemanticCapabilityState.ACTIVE
    unsupported = SemanticCapabilityState.UNSUPPORTED_BY_CURRENT_SNAPSHOT
    holdings = (
        active
        if os.getenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "0") == "1"
        else unsupported
    )
    return (
        RelationMapping("managedBy", "managedBy", ("운용사", "운용하는", "관리하는"), "MANAGED_BY"),
        RelationMapping("issuedBy", "issuedBy", ("발행사", "발행한"), "ISSUED_BY"),
        RelationMapping("tracksIndex", "tracksIndex", ("추종하는", "따라가는"), "TRACKS_INDEX", active, ("tracks",)),
        RelationMapping("hasUnderlyingIndex", "hasUnderlyingIndex", ("기초지수", "추종지수"), "HAS_UNDERLYING_INDEX"),
        RelationMapping("hasShareClass", "hasShareClass", ("펀드 클래스", "판매 클래스"), "HAS_SHARE_CLASS", active, ("hasClass",)),
        RelationMapping("hasBenchmark", "hasBenchmark", ("벤치마크",), "HAS_BENCHMARK", active, ("referencesBenchmark",)),
        RelationMapping("denominatedIn", "denominatedIn", ("표시통화",), "DENOMINATED_IN"),
        RelationMapping("hasRiskGrade", "hasRiskGrade", ("위험등급",), "HAS_RISK_GRADE"),
        RelationMapping("hasAssetClass", "hasAssetClass", ("자산군", "자산유형"), "HAS_ASSET_CLASS", active, ("hasAssetType",)),
        RelationMapping("hasExposureRegion", "hasExposureRegion", ("투자지역", "노출지역"), "HAS_EXPOSURE_REGION", active, ("investsInRegion",)),
        RelationMapping("hasMarketScope", "hasMarketScope", ("시장범위", "국내외구분"), "HAS_MARKET_SCOPE"),
        RelationMapping("tradedInCurrency", "tradedInCurrency", ("거래통화",), "TRADED_IN_CURRENCY"),
        RelationMapping("listedOnExchange", "listedOnExchange", ("거래소",), "LISTED_ON_EXCHANGE"),
        RelationMapping("listedInMarket", "listedInMarket", ("상장시장",), "LISTED_IN_MARKET"),
        RelationMapping("listedInCountry", "listedInCountry", ("상장국가",), "LISTED_IN_COUNTRY"),
        RelationMapping("hasSaleLot", "hasSaleLot", ("판매 LOT",), "HAS_SALE_LOT"),
        RelationMapping("holds", "holds", ("보유", "보유한"), "HOLDS", holdings),
        RelationMapping("securityIssuedBy", "securityIssuedBy", ("증권 발행사",), "SECURITY_ISSUED_BY", holdings),
    )


def _metric_mappings() -> tuple[MetricFamilyMapping, ...]:
    active = SemanticCapabilityState.ACTIVE
    prospective = SemanticCapabilityState.PROSPECTIVE
    return (
        MetricFamilyMapping("PriceMetric", ("price.close", "price.reference"), "product.price", "canonical_products.price", active),
        MetricFamilyMapping("NAVMetric", ("nav.perShare", "nav.reference"), "product.nav", "canonical_products.nav", active),
        MetricFamilyMapping("SizeMetric", ("aum", "netAssets"), "product.aum", "canonical_products.aum", active),
        MetricFamilyMapping("CostMetric", ("fee", "expenseRate"), "product.expense_ratio", "canonical_products.expense_ratio", active),
        MetricFamilyMapping("PerformanceMetric", ("return.1Y",), "product.one_year_return", "canonical_v2.metric_observations.ONE_YEAR_RETURN", active),
        MetricFamilyMapping("PerformanceMetric", ("return.1D", "return.1M", "return.1Y"), "product.return", "metric_observations", prospective),
        MetricFamilyMapping("YieldMetric", ("buyYield", "afterTaxYield", "corporateYield"), "product.yield", "metric_observations", prospective),
        MetricFamilyMapping("ValuationMetric", ("evaluatedPrice", "bondClosePrice"), "product.price", "canonical_products.price", active),
        MetricFamilyMapping("SaleMetric", ("tradePrice", "buyableQuantity"), "product.price", "unavailable", SemanticCapabilityState.UNSUPPORTED_BY_CURRENT_SNAPSHOT),
    )


def _index_aliases(items, values, category):
    result = {}
    for item in items:
        for value in values(item):
            result[(category(item), normalize_ontology_text(value))] = item
    return result


def _flat_aliases(items: Iterable, values):
    result = {}
    for item in items:
        for value in values(item):
            result[normalize_ontology_text(value)] = item
    return result

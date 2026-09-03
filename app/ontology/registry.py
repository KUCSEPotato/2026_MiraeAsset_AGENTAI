from __future__ import annotations

from app.domain.models import CanonicalConcept, ConceptCategory
from app.ontology.index import normalize_ontology_text


class StaticSemanticRegistry:
    """Small deterministic normalization baseline used during offline ingestion."""

    _aliases = {
        ConceptCategory.PRODUCT_TYPE: {
            "etf": CanonicalConcept.FINANCIAL_PRODUCT_ETF,
            "상장지수펀드": CanonicalConcept.FINANCIAL_PRODUCT_ETF,
            "etn": CanonicalConcept.FINANCIAL_PRODUCT_ETN,
            "상장지수증권": CanonicalConcept.FINANCIAL_PRODUCT_ETN,
            "채권": CanonicalConcept.FINANCIAL_PRODUCT_BOND,
            "bond": CanonicalConcept.FINANCIAL_PRODUCT_BOND,
            "펀드": CanonicalConcept.FINANCIAL_PRODUCT_FUND,
            "공모펀드": CanonicalConcept.FINANCIAL_PRODUCT_PUBLIC_FUND,
        },
        ConceptCategory.REGION: {
            "국내": CanonicalConcept.REGION_KR, "한국": CanonicalConcept.REGION_KR,
            "korea": CanonicalConcept.REGION_KR, "unitedstatesofamerica": CanonicalConcept.REGION_US,
            "미국": CanonicalConcept.REGION_US, "usa": CanonicalConcept.REGION_US,
            "unitedstates": CanonicalConcept.REGION_US,
            "japan": CanonicalConcept.REGION_JP,
            "일본": CanonicalConcept.REGION_JP, "china": CanonicalConcept.REGION_CN,
            "중국": CanonicalConcept.REGION_CN, "global": CanonicalConcept.REGION_GLOBAL,
            "글로벌": CanonicalConcept.REGION_GLOBAL, "globalexus": CanonicalConcept.REGION_GLOBAL,
            "asia": CanonicalConcept.REGION_ASIA, "아시아": CanonicalConcept.REGION_ASIA,
        },
        ConceptCategory.ASSET_TYPE: {
            "equity": CanonicalConcept.ASSET_TYPE_EQUITY, "주식": CanonicalConcept.ASSET_TYPE_EQUITY,
            "주식형": CanonicalConcept.ASSET_TYPE_EQUITY, "bond": CanonicalConcept.ASSET_TYPE_BOND,
            "채권": CanonicalConcept.ASSET_TYPE_BOND, "채권형": CanonicalConcept.ASSET_TYPE_BOND,
            "commodity": CanonicalConcept.ASSET_TYPE_COMMODITY, "원자재": CanonicalConcept.ASSET_TYPE_COMMODITY,
            "mixedassets": CanonicalConcept.ASSET_TYPE_MIXED, "혼합자산": CanonicalConcept.ASSET_TYPE_MIXED,
            "mmf": CanonicalConcept.ASSET_TYPE_MONEY_MARKET, "moneymarket": CanonicalConcept.ASSET_TYPE_MONEY_MARKET,
            "currency": CanonicalConcept.ASSET_TYPE_CURRENCY, "통화": CanonicalConcept.ASSET_TYPE_CURRENCY,
            "realestate": CanonicalConcept.ASSET_TYPE_REAL_ESTATE, "부동산": CanonicalConcept.ASSET_TYPE_REAL_ESTATE,
            "alternatives": CanonicalConcept.ASSET_TYPE_ALTERNATIVE, "대체투자": CanonicalConcept.ASSET_TYPE_ALTERNATIVE,
            "other": CanonicalConcept.ASSET_TYPE_OTHER, "기타": CanonicalConcept.ASSET_TYPE_OTHER,
        },
    }
    _concept_aliases = _aliases
    _field_aliases = {
        "상품명": "product.name",
        "이름": "product.name",
        "순자산": "product.aum",
        "aum": "product.aum",
        "운용규모": "product.aum",
        "총보수": "product.expense_ratio",
        "보수율": "product.expense_ratio",
        "운용보수": "product.expense_ratio",
        "가격": "product.price",
        "종가": "product.price",
        "nav": "product.nav",
        "기준가격": "product.nav",
        "지역": "product.region",
        "투자지역": "product.region",
        "region": "product.region",
        "자산유형": "product.asset_type",
        "자산군": "product.asset_type",
        "assettype": "product.asset_type",
        "운용사": "product.asset_manager",
        "발행사": "product.issuer",
        "기초지수": "product.base_index",
        "추종지수": "product.base_index",
        "표시통화": "product.currency",
        "통화": "product.currency",
        "위험등급": "product.risk_grade",
        "etp_distribution_status": "product.etp_distribution_status",
        "etp_trading_status": "product.etp_trading_status",
        "current_etp_sale_eligible": "product.current_etp_sale_eligible",
        "latest_etp_price_available": "product.latest_etp_price_available",
        "etp_listing_ended": "product.etp_listing_ended",
        "stale_etp_price_warning": "product.stale_etp_price_warning",
        "etp_insufficient_info": "product.etp_insufficient_info",
        "etp판매상태": "product.etp_distribution_status",
        "etp거래상태": "product.etp_trading_status",
        "현재etp구매가능": "product.current_etp_sale_eligible",
        "최신etp가격있음": "product.latest_etp_price_available",
        "etp상장종료": "product.etp_listing_ended",
        "etp가격오래됨": "product.stale_etp_price_warning",
        "etp정보부족": "product.etp_insufficient_info",
    }

    def resolve_concept(
        self, value: str, category: ConceptCategory
    ) -> CanonicalConcept | None:
        return self._aliases.get(category, {}).get(normalize_ontology_text(value))

    def map_field(self, value: str) -> str | None:
        return self._field_aliases.get(normalize_ontology_text(value))


# Compatibility protocol name retained for the deterministic test service.
SemanticRegistry = StaticSemanticRegistry

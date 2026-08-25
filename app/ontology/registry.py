from __future__ import annotations

from app.domain.models import CanonicalConcept, ConceptCategory
from app.ontology.index import normalize_ontology_text


class StaticSemanticRegistry:
    """Small deterministic normalization baseline used during offline ingestion."""

    _aliases = {
        ConceptCategory.REGION: {
            "국내": CanonicalConcept.REGION_KR, "한국": CanonicalConcept.REGION_KR,
            "korea": CanonicalConcept.REGION_KR, "unitedstatesofamerica": CanonicalConcept.REGION_US,
            "미국": CanonicalConcept.REGION_US, "japan": CanonicalConcept.REGION_JP,
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

    def resolve_concept(
        self, value: str, category: ConceptCategory
    ) -> CanonicalConcept | None:
        return self._aliases.get(category, {}).get(normalize_ontology_text(value))

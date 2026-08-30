from __future__ import annotations

from app.ontology.index import FP, OntologyIndex


DEFAULT_SEMANTIC_VOCABULARY = {
    "product_types": ["ETF", "ETN", "채권", "펀드", "공모펀드"],
    "regions": ["국내", "한국", "미국", "일본", "중국", "글로벌", "아시아"],
    "asset_types": [
        "주식", "주식형", "Equity", "채권", "채권형", "Bond",
        "원자재", "Commodity", "혼합자산",
    ],
    "relations": [
        "managedBy", "issuedBy", "holds", "securityIssuedBy", "tracks", "referencesBenchmark",
        "hasClass", "denominatedIn", "hasRiskGrade",
        "운용사", "운용하는", "관리하는", "발행사", "발행한",
        "기초지수", "추종지수", "추종하는", "따라가는", "벤치마크",
        "펀드 클래스", "표시통화", "위험등급",
    ],
    "fields": [
        "region", "asset_type", "product_type", "aum", "expense_ratio",
        "product.name", "product.short_name", "product.ticker",
        "product.isin", "product.asset_manager", "product.issuer",
        "product.product_type", "product.region", "product.asset_type",
        "product.risk_grade", "product.currency", "product.aum",
        "product.expense_ratio", "product.nav", "product.price",
        "product.base_index", "product.observed_at",
    ],
}


def export_compact_semantic_vocabulary(index: OntologyIndex | None) -> dict[str, list[str]]:
    if index is None:
        return {"product_types": [], "regions": [], "asset_types": [], "relations": [], "fields": []}
    def vocabulary_values(*categories: str) -> set[str]:
        return {
            value
            for category in categories
            for term in index.terms(category)
            for value in (term.canonical_name, *term.aliases)
            if value
        }

    product_types = vocabulary_values("product_type")
    regions = vocabulary_values("region", "exposure_region")
    asset_types = vocabulary_values("asset_type", "asset_class")
    relations = vocabulary_values("relation")
    fields = {
        value
        for term in index.terms("field")
        for value in (term.canonical_field, term.canonical_name, *term.aliases)
        if value is not None
    }
    if index.runtime_mapping is not None:
        mapping = index.runtime_mapping
        product_types.update(
            value
            for item in mapping.concepts
            if item.category == "product_type"
            for value in (item.canonical_name, item.runtime_key, *item.aliases)
        )
        regions.update(
            value
            for item in mapping.concepts
            if item.category == "exposure_region"
            for value in (item.canonical_name, item.runtime_key, *item.aliases)
        )
        asset_types.update(
            value
            for item in mapping.concepts
            if item.category == "asset_class"
            for value in (item.canonical_name, item.runtime_key, *item.aliases)
        )
        relations.update(
            value
            for item in mapping.relations
            for value in (
                item.canonical_relation,
                item.ontology_resource,
                *item.aliases,
                *item.legacy_names,
            )
        )
        fields.update(
            value
            for item in mapping.fields
            for value in (item.canonical_field, *item.aliases)
        )
    fields.update({"region", "asset_type", "product_type", "aum", "expense_ratio"})
    return {
        "product_types": sorted(product_types),
        "regions": sorted({
            *regions,
        }),
        "asset_types": sorted({
            *asset_types,
        }),
        "relations": sorted(relations),
        "fields": sorted(
            fields
            | {str(value) for value in index.graph.objects(None, FP.canonicalField)}
        ),
    }

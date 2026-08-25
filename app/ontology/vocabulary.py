from __future__ import annotations

from app.ontology.index import FP, OntologyIndex


def export_compact_semantic_vocabulary(index: OntologyIndex | None) -> dict[str, list[str]]:
    if index is None:
        return {"product_types": [], "regions": [], "asset_types": [], "relations": [], "fields": []}
    return {
        "product_types": [term.canonical_name for term in index.terms("product_type")],
        "regions": [term.canonical_name for term in index.terms("region")],
        "asset_types": [term.canonical_name for term in index.terms("asset_type")],
        "relations": [term.canonical_name for term in index.terms("relation")],
        "fields": sorted({str(value) for value in index.graph.objects(None, FP.canonicalField)}),
    }

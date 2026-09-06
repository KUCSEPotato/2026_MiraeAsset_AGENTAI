"""Lossless surface/enum normalization before semantic validation.

Unknown names remain unknown. Offsets may be repaired only for an exact,
unique original substring; no fuzzy source-text rewriting is performed.
"""

from copy import deepcopy
import re

from app.ontology.index import normalize_ontology_text as norm
from app.ontology.runtime_mapping import TeamOntologyRuntimeMapping
from app.query.normalization import metric_spec


RELATION_SUBJECT_TYPES = {
    "AssetManager", "Bond", "Currency", "ETF", "ETN", "ExchangeTradedProduct",
    "FinancialProduct", "Fund", "Index", "Issuer", "RiskGrade",
    "Organization", "Security", "EquitySecurity", "FundShareClass",
}
RELATION_TARGET_TYPES = {
    "AssetManager", "Currency", "Index", "Issuer", "RiskGrade",
    "Organization", "Security", "EquitySecurity", "FundShareClass",
}

_ENUMS = {
    "direction": {
        "ascending": "asc", "asc": "asc", "low": "asc", "lower": "asc", "낮은": "asc",
        "descending": "desc", "desc": "desc", "high": "desc", "higher": "desc", "높은": "desc",
        "outgoing": "outgoing", "incoming": "incoming",
    },
    "operator": {
        "equals": "eq", "equal": "eq", "=": "eq", "eq": "eq",
        "not_equal": "ne", "!=": "ne", "ne": "ne",
        "greater_than": "gt", ">": "gt", "gt": "gt",
        "greater_than_or_equal": "gte", ">=": "gte", "gte": "gte",
        "less_than": "lt", "<": "lt", "lt": "lt",
        "less_than_or_equal": "lte", "<=": "lte", "lte": "lte",
        "contains": "contains", "in": "in", "between": "between",
    },
    "subject_type": {norm(v): v for v in RELATION_SUBJECT_TYPES},
    "target_type": {norm(v): v for v in RELATION_TARGET_TYPES},
}


def normalize_candidate_payload(question: str, payload: object) -> object:
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(item) for key, item in value.items()}
        if {"start", "end", "raw_text"}.issubset(result):
            raw = result["raw_text"]
            start, end = result["start"], result["end"]
            valid = (isinstance(start, int) and isinstance(end, int)
                     and 0 <= start < end <= len(question)
                     and question[start:end] == raw)
            if not valid and isinstance(raw, str) and raw and question.count(raw) == 1:
                result.update(start=question.index(raw), end=question.index(raw) + len(raw))
        for key, aliases in _ENUMS.items():
            raw = result.get(key)
            if isinstance(raw, str):
                token = norm(raw) if key.endswith("_type") else raw.strip().casefold().replace(" ", "_")
                result[key] = aliases.get(token, raw)
        return result
    return visit(deepcopy(payload))


class CandidateAliasNormalizer:
    def __init__(self):
        runtime = TeamOntologyRuntimeMapping()
        self.field_groups = [tuple((item.canonical_field, *item.aliases)) for item in runtime.fields]
        self.field_groups += [
            ("product.return", "return", "수익률"),
            ("product.one_year_return", "1 year return", "1Y return", "1년 수익률"),
        ]
        self.product_groups = [
            tuple((item.canonical_name, item.runtime_key, *item.aliases, *item.legacy_names))
            for item in runtime.concepts if item.category == "product_type"
        ]
        self.relation_groups = [
            tuple((item.canonical_relation, item.ontology_resource, item.ontology_uri,
                   *item.aliases, *item.legacy_names)) for item in runtime.relations
        ]

    def field_key(self, field: str, context: str = "") -> str:
        for group in self.field_groups:
            if norm(field) in {norm(item) for item in group}:
                field = group[0]
                break
        metric = metric_spec(field, context=context)
        return metric.canonical_field if metric and metric.canonical_field else norm(field)

    @staticmethod
    def _surface(value: str, raw: str, groups) -> str:
        # Prefer literal user vocabulary. Synonyms are accepted only within
        # the same registered semantic group, never across field meanings.
        if norm(value) in norm(raw):
            return value
        matches = []
        for group in groups:
            if norm(value) not in {norm(item) for item in group}:
                continue
            for alias in group:
                pattern = re.escape(alias).replace(r"\ ", r"\s*")
                matches.extend(match.group() for match in re.finditer(pattern, raw, re.IGNORECASE))
        return max(matches, key=len) if matches else value

    def _field_surface(self, value: str, raw: str) -> str:
        surface = self._surface(value, raw, self.field_groups)
        if norm(surface) in norm(raw):
            return surface
        # 1Y is the existing default for an unqualified return. An explicit
        # different period must never be swallowed by the generic substring.
        if self.field_key(value) == "product.one_year_return":
            match = re.search(r"수익률|return", raw, re.IGNORECASE)
            if match and not re.search(r"\d+\s*(?:일|개월|년|[DMY])|YTD|올해|오늘|연초", raw, re.IGNORECASE):
                return match.group()
        return surface

    def normalize(self, candidate):
        updates = {}
        for name in ["requested_fields", "group_by"]:
            updates[name] = [item.model_copy(update={"value": self._field_surface(item.value, item.source_span.raw_text)})
                             for item in getattr(candidate, name)]
        updates["sorts"] = [item.model_copy(update={"field": self._field_surface(item.field, item.source_span.raw_text)})
                            for item in candidate.sorts]
        updates["product_types"] = [item.model_copy(update={"value": self._surface(item.value, item.source_span.raw_text, self.product_groups)})
                                    for item in candidate.product_types]
        updates["relations"] = [item.model_copy(update={"raw_relation": self._surface(item.raw_relation, item.source_span.raw_text, self.relation_groups)})
                                for item in candidate.relations]
        return candidate.model_copy(update=updates)

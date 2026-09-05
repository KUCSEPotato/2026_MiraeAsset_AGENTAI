"""Deterministic metric/temporal meaning shared by rule and LLM parsing.

The existing ontology and capability validators still decide whether these
requests can run.  In particular, a return period observation is not a series.
"""

from __future__ import annotations

import re

from app.data.metric_capabilities import MetricCapabilityRegistry, PREF01_RETURN_CONTRACTS
from app.domain.models import (
    ComparisonSpec, ConstraintSemanticType, ConstraintStatus, MetricSpec,
    GroupBySpec, ParsedQuery, QueryIntent, SemanticConstraint, SemanticCoverageStatus,
    SourceSpan, TemporalSpec,
)


_PERIOD_PATTERNS = (
    ("YTD", r"YTD|올해|연초\s*이후"),
    ("1D", r"1D|1\s*일|오늘"),
    ("1M", r"1M|1\s*개월"),
    ("3M", r"3M|3\s*개월"),
    ("6M", r"6M|6\s*개월"),
    ("1Y", r"1Y|1\s*년|연\s*수익률"),
)
_METRIC_ALIASES = {
    "RETURN": ("수익률", "return"),
    "AUM": ("aum", "순자산", "운용규모"),
    "EXPENSE_RATIO": ("운용보수", "총보수", "보수율", "expense_ratio"),
    "NAV": ("nav", "기준가격"),
    "PRICE": ("가격", "종가", "price"),
}


def temporal_spec(text: str, *, default_return: bool = False) -> TemporalSpec | None:
    """Resolve period language without deciding which dataset may supply it."""
    periods = {
        period for period, pattern in _PERIOD_PATTERNS
        if re.search(rf"(?<![\dA-Za-z])(?:{pattern})(?![\dA-Za-z])", text, re.IGNORECASE)
    }
    if len(periods) != 1:
        if periods or not default_return or re.search(r"\d+\s*(?:일|개월|년|[DMY])", text, re.IGNORECASE):
            return None
        period, source = MetricCapabilityRegistry.return_default_period, "DEFAULT_POLICY"
    else:
        period, source = next(iter(periods)), "EXPLICIT_QUERY"
    operation = (
        "GROWTH_RATE" if re.search(r"증가율|감소율|변화율|성장률|growth\s*rate", text, re.IGNORECASE)
        else "CHANGE" if re.search(r"증가|감소|변화|늘었|늘어난|줄었|얼마나\s*(?:올랐|내렸)|change", text, re.IGNORECASE)
        else "PERIOD_VALUE"
    )
    return TemporalSpec(period=period, period_source=source, operation=operation)


def metric_spec(raw_field: str, *, context: str = "", constraint_id: str | None = None) -> MetricSpec | None:
    """Bind recognized metric aliases using the authoritative return contracts."""
    folded = raw_field.strip().casefold()
    field_contract = PREF01_RETURN_CONTRACTS.get(raw_field)
    metric = "RETURN" if field_contract else next(
        (key for key, aliases in _METRIC_ALIASES.items()
         if folded in aliases or folded == f"product.{key.casefold()}"
         or (key == "RETURN" and re.fullmatch(r"(?:(?:최근\s*)?(?:\d+\s*(?:일|개월|년|[dmy])|ytd|오늘|올해|연초\s*이후|연)\s*)?수익률", folded))), None,
    )
    if metric is None:
        return None
    change = bool(re.search(r"증가|감소|변화|늘었|늘어난|줄었|growth|change", context, re.IGNORECASE))
    temporal_text = context if change else raw_field
    if not change and context and not field_contract:
        adjacent_period = re.search(
            rf"(?:최근\s*)?(?:\d+\s*(?:일|개월|년|[DMY])|YTD|오늘|올해|연초\s*이후)\s*{re.escape(raw_field)}",
            context, re.IGNORECASE,
        )
        if adjacent_period:
            temporal_text = adjacent_period.group()
    temporal = temporal_spec(temporal_text, default_return=metric == "RETURN")
    if field_contract and not change:
        temporal = TemporalSpec(period=field_contract.exact_period, period_source="EXPLICIT_QUERY")
    if metric == "RETURN":
        canonical = next(
            (field for field, contract in PREF01_RETURN_CONTRACTS.items()
             if temporal is not None and contract.exact_period == temporal.period), None,
        )
    else:
        canonical = {
            "AUM": "product.aum", "EXPENSE_RATIO": "product.expense_ratio",
            "NAV": "product.nav", "PRICE": "product.price",
        }[metric]
    return MetricSpec(metric=metric, temporal=temporal, canonical_field=canonical, constraint_id=constraint_id)


def normalize_query_semantics(parsed: ParsedQuery) -> ParsedQuery:
    """Add reusable semantics without replacing legacy grounded-field inputs."""
    metrics = list(parsed.metrics)
    requested_ids = {
        str(item.payload.get("field")): item.constraint_id
        for item in parsed.semantic_constraints
        if item.semantic_type is ConstraintSemanticType.REQUESTED_FIELD
    }
    for raw, constraint_id in (
        *((item.field, item.constraint_id) for item in parsed.sort),
        *((item, requested_ids.get(item)) for item in parsed.requested_fields),
        *((item.field, item.constraint_id) for item in parsed.filters),
    ):
        spec = metric_spec(raw, context=parsed.original_question, constraint_id=constraint_id)
        if spec is not None and spec not in metrics:
            metrics.append(spec)
    comparison = parsed.comparison
    if parsed.intent is QueryIntent.COMPARE_PRODUCTS and parsed.requested_fields:
        intent_id = next((item.constraint_id for item in parsed.semantic_constraints
                          if item.semantic_type is ConstraintSemanticType.INTENT), None)
        comparison = comparison or ComparisonSpec(fields=list(parsed.requested_fields), constraint_id=intent_id)
    constraints = list(parsed.semantic_constraints)
    group_by = parsed.group_by
    grouping = re.search(r"(운용사|지역|자산유형|상품유형|통화|신용등급)\s*별", parsed.original_question)
    if grouping and group_by is None:
        number = len(constraints) + 1
        existing = {item.constraint_id for item in constraints}
        while f"C{number}" in existing:
            number += 1
        group_by = GroupBySpec(fields=[grouping.group(1)], constraint_id=f"C{number}")
        constraints.append(SemanticConstraint(
            constraint_id=f"C{number}", source_span=SourceSpan(start=grouping.start(), end=grouping.end()),
            raw_text=grouping.group(), semantic_type=ConstraintSemanticType.GROUP_BY,
            status=ConstraintStatus.UNSUPPORTED, unsupported_reason="group_by_execution_unsupported",
            payload={"fields": group_by.fields},
        ))
    historical = re.search(r"증가율|감소율|변화율|성장률|증가|감소|변화|늘었|늘어난|줄었|growth|change", parsed.original_question, re.IGNORECASE)
    if historical and (metrics or parsed.temporal_constraint) and not any(
        item.unsupported_reason == "historical_metric_series_unavailable" for item in constraints
    ):
        existing = {item.constraint_id for item in constraints}
        number = len(existing) + 1
        while f"C{number}" in existing:
            number += 1
        constraints.append(SemanticConstraint(
            constraint_id=f"C{number}", source_span=SourceSpan(start=historical.start(), end=historical.end()),
            raw_text=historical.group(), semantic_type=ConstraintSemanticType.TEMPORAL,
            status=ConstraintStatus.UNSUPPORTED, unsupported_reason="historical_metric_series_unavailable",
            payload={"requires_historical_series": True},
        ))
    unsupported = list(dict.fromkeys([
        *parsed.unsupported_constraint_ids,
        *(item.constraint_id for item in constraints if item.status is ConstraintStatus.UNSUPPORTED),
    ]))
    return parsed.model_copy(update={
        "metrics": metrics, "comparison": comparison, "group_by": group_by, "semantic_constraints": constraints,
        "unsupported_constraint_ids": unsupported,
        "semantic_coverage": SemanticCoverageStatus.INCOMPLETE if unsupported else parsed.semantic_coverage,
    })

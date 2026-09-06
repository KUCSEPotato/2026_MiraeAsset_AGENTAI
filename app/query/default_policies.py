"""Reviewed missing-parameter defaults shared by rules and LLM candidates.

Only the application applies these values. No default changes a product
population, invents an observation, or grants execution capability.
"""

import re

from app.data.metric_capabilities import MetricCapabilityRegistry
from app.domain.models import (
    AppliedDefaultPolicy, ConstraintSemanticType, ParsedQuery, QueryIntent,
    ResultLimit, SemanticConstraint,
)


DEFAULT_POLICIES = {
    "RETURN.default_period": MetricCapabilityRegistry.return_default_period,
    "TOPK.default_k": 5,
    "RETURN.positive_direction": "DESC",
    "RETURN.negative_direction": "ASC",
}


def apply_default_policies(parsed: ParsedQuery) -> ParsedQuery:
    policies = list(parsed.parse_provenance.default_policies)

    def record(policy_id, constraint_id, disclosure):
        item = AppliedDefaultPolicy(
            policy_id=policy_id, inferred_value=DEFAULT_POLICIES[policy_id],
            constraint_id=constraint_id, disclosure=disclosure,
        )
        if item not in policies:
            policies.append(item)

    for metric in parsed.metrics:
        if (metric.metric == "RETURN" and metric.temporal
                and metric.temporal.period_source == "DEFAULT_POLICY"):
            record("RETURN.default_period", metric.constraint_id,
                   "기간을 지정하지 않아 수익률은 1년 기준으로 해석했습니다.")
        if metric.metric != "RETURN":
            continue
        constraint = next((item for item in parsed.semantic_constraints
                           if item.constraint_id == metric.constraint_id
                           and item.semantic_type is ConstraintSemanticType.SORT), None)
        if constraint and re.search(r"좋은|좋고", constraint.raw_text):
            record("RETURN.positive_direction", metric.constraint_id,
                   "‘수익률이 좋다’는 수익률이 높은 순서로 해석했습니다.")
        elif constraint and re.search(r"나쁜|나쁘고", constraint.raw_text):
            record("RETURN.negative_direction", metric.constraint_id,
                   "‘수익률이 나쁘다’는 수익률이 낮은 순서로 해석했습니다.")

    limit = parsed.result_limit
    constraints = list(parsed.semantic_constraints)
    if (limit is None and parsed.sort
            and parsed.intent is QueryIntent.SEARCH_PRODUCT
            and not parsed.aggregation and not parsed.selectors
            and not re.search(r"모든|전체|전부|모두", parsed.original_question)):
        anchor = next((c for c in constraints
                       if c.constraint_id == parsed.sort[0].constraint_id
                       and c.semantic_type is ConstraintSemanticType.SORT), None)
        if anchor is not None:
            used = {item.constraint_id for item in constraints}
            number = len(used) + 1
            while f"C{number}" in used:
                number += 1
            identifier = f"C{number}"
            limit = ResultLimit(value=DEFAULT_POLICIES["TOPK.default_k"],
                                raw_text=anchor.raw_text, constraint_id=identifier)
            constraints.append(SemanticConstraint(
                constraint_id=identifier, source_span=anchor.source_span,
                raw_text=anchor.raw_text, semantic_type=ConstraintSemanticType.LIMIT,
                payload={"value": limit.value, "source": "DEFAULT_POLICY",
                         "policy_id": "TOPK.default_k"},
            ))
            record("TOPK.default_k", identifier,
                   "개수를 지정하지 않아 정렬 결과는 최대 5개로 제한했습니다.")
    return parsed.model_copy(update={
        "result_limit": limit, "semantic_constraints": constraints,
        "parse_provenance": parsed.parse_provenance.model_copy(update={"default_policies": policies}),
    })


def disclose_defaults(answer: str, policies: list[AppliedDefaultPolicy]) -> str:
    disclosures = list(dict.fromkeys(item.disclosure for item in policies))
    return answer + ("\n\n" + " ".join(disclosures) if disclosures else "")

from app.domain.models import AnswerabilityStatus, ClauseStatus, EvidenceBundle, ValidationResult
from app.evidence.display import bundle_entity_labels, format_evidence_value


VALUE_ONLY_FIELDS = frozenset({"product.risk_grade"})
_SCOPE_REASON_LABELS = {
    "ONE_YEAR_RETURN_UNAVAILABLE_OUTSIDE_READY_FOREIGN_ETF_SCOPE": (
        "검증된 해외 ETF 범위 밖에는 1년 수익률 근거가 부족함"
    ),
    "COMPARISON_GRAIN_NOT_COMPATIBLE": (
        "공모펀드 수익률은 FundShareClass 단위이며 Fund 단위로 승격할 수 없음"
    ),
    "CROSS_GROUP_RETURN_BASIS_NOT_COMPARABLE": (
        "그룹 간 수익률 기준과 관측 단위의 통합 비교가 검증되지 않음"
    ),
}
_SCOPE_LABELS = {
    "ForeignETF outside READY iShares scope": (
        "검증된 READY iShares 범위 밖의 해외 ETF"
    ),
    "PublicFund family-level ranking": "Fund 단위 공모펀드 순위",
}


def answer_contract(evidence: EvidenceBundle) -> dict:
    """Field policies are application authority, never model/source metadata."""
    fields = sorted({item.field for item in evidence.evidence} & VALUE_ONLY_FIELDS)
    return {
        "value_only_fields": fields,
        "risk_grade_interpretation_allowed": False,
        "risk_grade_ordering_allowed": False,
        "risk_grade_scale_inference_allowed": False,
        "risk_grade_comparison_allowed": False,
    }


def satisfies_answer_contract(candidate: str, reference: str, evidence: EvidenceBundle) -> bool:
    # An unrestricted paraphrase cannot prove absence of an ordinal inference.
    # For audited value-only facts admit only the evidence-derived rendering;
    # do not try to enumerate every Korean/English hallucination with a regex.
    if answer_contract(evidence)["value_only_fields"] and candidate != reference:
        return False
    labels = bundle_entity_labels(evidence)
    return not any(
        entity_id in candidate and label != entity_id
        for entity_id, label in labels.items()
    )

_FIELD_LABELS = {
    "product.name": "상품명",
    "product.aum": "순자산",
    "product.expense_ratio": "총보수",
    "product.one_day_return": "1일 수익률",
    "product.one_month_return": "1개월 수익률",
    "product.three_month_return": "3개월 수익률",
    "product.six_month_return": "6개월 수익률",
    "product.one_year_return": "1년 수익률",
    "product.year_to_date_return": "연초 이후 수익률",
    "product.risk_grade": "위험등급",
    "product.region": "지역",
    "product.asset_type": "자산유형",
    "product.product_type": "상품유형",
    "product.market_scope": "시장범위",
    "product.bond_type": "채권유형",
    "product.offering_type": "공모·사모 구분",
    "product.listing_country": "상장국가",
    "product.ticker": "티커",
    "product.isin": "ISIN",
    "product.nav": "NAV",
    "product.price": "가격",
    "product.base_index": "기초지수",
    "product.strategy_description": "투자전략",
}


class DeterministicEvidenceAnswerGenerator:
    """Render a compact non-LLM answer from validated canonical evidence."""

    async def generate(
        self,
        question: str,
        evidence: EvidenceBundle,
        validation: ValidationResult,
    ) -> str:
        if validation.answerability is AnswerabilityStatus.PARTIALLY_ANSWERABLE:
            return render_partial_answer(evidence, validation)
        del question, validation
        lines: list[str] = []
        disclosures: list[str] = []
        entity_labels = bundle_entity_labels(evidence)
        for item in evidence.evidence:
            value_only = item.field in VALUE_ONLY_FIELDS
            for contract in ([] if value_only else item.metadata.get("comparison_contracts", [])):
                if not isinstance(contract, dict):
                    continue
                resolution = contract.get("metric_resolution")
                disclosure = (
                    resolution.get("disclosure")
                    if isinstance(resolution, dict)
                    else contract.get("answer_disclosure")
                )
                if isinstance(disclosure, str) and disclosure not in disclosures:
                    disclosures.append(disclosure)
            field = _FIELD_LABELS.get(item.field or "", item.field or "근거")
            value = item.value if item.value is not None else item.text
            if value is None:
                continue
            value = format_evidence_value(item.field, value)
            if (
                not value_only and item.metadata.get("metric_unit") == "PERCENT"
                and item.value is not None
                and not value.rstrip().endswith("%")
            ):
                value = f"{value}%"
            product = (
                entity_labels.get(item.entity_id, item.entity_id)
                if item.entity_id
                else None
            ) or "상품"
            lines.append(f"{product} — {field}: {value}")
        if not lines:
            return (
                "검증된 근거에서 표시할 수 있는 값을 "
                "찾지 못했습니다."
            )
        if disclosures:
            disclosure_text = " / ".join(disclosures)
            basis = (
                disclosure_text + " "
                if disclosure_text.endswith((".", "!", "?"))
                else disclosure_text + "으로 비교했습니다. "
            )
        else:
            basis = "확인된 데이터 기준 결과입니다. "
        return basis + " / ".join(lines)


def render_partial_answer(evidence: EvidenceBundle, validation: ValidationResult) -> str:
    """Only validated entity×field cells may contribute factual output."""
    scopes = _comparison_scopes(evidence)
    if scopes:
        return _render_scoped_ranking(evidence, scopes)
    lines = ["확인 가능한 정보는 다음과 같습니다. (일부 항목 확인 불가)"]
    for clause in validation.clauses:
        label = _FIELD_LABELS.get(clause.field, clause.label)
        prefix = f"{clause.entity_label} — " if clause.entity_label else ""
        if clause.kind == "COMPARISON":
            if clause.status is not ClauseStatus.SATISFIED:
                lines.append("상품 간 비교는 완료하지 못했으며 우열을 판단할 수 없습니다.")
            continue
        if clause.status is ClauseStatus.SATISFIED:
            values = []
            for index in clause.evidence_indices:
                item = evidence.evidence[index]
                if item.field != clause.field or item.entity_id != clause.entity_id or item.value is None:
                    raise ValueError("partial answer evidence does not match its clause")
                value = format_evidence_value(item.field, item.value)
                if item.field not in VALUE_ONLY_FIELDS and item.metadata.get("metric_unit") == "PERCENT":
                    value = value if value.rstrip().endswith("%") else value + "%"
                if value not in values:
                    values.append(value)
            if values:
                lines.append(f"- {prefix}{label}: {', '.join(values)}")
        else:
            unavailable = ("하나로 특정할 수 없음" if clause.status is ClauseStatus.AMBIGUOUS
                           else "현재 지원되는 근거로 확인할 수 없음" if clause.status is ClauseStatus.UNSUPPORTED
                           else "현재 데이터에서 확인할 수 없음")
            lines.append(f"- {prefix}{label}: {unavailable}")
    return "\n".join(lines)


def _comparison_scopes(evidence: EvidenceBundle) -> list[dict]:
    if evidence.execution_result is None:
        return []
    return [
        scope
        for result in evidence.execution_result.step_results.values()
        if isinstance(
            scope := result.retrieval_metadata.get("comparison_scope"), dict
        )
    ]


def _render_scoped_ranking(evidence: EvidenceBundle, scopes: list[dict]) -> str:
    labels = bundle_entity_labels(evidence)
    lines = [
        "현재 데이터에서 각 그룹 내부의 동일한 기준으로 비교 가능한 상품을 "
        "검증된 비교 그룹별로 정리했습니다."
    ]
    for scope in scopes:
        group_id = scope.get("group_id")
        metric_field = scope.get("metric_field")
        lines.append(f"[{scope.get('label', group_id or '비교 그룹')}]")
        seen: set[str] = set()
        rank = 0
        for item in evidence.evidence:
            item_scope = item.metadata.get("comparison_scope")
            if (
                not isinstance(item_scope, dict)
                or item_scope.get("group_id") != group_id
                or item.field != metric_field
                or item.entity_id is None
                or item.entity_id in seen
                or item.value is None
            ):
                continue
            seen.add(item.entity_id)
            rank += 1
            value = format_evidence_value(item.field, item.value)
            if (
                item.metadata.get("metric_unit") == "PERCENT"
                and not value.rstrip().endswith("%")
            ):
                value += "%"
            label = labels.get(item.entity_id, item.entity_id)
            field_label = _FIELD_LABELS.get(item.field or "", item.field or "근거")
            lines.append(f"{rank}. {label} — {field_label}: {value}")
        if rank == 0:
            lines.append("- 비교 계약을 만족하는 근거가 현재 데이터에 없습니다.")

        excluded = scope.get("excluded_scope", [])
        reasons = scope.get("exclusion_reasons", [])
        missing = scope.get("missing_metric_count", 0)
        disclosures = []
        if excluded:
            disclosures.append("제외 범위: " + ", ".join(
                _SCOPE_LABELS.get(str(value), str(value)) for value in excluded
            ))
        if reasons:
            disclosures.append("사유: " + ", ".join(
                _SCOPE_REASON_LABELS.get(str(reason), str(reason))
                for reason in reasons
            ))
        if isinstance(missing, int) and missing:
            disclosures.append(f"해당 지표 결측 {missing}건")
        if disclosures:
            lines.append("※ " + " / ".join(disclosures))
    lines.append(
        "그룹 간 수익률 기준·관측 단위가 서로 검증되지 않은 경우 "
        "하나의 통합 순위로 섞지 않았습니다."
    )
    return "\n".join(lines)

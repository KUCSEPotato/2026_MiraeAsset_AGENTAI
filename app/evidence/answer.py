from app.domain.models import AnswerabilityStatus, ClauseStatus, EvidenceBundle, ValidationResult


VALUE_ONLY_FIELDS = frozenset({"product.risk_grade"})


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
    return not answer_contract(evidence)["value_only_fields"] or candidate == reference

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
            if (
                not value_only and item.metadata.get("metric_unit") == "PERCENT"
                and item.value is not None
                and not value.rstrip().endswith("%")
            ):
                value = f"{value}%"
            product = (item.entity_id or "상품") if value_only else (
                item.metadata.get("product_name")
                if item.field == "product.strategy_description"
                else item.text
            ) or item.entity_id or "상품"
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
                value = item.value
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

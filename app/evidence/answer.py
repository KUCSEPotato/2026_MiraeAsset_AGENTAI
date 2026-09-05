from app.domain.models import EvidenceBundle, ValidationResult


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

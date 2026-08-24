from app.domain.models import EvidenceBundle, ValidationResult


_FIELD_LABELS = {
    "product.name": "상품명",
    "product.aum": "순자산",
    "product.expense_ratio": "총보수",
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
        for item in evidence.evidence[:10]:
            field = _FIELD_LABELS.get(item.field or "", item.field or "근거")
            value = item.value if item.value is not None else item.text
            if value is None:
                continue
            product = (
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
        return "확인된 데이터 기준 결과입니다. " + " / ".join(lines)

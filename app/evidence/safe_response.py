from app.domain.models import AnswerabilityReasonCode, ValidationResult


_FIELD_LABELS = {
    "product.aum": "순자산",
    "product.expense_ratio": "총보수",
    "product.base_index": "기초지수",
    "product.maturity_date": "만기일",
}


class ReasonAwareSafeResponseGenerator:
    """Render deterministic, user-safe messages without backend details."""

    async def generate(self, validation: ValidationResult) -> str:
        codes = set(validation.reason_codes)
        if AnswerabilityReasonCode.SEMANTIC_PARSE_FAILED in codes:
            return (
                "질의 해석 서비스를 완료하지 못해 안전하게 검색을 진행할 수 "
                "없습니다. 잠시 후 다시 시도해 주세요."
            )
        if AnswerabilityReasonCode.ENTITY_RESOLUTION_FAILED in codes:
            return (
                "상품·기관 식별 서비스를 완료하지 못해 안전하게 검색을 진행할 "
                "수 없습니다. 잠시 후 다시 시도해 주세요."
            )
        if AnswerabilityReasonCode.ENTITY_PARSE_FAILED in codes:
            return "질문에서 조회할 상품 또는 기관 이름을 식별하지 못했습니다."
        if (
            AnswerabilityReasonCode.UNSUPPORTED_QUERY_SEMANTICS in codes
            or AnswerabilityReasonCode.UNSUPPORTED_CONSTRAINT in codes
        ):
            return (
                "현재 제공된 조건을 모두 정확하게 해석하여 조회하기 "
                "어렵습니다. 조건을 조금 더 구체적으로 지정해 주세요."
            )
        if AnswerabilityReasonCode.RANKING_NOT_APPLIED in codes:
            return "요청하신 정렬 기준을 신뢰성 있게 적용할 수 없습니다."
        if AnswerabilityReasonCode.RETRIEVAL_TIMED_OUT in codes:
            return (
                "필요한 검색을 제한 시간 내 완료하지 못해 해당 정보를 "
                "확인할 수 없습니다."
            )
        if (
            AnswerabilityReasonCode.RETRIEVAL_FAILED in codes
            or AnswerabilityReasonCode.DEPENDENCY_INCOMPLETE in codes
        ):
            return "필요한 검색을 완료하지 못해 해당 정보를 확인할 수 없습니다."
        if AnswerabilityReasonCode.INSUFFICIENT_COVERAGE in codes:
            return (
                "해당 항목은 일부 상품에 대해서만 제공되어 전체 상품을 "
                "기준으로 비교할 수 없습니다."
            )
        if AnswerabilityReasonCode.CONFLICTING_EVIDENCE in codes:
            return (
                "확인된 근거 간 값이 일치하지 않아 신뢰할 수 있는 답변을 "
                "제공하기 어렵습니다."
            )
        if AnswerabilityReasonCode.ENTITY_MISMATCH in codes:
            return (
                "확인된 근거의 상품이 요청하신 상품과 일치하지 않아 답변을 "
                "제공할 수 없습니다."
            )
        if AnswerabilityReasonCode.SNAPSHOT_MISMATCH in codes:
            return (
                "서로 다른 기준 시점의 근거를 동일 조건으로 비교할 수 없어 "
                "답변을 제공할 수 없습니다."
            )
        if AnswerabilityReasonCode.AMBIGUOUS_ENTITY in codes:
            return "요청하신 상품을 하나로 특정할 수 없어 답변할 수 없습니다."
        if (
            AnswerabilityReasonCode.ENTITY_NOT_FOUND in codes
            or AnswerabilityReasonCode.ENTITY_UNRESOLVED in codes
        ):
            return "요청하신 상품 또는 기관을 현재 데이터에서 찾을 수 없습니다."
        if AnswerabilityReasonCode.ZERO_MATCH in codes:
            return "제공된 조건을 모두 만족하는 결과는 없습니다."
        if AnswerabilityReasonCode.INSUFFICIENT_EVIDENCE in codes:
            return "조회 결과가 있으나 답변에 필요한 근거가 충분하지 않습니다."
        if (
            AnswerabilityReasonCode.MISSING_REQUIRED_FIELD in codes
            or AnswerabilityReasonCode.INVALID_SENTINEL in codes
        ):
            labels = [
                _FIELD_LABELS.get(field, field)
                for field in validation.missing_fields
            ]
            if labels:
                return (
                    f"요청하신 {', '.join(labels)} 정보는 제공된 근거에서 "
                    "확인할 수 없습니다."
                )
        return "제공된 데이터에서 해당 정보를 확인할 수 없습니다."

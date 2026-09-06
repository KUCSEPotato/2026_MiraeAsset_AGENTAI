import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.ontology.runtime_mapping import BOND_TYPE_RESOURCES
from app.query.normalization import MATURITY_YEAR_PATTERNS, normalize_query_semantics

from app.domain.models import (
    AggregationOperator,
    AggregationSpec,
    BooleanExpression,
    BooleanNodeType,
    ConstraintSemanticType,
    ConstraintStatus,
    EntityMention,
    FilterOperator,
    FilterSpec,
    ParsedQuery,
    PeerSelector,
    ProductUniverseUnion,
    QueryIntent,
    RelationDirection,
    RelationMention,
    ResultLimit,
    ScalarUnit,
    SemanticConstraint,
    SemanticCoverageStatus,
    SortSpec,
    SourceSpan,
    TemporalConstraint,
    TypedScalarValue,
    UnparsedMaterialSpan,
)


_BOND_LIFECYCLE_EXCLUSION_PATTERN = re.compile(
    r"(?:상장\s*폐지|리스팅\s*종료)"
    r"(?:\s*또는\s*(?:상장\s*폐지|리스팅\s*종료))?"
    r"\s*채권(?:을|은|는)?\s*제외",
    re.IGNORECASE,
)

# Query-only lexical aliases normalize to values already declared by the Team
# Ontology runtime registry. They do not add ontology individuals or runtime
# capabilities. ``회사채`` is the ordinary-language shorthand for the
# authoritative ``일반회사채`` classification.
_BOND_TYPE_QUERY_ALIASES = {
    **{value: value for value in BOND_TYPE_RESOURCES},
    "회사채": "일반회사채",
}
_BOND_TYPE_QUERY_ALIASES_NORMALIZED = {
    value.casefold(): canonical
    for value, canonical in _BOND_TYPE_QUERY_ALIASES.items()
}
_TRADING_CURRENCY_QUERY_ALIASES = {
    "usd": "USD",
    "달러": "USD",
    "미국달러": "USD",
    "달러화": "USD",
    "미달러": "USD",
}
_OFFERING_TYPE_QUERY_ALIASES = {
    "공모": "공모",
    "사모": "사모",
    "public": "Public",
    "private": "Private",
}
_SUBSCRIPTION_STATUS_QUERY_ALIASES = {
    "판매중": "SubscriptionStatus.OPEN_FOR_SUBSCRIPTION",
    "판매완료": "SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION",
    "가입 종료": "SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION",
    "추가매수 종료": "SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION",
}
_SUBSCRIPTION_STATUS_QUERY_ALIASES_NORMALIZED = {
    re.sub(r"\s+", "", value).casefold(): canonical
    for value, canonical in _SUBSCRIPTION_STATUS_QUERY_ALIASES.items()
}
_FUND_ELIGIBILITY_QUERY_ALIASES = ("가입 가능", "추가매수 가능")
_ASSET_TYPE_QUERY_ALIASES = {
    "혼합형": "혼합자산",
}


def _normalize_asset_type_query_value(value: str) -> str:
    return _ASSET_TYPE_QUERY_ALIASES.get(value.casefold(), value)


def _lexical_alternation(values: Iterable[str]) -> str:
    return "|".join(
        re.escape(value).replace(r"\ ", r"\s*")
        for value in sorted(values, key=len, reverse=True)
    )


_TRADING_CURRENCY_VALUE_PATTERN = _lexical_alternation(
    ("미국 달러", "미 달러", "달러화", "달러", "USD")
)
_TRADING_CURRENCY_PATTERN = re.compile(
    rf"거래통화(?:가|는|은)?\s*(?P<field_value>{_TRADING_CURRENCY_VALUE_PATTERN})(?:인)?"
    rf"|(?P<traded_value>{_TRADING_CURRENCY_VALUE_PATTERN})(?:으)?로\s*거래되(?:는|어|고|며|지)?"
    rf"|(?P<label_value>{_TRADING_CURRENCY_VALUE_PATTERN})\s*거래통화(?:인|의)?",
    re.IGNORECASE,
)
_SUBSCRIPTION_STATUS_FUND_PATTERN = re.compile(
    rf"(?P<status>{_lexical_alternation(_SUBSCRIPTION_STATUS_QUERY_ALIASES)})"
    r"\s*펀드",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Draft:
    start: int
    end: int
    raw_text: str
    semantic_type: ConstraintSemanticType
    payload: dict[str, Any]
    ref: tuple[str, int] | None = None
    status: ConstraintStatus = ConstraintStatus.PARSED
    unsupported_reason: str | None = None


class RuleBasedQueryAnalyzer:
    """Conservative deterministic parser with fail-closed coverage tracking."""

    _product_type_aliases = (
        "상장지수펀드", "상장지수증권", "공모 펀드", "공모펀드", "ETF", "ETN",
        "Fund", "Bond", "펀드", "채권",
    )
    _region_aliases = (
        "United States of America", "United States", "글로벌", "국내", "한국",
        "미국", "일본", "중국", "아시아", "인도", "USA", "Korea", "Japan",
        "China", "Global", "Asia", "India",
    )
    _asset_type_aliases = (
        "Commodity", "Mixed Assets", "Money Market", "Real Estate", "Alternatives",
        "주식형", "채권형", "혼합자산", "채권혼합", "주식혼합", "단기자금",
        "혼합형", "대체투자", "대체자산", "부동산", "원자재", "Equity", "Currency",
        "Other", "MMF", "주식", "Bond", "채권", "통화", "기타",
    )
    _bond_type_aliases = tuple(_BOND_TYPE_QUERY_ALIASES)
    _field_aliases = (
        "연초 이후 수익률", "연초이후수익률", "올해 수익률", "올해수익률",
        "YTD 수익률", "YTD수익률",
        "6개월 수익률", "6개월수익률", "6M 수익률", "6M수익률",
        "3개월 수익률", "3개월수익률", "3M 수익률", "3M수익률",
        "1개월 수익률", "1개월수익률", "1M 수익률", "1M수익률",
        "1년 수익률", "1년수익률", "1Y 수익률", "1Y수익률",
        "연 수익률", "연수익률",
        "오늘 수익률", "오늘수익률", "1일 수익률", "1일수익률",
        "1D 수익률", "1D수익률",
        "상품판매여부", "추가매수 상태", "거래정지 상태", "가입 상태",
        "상품명", "이름", "단축명", "약칭", "표준코드", "지역", "region",
        "운용규모", "운용 규모", "순자산", "AUM", "운용보수", "총보수", "보수율",
        "위험 정보", "위험정보", "위험등급", "위험도", "리스크", "위험",
        "기준가격", "NAV", "가격", "종가", "티커", "관측일", "기준일",
        "ticker", "ISIN", "신용등급", "표면이자율", "표면금리", "쿠폰금리",
        "편입 비중", "보유 비중",
        "상품유형", "상품종류", "자산유형", "자산군", "투자지역", "노출지역",
        "상장국가", "거래통화", "통화", "시장범위", "국내외구분",
        "채권유형", "채권종류", "만기일", "만기",
        "공모", "사모",
    )
    _ranking_field_aliases = (*_field_aliases, "수익률", "위험", "만기일", "만기")
    _semantic_markers = (
        "관련", "전략", "친환경", "폭넓게", "테마", "산업", "혁신형",
        "구조", "위험요인", "특징", "동향",
        "covered call",
    )
    _descending_words = {"큰", "높은", "많은"}
    _ascending_words = {"낮은", "작은", "적은"}
    _non_material_tokens = {
        "에", "에는", "에서", "에게", "으로", "중", "중에서", "의", "이", "가",
        "은", "는", "을", "를", "와", "과", "그리고", "및", "and", "모두", "만", "어떤",
        "상품", "상품을", "상품은", "상품이", "알려줘", "알려주세요", "찾아줘", "보여줘", "정보",
        "정보를", "조회", "있어", "있는", "가진", "투자", "투자하는", "투자한", "관련된",
        "해줘", "설명해줘", "설명해주세요", "대해", "인가", "기준",
        "비교", "비교해줘", "추천", "추천해줘", "클래스",
        "종목", "종목을", "순으로", "순서로", "찾고", "각각의", "각각",
        "여러", "개", "개를",
        "도", "각", "상품의",
        "TOP", "top",
    }

    async def analyze(self, question: str) -> ParsedQuery:
        selectors = self._extract_peer_selectors(question)
        entities = self._extract_entity_mentions(question)
        entities = [item for item in entities if not any(item.raw_text == selector.raw_text for selector in selectors)]
        entity_spans = [range(*_find_span(question, item.raw_text)) for item in entities]
        entity_spans.extend(range(item.source_span.start, item.source_span.end) for item in selectors)
        product_types = self._extract_product_types(question, entity_spans)
        scope_text = list(question)
        for selector in selectors:
            scope_text[selector.source_span.start:selector.source_span.end] = " " * (selector.source_span.end - selector.source_span.start)
        product_universe, universe_span = self._extract_product_universe("".join(scope_text))
        filters = self._extract_filters(
            question, product_types, entity_spans, region_scope_span=universe_span,
        )
        sort, sort_spans = self._extract_sort(question)
        requested_fields = self._extract_requested_fields(
            question,
            sort_spans,
            filters,
            region_scope_span=universe_span,
        )
        relations = self._extract_relations(question)
        # In an explicit "<name> fund class" lookup, the suffix declares the
        # entity grain; it is not a separate traversal request.
        if any(item.entity_type == "fund_share_class" for item in entities):
            relations = [
                item for item in relations
                if item.raw_text.casefold() != "펀드 클래스"
            ]
        bound_targets = {
            item.target_raw_text.casefold()
            for item in relations
            if item.target_raw_text is not None
        }
        entities = [
            item
            for item in entities
            if item.raw_text.casefold() not in bound_targets
        ]
        # Holdings targets participate in Entity Resolution even though they
        # are not result-grain anchors.  This keeps company names and explicit
        # Security tickers on the same exact, canonical lookup boundary.
        target_entity_types = {
            "AssetManager": "management_company",
            "Organization": "organization",
            "EquitySecurity": "security",
            "Security": "security",
            "Index": "index",
        }
        entities.extend(
            EntityMention(
                raw_text=_strip_korean_particle(str(item.target_value)),
                entity_type=target_entity_types[item.target_type],
            )
            for item in relations
            if item.target_value is not None
            and item.target_type in target_entity_types
        )
        result_limit, limit_span = self._extract_limit(question)
        aggregation, aggregation_span = self._extract_aggregation(question)
        temporal, temporal_span = self._extract_temporal(question)
        intent = self._classify_intent(question, entities)
        # An explicit metric order is a lookup request even when phrased as
        # "추천". Other selection clauses remain required and can still fail.
        if intent is QueryIntent.RECOMMEND_PRODUCT and sort:
            intent = QueryIntent.SEARCH_PRODUCT
        semantic_search = any(
            marker.casefold() in question.casefold() for marker in self._semantic_markers
        )
        semantic_terms: list[str] = []
        complex_semantic_boolean = semantic_search and bool(
            re.search(r"(?:와|과|and).*(?:모두|동시에)", question, re.IGNORECASE)
        )
        complex_structured_boolean = self._is_complex_structured_boolean(question)
        drafts: list[_Draft] = []
        occupied: list[range] = []

        def add(
            start: int,
            end: int,
            semantic_type: ConstraintSemanticType,
            *,
            raw_text: str | None = None,
            payload: dict[str, Any] | None = None,
            ref: tuple[str, int] | None = None,
            status: ConstraintStatus = ConstraintStatus.PARSED,
            reason: str | None = None,
        ) -> None:
            drafts.append(
                _Draft(
                    start, end, raw_text or question[start:end], semantic_type,
                    payload or {}, ref, status, reason,
                )
            )
            occupied.append(range(start, end))

        for index, value in enumerate(product_types):
            try:
                start, end = _find_span(question, value)
            except ValueError:
                if value != "채권":
                    raise
                bond_alias = next(
                    (
                        alias
                        for alias in self._bond_type_aliases
                        if re.search(re.escape(alias), question, re.IGNORECASE)
                    ),
                    None,
                )
                if bond_alias is None:
                    raise
                start, end = _find_span(question, bond_alias)
            add(start, end, ConstraintSemanticType.PRODUCT_TYPE,
                payload={"value": value}, ref=("product_type", index))
            # Repeated type words are semantically idempotent.  Keep one
            # canonical constraint while marking every exact occurrence as
            # represented so a provider-scope prefix plus result suffix does
            # not create a false residual.
            occupied.extend(
                range(match.start(), match.end())
                for match in re.finditer(re.escape(value), question, re.IGNORECASE)
            )

        if product_universe is not None and universe_span is not None:
            add(
                universe_span.start,
                universe_span.stop,
                ConstraintSemanticType.PRODUCT_UNIVERSE,
                payload={"operands": product_universe.operands},
                ref=("product_universe", 0),
            )

        for index, item in enumerate(filters):
            start, end = self._filter_span(question, item, region_scope_span=universe_span)
            unit_is_unexecutable = (
                isinstance(item.value, TypedScalarValue)
                and item.value.unit is not ScalarUnit.NONE
            )
            add(
                start, end, ConstraintSemanticType.FILTER,
                payload={
                    "field": item.field, "operator": item.operator.value,
                    "value": item.model_dump(mode="json")["value"],
                },
                ref=("filter", index),
                status=(ConstraintStatus.UNSUPPORTED if unit_is_unexecutable
                        else ConstraintStatus.PARSED),
                reason=("dataset_unit_mapping_unverified"
                        if unit_is_unexecutable else None),
            )

        for index, (item, span) in enumerate(zip(sort, sort_spans, strict=True)):
            add(span.start, span.stop, ConstraintSemanticType.SORT,
                payload={"field": item.field, "direction": item.direction},
                ref=("sort", index))

        for index, value in enumerate(requested_fields):
            start, end = self._requested_field_span(question, value)
            path_weight = value in {"편입 비중", "보유 비중"}
            unavailable_date = value in {"관측일", "기준일"}
            add(start, end, ConstraintSemanticType.REQUESTED_FIELD,
                payload={"field": value, **({"projection_scope": "path", "relation": "holds",
                                            "property": "weight"} if path_weight else {})},
                ref=("requested_field", index),
                status=ConstraintStatus.UNSUPPORTED if path_weight or unavailable_date else ConstraintStatus.PARSED,
                reason=("holdings_weight_projection_unavailable" if path_weight
                        else "projection_unavailable" if unavailable_date else None))

        for index, item in enumerate(entities):
            start, end = _find_span(question, item.raw_text)
            add(start, end, ConstraintSemanticType.ENTITY,
                payload={"entity_type": item.entity_type}, ref=("entity", index))

        for index, selector in enumerate(selectors):
            add(selector.source_span.start, selector.source_span.end, ConstraintSemanticType.REQUESTED_FIELD,
                payload={"role": "PEER_SELECTOR", "selector": selector.raw_text}, ref=("selector", index),
                status=ConstraintStatus.UNSUPPORTED, reason="peer_selector_unverified")

        for index, item in enumerate(relations):
            start, end = self._relation_span(question, item)
            add(
                start, end, ConstraintSemanticType.RELATION,
                payload={
                    "direction": item.direction.value, "target": item.target_value,
                    "chain_id": item.chain_id, "path_position": item.path_position,
                },
                ref=("relation", index),
            )

        if result_limit is not None and limit_span is not None:
            limit_out_of_bounds = result_limit.value > 1000
            ranking_field_missing = (
                result_limit.raw_text.startswith(("상위", "하위", "가장", "TOP", "Top", "top"))
                and not sort
            )
            add(
                limit_span.start,
                limit_span.stop,
                ConstraintSemanticType.LIMIT,
                payload={"value": result_limit.value},
                ref=("limit", 0),
                status=(
                    ConstraintStatus.UNSUPPORTED
                    if limit_out_of_bounds or ranking_field_missing
                    else ConstraintStatus.PARSED
                ),
                reason=(
                    "top_n_out_of_bounds"
                    if limit_out_of_bounds
                    else "ranking_field_missing"
                    if ranking_field_missing
                    else None
                ),
            )
        if aggregation is not None and aggregation_span is not None:
            add(
                aggregation_span.start, aggregation_span.stop,
                ConstraintSemanticType.AGGREGATION,
                payload={"operator": aggregation.operator.value},
                ref=("aggregation", 0), status=ConstraintStatus.UNSUPPORTED,
                reason="aggregation_execution_not_implemented",
            )
        if temporal is not None and temporal_span is not None:
            add(
                temporal_span.start, temporal_span.stop, ConstraintSemanticType.TEMPORAL,
                payload={"requested_snapshot": temporal.requested_snapshot},
                ref=("temporal", 0), status=ConstraintStatus.UNSUPPORTED,
                reason="historical_snapshot_selection_not_implemented",
            )

        intent_match = re.search(
            r"비교|추천|알려줘|찾아줘|보여줘|정보|조회|있어", question
        )
        intent_start = intent_match.start() if intent_match else 0
        intent_end = intent_match.end() if intent_match else len(question)
        unsupported_intent = (
            intent in {QueryIntent.RECOMMEND_PRODUCT, QueryIntent.UNKNOWN}
            or (intent is QueryIntent.COMPARE_PRODUCTS and not sort and not requested_fields)
        )
        add(
            intent_start, intent_end, ConstraintSemanticType.INTENT,
            raw_text=intent.value,
            payload={
                "intent": intent.value,
                **(
                    {"ambiguity_class": "TRUE_AMBIGUITY"}
                    if intent is QueryIntent.COMPARE_PRODUCTS and not sort and not requested_fields
                    else {}
                ),
            },
            ref=("intent", 0),
            status=(ConstraintStatus.UNSUPPORTED if unsupported_intent
                    else ConstraintStatus.PARSED),
            reason=(
                "true_ambiguity:comparison_metric_missing"
                if intent is QueryIntent.COMPARE_PRODUCTS and not sort and not requested_fields
                else "intent_execution_not_implemented"
                if unsupported_intent
                else None
            ),
        )
        # Intent words are grammatical/request operators, not evidence that the
        # surrounding material clause was semantically consumed.
        occupied.pop()

        if semantic_search:
            semantic_query_text = self._semantic_query_text(question, occupied)
            semantic_terms = [semantic_query_text]
            add(
                0, len(question), ConstraintSemanticType.SEMANTIC, raw_text=question,
                payload={"query_text": semantic_query_text}, ref=("semantic", 0),
                status=(ConstraintStatus.UNSUPPORTED if complex_semantic_boolean
                        else ConstraintStatus.PARSED),
                reason=("conjunctive_semantic_retrieval_not_guaranteed"
                        if complex_semantic_boolean else None),
            )
        if complex_structured_boolean:
            add(
                0, len(question), ConstraintSemanticType.BOOLEAN, raw_text=question,
                payload={"operator": "or"}, ref=("boolean", 0),
                status=ConstraintStatus.UNSUPPORTED,
                reason="grouped_boolean_planning_not_implemented",
            )

        residuals = [] if semantic_search else self._unparsed_material_spans(
            question, occupied
        )
        for residual in residuals:
            subjective = bool(re.fullmatch(r"안전(?:한|하고|하게)?|좋은|좋고|유망(?:한|하고)?", residual.raw_text))
            add(
                residual.source_span.start, residual.source_span.end,
                ConstraintSemanticType.SUBJECTIVE if subjective else ConstraintSemanticType.SEMANTIC,
                raw_text=residual.raw_text,
                payload={"query_text": residual.raw_text},
                status=ConstraintStatus.UNSUPPORTED,
                reason="subjective_execution_unsupported" if subjective else "unparsed_material_clause",
            )
        residuals = [item for item in residuals if not re.fullmatch(
            r"안전(?:한|하고|하게)?|좋은|좋고|유망(?:한|하고)?", item.raw_text)]

        constraints, ref_ids = _materialize_constraints(drafts)
        filters = [item.model_copy(update={"constraint_id": ref_ids.get(("filter", i))})
                   for i, item in enumerate(filters)]
        sort = [item.model_copy(update={"constraint_id": ref_ids.get(("sort", i))})
                for i, item in enumerate(sort)]
        entities = [item.model_copy(update={"constraint_id": ref_ids.get(("entity", i))})
                    for i, item in enumerate(entities)]
        selectors = [item.model_copy(update={"constraint_id": ref_ids.get(("selector", i))})
                     for i, item in enumerate(selectors)]
        relations = [
            item.model_copy(update={"constraint_id": ref_ids.get(("relation", i))})
            for i, item in enumerate(relations)
        ]
        if product_universe is not None:
            product_universe = product_universe.model_copy(
                update={"constraint_id": ref_ids.get(("product_universe", 0))}
            )
        if result_limit is not None:
            result_limit = result_limit.model_copy(
                update={"constraint_id": ref_ids.get(("limit", 0))}
            )
        if aggregation is not None:
            aggregation = aggregation.model_copy(
                update={"constraint_id": ref_ids.get(("aggregation", 0))}
            )
        if temporal is not None:
            temporal = temporal.model_copy(
                update={"constraint_id": ref_ids.get(("temporal", 0))}
            )
        unsupported = [
            item.constraint_id for item in constraints
            if item.status is ConstraintStatus.UNSUPPORTED
        ]
        return normalize_query_semantics(ParsedQuery(
            original_question=question, intent=intent, product_types=product_types,
            product_universe=product_universe,
            entities=entities, selectors=selectors, filters=filters, relations=relations, sort=sort,
            requested_fields=requested_fields,
            semantic_terms=semantic_terms,
            requires_semantic_search=semantic_search,
            boolean_expression=_boolean_expression(filters, constraints),
            result_limit=result_limit, aggregation=aggregation,
            temporal_constraint=temporal, semantic_constraints=constraints,
            semantic_coverage=(SemanticCoverageStatus.INCOMPLETE
                               if unsupported or residuals
                               else SemanticCoverageStatus.COMPLETE),
            unparsed_material_spans=residuals,
            unsupported_constraint_ids=unsupported,
        ))

    def _classify_intent(self, question: str, entities: list[EntityMention]) -> QueryIntent:
        etp_status_query = bool(
            re.search(r"(?:ETF|ETN|ETP|\uc0c1\uc7a5\uc9c0\uc218)", question, re.IGNORECASE)
            and re.search(
                r"\uad6c\ub9e4|\ub9e4\uc218|\ud310\ub9e4|\uac70\ub798\s*\uc815\uc9c0|\uac70\ub798\uc815\uc9c0|\uc0c1\uc7a5\s*(?:\uc885\ub8cc|\ud3d0\uc9c0)|\ucd5c\uc2e0\s*(?:\uac00\uaca9|\uc885\uac00)|\uc624\ub798\ub41c\s*(?:\uac00\uaca9|\uc885\uac00)|(?:\uac00\uaca9|\uc885\uac00)(?:\uc774|\uac00)?\s*\uc624\ub798\ub41c|\uc815\ubcf4(?:\uac00)?\s*\ubd80\uc871(?:\ud574)?|\ucd94\ucc9c\ud558\uae30\s*\uc5b4\ub824\uc6b4",
                question,
            )
        )
        if etp_status_query:
            return QueryIntent.SEARCH_PRODUCT
        if "비교" in question:
            return QueryIntent.COMPARE_PRODUCTS
        if "추천" in question:
            return QueryIntent.RECOMMEND_PRODUCT
        if any(token in question for token in (
            "알려줘", "알려주세요", "찾아줘", "보여줘", "설명해줘",
            "설명해주세요", "있어",
        )):
            return QueryIntent.SEARCH_PRODUCT
        if entities and any(token in question for token in ("정보", "조회")):
            return QueryIntent.LOOKUP_PRODUCT
        if self._find_aliases(question, self._product_type_aliases):
            return QueryIntent.SEARCH_PRODUCT
        return QueryIntent.UNKNOWN

    def _extract_product_types(
        self,
        question: str,
        excluded_spans: list[range] | None = None,
    ) -> list[str]:
        matches = self._find_aliases(
            question, self._product_type_aliases, excluded_spans=excluded_spans,
        )
        if "펀드 클래스" in question:
            matches = [value for value in matches if value != "펀드"]
        # "채권형" is an AssetClass expression.  It must not also become the
        # Bond product subtype merely because one alias is a substring of the
        # other.  A separate standalone "채권" still preserves product grain.
        if "채권" in matches and not re.search(r"채권(?!형)", question):
            matches = [value for value in matches if value != "채권"]
        lifecycle_bond_exclusion = bool(
            _BOND_LIFECYCLE_EXCLUSION_PATTERN.search(question)
        )
        matches = [
            value for value in matches
            if lifecycle_bond_exclusion and value == "채권"
            or not re.search(
                rf"{re.escape(value)}(?:이|가)?\s*(?:아닌|제외)",
                question,
                re.IGNORECASE,
            )
        ]
        non_bond_product = any(
            value.upper() in {"ETF", "ETN"}
            or value in {"상장지수펀드", "상장지수증권"}
            or value.casefold() == "fund"
            or "펀드" in value
            for value in matches
        )
        if non_bond_product:
            matches = [
                value for value in matches
                if value.casefold() not in {"채권", "bond"}
            ]
        elif self._find_aliases(
            question,
            self._bond_type_aliases,
            excluded_spans=excluded_spans,
        ) and "채권" not in matches:
            matches.append("채권")
        return matches

    def _extract_filters(
        self,
        question: str,
        product_types: list[str],
        excluded_spans: list[range] | None = None,
        *,
        region_scope_span: range | None = None,
    ) -> list[FilterSpec]:
        filters: list[FilterSpec] = []
        field_label_spans = [
            range(match.start(), match.end())
            for alias in self._field_aliases
            for match in re.finditer(re.escape(alias), question, re.IGNORECASE)
            if alias != "통화"
            or re.search(r"의\s*$", question[:match.start()]) is not None
        ]
        trading_currency = _TRADING_CURRENCY_PATTERN.search(question)
        trading_currency_span = (
            range(trading_currency.start(), trading_currency.end())
            if trading_currency is not None
            else None
        )
        market_scope_match = re.search(
            r"시장범위(?:가|는|은)?\s*(국내외혼합|국내|해외)(?:인|인\s+상품)?"
            r"|(국내외혼합|국내|해외)\s*시장범위(?:인)?",
            question,
            re.IGNORECASE,
        )
        market_scope_span = (
            range(market_scope_match.start(), market_scope_match.end())
            if market_scope_match is not None
            else None
        )
        bond_types = self._find_aliases(
            question,
            self._bond_type_aliases,
            excluded_spans=excluded_spans,
        )
        bond_type_spans = [
            range(match.start(), match.end())
            for value in bond_types
            for match in re.finditer(re.escape(value), question, re.IGNORECASE)
        ]
        for pattern in MATURITY_YEAR_PATTERNS:
            for match in pattern.finditer(question):
                if not any(match.start() in span for span in (excluded_spans or [])):
                    filters.append(FilterSpec(
                        field="만기일",
                        operator="eq",
                        value=match.group("year"),
                    ))
        # A geography already consumed by product-universe selection is not
        # an exposure predicate. Select remaining aliases before taking the
        # first region, so an explicit second geography cannot be discarded.
        regions = self._find_aliases(
            question, self._region_aliases,
            excluded_spans=[
                *(excluded_spans or []),
                *field_label_spans,
                *([region_scope_span] if region_scope_span else []),
                *([market_scope_span] if market_scope_span else []),
                *([trading_currency_span] if trading_currency_span else []),
            ],
        )
        assets = self._find_aliases(
            question,
            self._asset_type_aliases,
            excluded_spans=[
                *(excluded_spans or []), *field_label_spans, *bond_type_spans,
            ],
        )
        if "표시통화" in question or "거래통화" in question:
            assets = [value for value in assets if value != "통화"]
        if len(product_types) == 1 and product_types[0].casefold() in {"채권", "bond"}:
            assets = [
                value for value in assets
                if value.casefold() not in {"채권", "bond"}
            ]
        lifecycle_bond_exclusion = bool(
            _BOND_LIFECYCLE_EXCLUSION_PATTERN.search(question)
        )
        if regions:
            listing_regions = {
                match.group(1).casefold()
                for match in re.finditer(
                    r"(미국|한국|국내)\s*(?:증시|시장|거래소)(?:에)?\s*상장",
                    question,
                    re.IGNORECASE,
                )
            }
            regions = [
                value for value in regions if value.casefold() not in listing_regions
            ]
        if regions:
            negated = next((value for value in regions if re.search(
                rf"{re.escape(value)}(?:을|를|이|가)?\s*(?:제외|아닌)", question,
                re.IGNORECASE)), None)
            if negated is not None:
                filters.append(FilterSpec(field="region", operator="ne", value=negated))
            elif len(regions) > 1 and "또는" in question:
                filters.append(FilterSpec(field="region", operator="in", value=regions))
            else:
                filters.append(FilterSpec(field="region", operator="eq", value=regions[0]))
        if assets:
            negated = next((value for value in assets if re.search(
                rf"{re.escape(value)}(?:이|가)?\s*(?:아닌|제외)", question,
                re.IGNORECASE)), None)
            if negated is not None:
                filters.append(FilterSpec(
                    field="asset_type",
                    operator="ne",
                    value=_normalize_asset_type_query_value(negated),
                ))
            elif len(assets) > 1 and "또는" in question:
                filters.append(FilterSpec(
                    field="asset_type",
                    operator="in",
                    value=[_normalize_asset_type_query_value(value) for value in assets],
                ))
            else:
                filters.append(FilterSpec(
                    field="asset_type",
                    operator="eq",
                    value=_normalize_asset_type_query_value(assets[0]),
                ))
        if bond_types:
            negated = next((value for value in bond_types if re.search(
                rf"{re.escape(value)}(?:을|를|이|가)?\s*(?:제외|아닌)",
                question,
                re.IGNORECASE,
            )), None)
            if negated is not None:
                filters.append(FilterSpec(
                    field="bond_type",
                    operator=FilterOperator.NE,
                    value=_BOND_TYPE_QUERY_ALIASES_NORMALIZED[negated.casefold()],
                ))
            elif len(bond_types) > 1 and "또는" in question:
                filters.append(FilterSpec(
                    field="bond_type",
                    operator=FilterOperator.IN,
                    value=[
                        _BOND_TYPE_QUERY_ALIASES_NORMALIZED[value.casefold()]
                        for value in bond_types
                    ],
                ))
            else:
                filters.append(FilterSpec(
                    field="bond_type",
                    operator=FilterOperator.EQ,
                    value=_BOND_TYPE_QUERY_ALIASES_NORMALIZED[
                        bond_types[0].casefold()
                    ],
                ))
        if market_scope_match is not None:
            filters.append(FilterSpec(
                field="market_scope",
                operator=FilterOperator.EQ,
                value=market_scope_match.group(1) or market_scope_match.group(2),
            ))
        for value in self._find_aliases(
            question, self._product_type_aliases, excluded_spans=excluded_spans,
        ):
            if value == "채권" and lifecycle_bond_exclusion:
                continue
            if re.search(rf"{re.escape(value)}(?:이|가)?\s*(?:아닌|제외)",
                         question, re.IGNORECASE):
                filters.append(FilterSpec(field="product_type", operator="ne", value=value))
        offering_type = re.search(
            r"(?P<offering>공모|사모|Public|Private)\s*펀드",
            question,
            re.IGNORECASE,
        )
        if offering_type is not None:
            filters.append(
                FilterSpec(
                    field="offering_type",
                    operator="eq",
                    value=_OFFERING_TYPE_QUERY_ALIASES[
                        offering_type.group("offering").casefold()
                    ],
                )
            )
        listing = re.search(
            r"(미국|한국|국내)\s*(?:증시|시장|거래소)(?:에)?\s*상장(?:된|한)?",
            question,
            re.IGNORECASE,
        )
        if listing:
            filters.append(
                FilterSpec(
                    field="listing_country",
                    operator=FilterOperator.EQ,
                    value="US" if listing.group(1) == "미국" else "KR",
                )
            )
        if trading_currency:
            raw_currency = next(
                value for value in trading_currency.groupdict().values() if value
            )
            filters.append(FilterSpec(
                field="trading_currency",
                operator=FilterOperator.EQ,
                value=_TRADING_CURRENCY_QUERY_ALIASES[
                    re.sub(r"\s+", "", raw_currency).casefold()
                ],
            ))
        if re.search(r"원화\s*채권", question):
            filters.append(
                FilterSpec(field="currency", operator=FilterOperator.EQ, value="KRW")
            )
        filters.extend(self._extract_maturity_filters(question))
        rating = re.search(
            r"(?:신용등급\s*)?([A-Z]{1,4}(?:[+\-0])?)\s*(이상|이하|초과|미만)",
            question,
            re.IGNORECASE,
        )
        if rating:
            operators = {
                "이상": FilterOperator.GTE,
                "이하": FilterOperator.LTE,
                "초과": FilterOperator.GT,
                "미만": FilterOperator.LT,
            }
            filters.append(
                FilterSpec(
                    field="credit_rating",
                    operator=operators[rating.group(2)],
                    value=rating.group(1).upper(),
                )
            )
        etp_requested = bool(
            re.search(r"(?:ETF|ETN|ETP|\uc0c1\uc7a5\uc9c0\uc218)", question, re.IGNORECASE)
        )
        if etp_requested and re.search(
            r"(?:\ud604\uc7ac|\uc9c0\uae08)?\s*(?:\uad6c\ub9e4|\ub9e4\uc218|\ud310\ub9e4)\s*(?:\uac00\ub2a5|\uc911)(?:\ud558\uc9c0\ub9cc)?",
            question,
            re.IGNORECASE,
        ):
            filters.append(
                FilterSpec(
                    field="current_etp_sale_eligible",
                    operator=FilterOperator.EQ,
                    value=True,
                )
            )
        if etp_requested and re.search(
            r"\uac70\ub798\s*\uc815\uc9c0(?:\uac00|\ub294)?\s*\uc544\ub2cc|\uac70\ub798\uc815\uc9c0\uac00\s*\uc544\ub2cc|\uac70\ub798\uc815\uc9c0\s*\uc81c\uc678",
            question,
        ):
            filters.append(FilterSpec(
                field="etp_trading_status",
                operator=FilterOperator.EQ,
                value="TRADING_ACTIVE",
            ))
        if etp_requested and re.search(r"\uc0c1\uc7a5\s*(?:\uc885\ub8cc|\ud3d0\uc9c0).*\uc81c\uc678", question):
            filters.append(FilterSpec(
                field="etp_listing_ended",
                operator=FilterOperator.NE,
                value=True,
            ))
        if etp_requested and re.search(r"\ucd5c\uc2e0\s*(?:\uac00\uaca9|\uc885\uac00)(?:\uc774|\uac00)?\s*(?:\uc788\ub294|\ubcf4\uc720)", question):
            filters.append(FilterSpec(
                field="latest_etp_price_available",
                operator=FilterOperator.EQ,
                value=True,
            ))
        if etp_requested and re.search(r"(?:\uac00\uaca9|\uc885\uac00)(?:\uc774|\uac00)?\s*\uc624\ub798\ub41c|\uc624\ub798\ub41c\s*(?:\uac00\uaca9|\uc885\uac00)", question):
            filters.append(FilterSpec(
                field="stale_etp_price_warning",
                operator=FilterOperator.EQ,
                value=True,
            ))
        if etp_requested and re.search(r"\uc815\ubcf4(?:\uac00)?\s*\ubd80\uc871(?:\ud55c|\ud574)?(?:\s*\ucd94\ucc9c\ud558\uae30\s*\uc5b4\ub824\uc6b4)?|\ucd94\ucc9c\ud558\uae30\s*\uc5b4\ub824\uc6b4", question):
            filters.append(FilterSpec(
                field="etp_insufficient_info",
                operator=FilterOperator.EQ,
                value=True,
            ))
        if not etp_requested and re.search(r"현재\s*판매\s*가능", question):
            filters.append(
                FilterSpec(
                    field="current_sale_available",
                    operator=FilterOperator.EQ,
                    value=True,
                )
            )
        bond_requested = "채권" in product_types or bool(
            re.search(r"채권", question, re.IGNORECASE)
        )
        if bond_requested and (
            re.search(
                r"(?:(?:현재|지금)\s*)?구매\s*가능(?:한)?",
                question,
                re.IGNORECASE,
            )
            or lifecycle_bond_exclusion
        ):
            filters.append(FilterSpec(
                field="current_bond_purchase_eligible",
                operator=FilterOperator.EQ,
                value=True,
            ))
        if bond_requested and re.search(r"장내\s*채권", question):
            filters.append(FilterSpec(
                field="bond_market_presence",
                operator=FilterOperator.EQ,
                value="EXCHANGE_TRADED",
            ))
        if bond_requested and re.search(r"장외\s*채권", question):
            filters.append(FilterSpec(
                field="bond_market_presence",
                operator=FilterOperator.EQ,
                value="OTC",
            ))
        no_sale_lot = bond_requested and bool(re.search(
            r"(?:판매\s*(?:LOT|로트)|판매조건)(?:이|은|는|가)?\s*없",
            question,
            re.IGNORECASE,
        ))
        multiple_sale_lots = bond_requested and bool(re.search(
            r"(?:하나의\s*종목에\s*)?(?:여러|복수)\s*(?:판매조건|판매\s*(?:LOT|로트))",
            question,
            re.IGNORECASE,
        ))
        same_lot_price_yield = bond_requested and bool(re.search(
            r"매매단가(?:와|과)\s*수익률(?:이|가)?\s*제공된",
            question,
            re.IGNORECASE,
        ))
        has_sale_lot = bond_requested and bool(re.search(
            r"(?:미래에셋\s*)?(?:판매조건|판매\s*(?:LOT|로트))(?:이|은|는|가)?\s*있는",
            question,
            re.IGNORECASE,
        ))
        if no_sale_lot:
            filters.append(FilterSpec(
                field="has_sale_lot",
                operator=FilterOperator.EQ,
                value=False,
            ))
        elif multiple_sale_lots:
            filters.append(FilterSpec(
                field="has_multiple_sale_lots",
                operator=FilterOperator.EQ,
                value=True,
            ))
        elif has_sale_lot:
            filters.append(FilterSpec(
                field="has_sale_lot",
                operator=FilterOperator.EQ,
                value=True,
            ))
        if same_lot_price_yield:
            filters.append(FilterSpec(
                field="has_trade_price_and_buy_yield_sale_lot",
                operator=FilterOperator.EQ,
                value=True,
            ))
        fund_subscription = re.search(
            r"(?:(?:현재|지금)\s*)?(?:미래에셋(?:에서)?\s*)?(?:가입|신규\s*가입|추가매수)(?:할\s*수\s*있는|\s*가능(?:한)?)|미래에셋(?:에서)?\s*판매\s*중(?:인)?",
            question,
        )
        if fund_subscription:
            filters.append(FilterSpec(
                field="current_fund_subscription_eligible",
                operator=FilterOperator.EQ,
                value=True,
            ))
        subscription_exclusion = re.search(
            r"판매완료\s*펀드(?:는|를)?\s*제외", question
        )
        if subscription_exclusion:
            filters.append(FilterSpec(
                field="subscription_status",
                operator=FilterOperator.NE,
                value="SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION",
            ))
        elif subscription_match := _SUBSCRIPTION_STATUS_FUND_PATTERN.search(question):
            subscription_status = _SUBSCRIPTION_STATUS_QUERY_ALIASES_NORMALIZED[
                re.sub(r"\s+", "", subscription_match.group("status")).casefold()
            ]
            filters.append(FilterSpec(
                field="subscription_status",
                operator=FilterOperator.EQ,
                value=subscription_status,
            ))
        if re.search(r"최신\s*기준가(?:가)?\s*(?:있는|보유)", question):
            filters.append(FilterSpec(
                field="latest_fund_price_available",
                operator=FilterOperator.EQ,
                value=True,
            ))
        filters.extend(self._extract_numeric_filters(question))
        return filters

    def _extract_maturity_filters(self, question: str) -> list[FilterSpec]:
        result: list[FilterSpec] = []
        for match in re.finditer(
            r"(?:만기|만기일)(?:이|가|은|는)?\s*(\d{4})년(?:인|의)?",
            question,
        ):
            year = int(match.group(1))
            result.append(
                FilterSpec(
                    field="만기일",
                    operator=FilterOperator.BETWEEN,
                    value=[f"{year:04d}-01-01", f"{year:04d}-12-31"],
                )
            )
        return result

    def _extract_numeric_filters(self, question: str) -> list[FilterSpec]:
        patterns = (
            ("aum", ScalarUnit.KRW, re.compile(
                r"(?:순자산|AUM|운용규모)(?:이|가|은|는)?\s*"
                r"([\d,]+(?:\.\d+)?)\s*(조|억|원)\s*(이상|이하|초과|미만)",
                re.IGNORECASE)),
            ("expense_ratio", ScalarUnit.RATIO, re.compile(
                r"(?:총보수|보수율|운용보수)(?:이|가|은|는)?\s*"
                r"([\d,]+(?:\.\d+)?)\s*(%)\s*(이상|이하|초과|미만)",
                re.IGNORECASE)),
        )
        operators = {"이상": FilterOperator.GTE, "이하": FilterOperator.LTE,
                     "초과": FilterOperator.GT, "미만": FilterOperator.LT}
        result: list[FilterSpec] = []
        for field, unit, pattern in patterns:
            for match in pattern.finditer(question):
                number = float(match.group(1).replace(",", ""))
                raw_unit = match.group(2)
                if unit is ScalarUnit.RATIO:
                    normalized = number / 100.0
                else:
                    multiplier = {"원": 1, "억": 100_000_000,
                                  "조": 1_000_000_000_000}
                    normalized = number * multiplier[raw_unit]
                result.append(FilterSpec(
                    field=field, operator=operators[match.group(3)],
                    value=TypedScalarValue(
                        raw=f"{match.group(1)}{raw_unit}", normalized=normalized,
                        unit=unit, currency="KRW" if unit is ScalarUnit.KRW else None,
                    ),
                ))
        return result

    def _extract_sort(self, question: str) -> tuple[list[SortSpec], list[range]]:
        found: list[tuple[int, SortSpec, range]] = []
        # Preserve historical change as metric semantics, never as a current
        # snapshot AUM ranking. Normalization marks the series unavailable.
        historical = re.compile(
            r"(?:최근\s*)?(?:\d+\s*(?:일|개월|년)|[136]M|1Y)\s*(?:동안\s*)?"
            r"(AUM|순자산|운용규모)(?:이|가|은|는)?\s*"
            r"(?:가장\s*)?(많이|크게|적게)\s*(?:증가한|감소한|변화한|늘어난|줄어든)",
            re.IGNORECASE,
        )
        for match in historical.finditer(question):
            found.append((match.start(), SortSpec(
                field=match.group(1), direction="asc" if match.group(2) == "적게" else "desc",
            ), range(match.start(), match.end())))
        adjective_pattern = "큰|높은|많은|낮은|작은|적은|크고|높고|많고|낮고|작고|적고"
        for alias in sorted(self._ranking_field_aliases, key=len, reverse=True):
            adjectives = (
                "빠른|늦은|이른|가까운"
                if alias in {"만기", "만기일"}
                else adjective_pattern
            )
            if "수익률" in alias:
                adjectives += "|좋은|좋고|나쁜|나쁘고"
            # In return comparisons, ``최근`` qualifies the trailing-period
            # metric (for example, 최근 6개월 수익률).  Consume it with the
            # structured sort span so it cannot become an unrelated temporal
            # residual and spuriously trigger semantic-parser fallback.
            context_prefix = r"(?:최근\s*)?" if "수익률" in alias else ""
            pattern = re.compile(
                rf"{context_prefix}({re.escape(alias)})(?:이|가|은|는)?\s*(?:가장\s*)?"
                rf"({adjectives})(?:고|며)?(?:\s*순(?:서)?(?:로|으로)?)?", re.IGNORECASE,
            )
            for match in pattern.finditer(question):
                direction = (
                    "desc"
                    if match.group(2) in self._descending_words
                    or match.group(2) in {"크고", "높고", "많고", "늦은", "좋은", "좋고"}
                    else "asc"
                )
                found.append((match.start(), SortSpec(field=match.group(1), direction=direction),
                              range(match.start(), match.end())))
            explicit = re.compile(
                rf"{context_prefix}({re.escape(alias)})(?:이|가|은|는)?\s*(?:기준\s*)?"
                r"(오름차순|내림차순|ASC|DESC)(?:으로|로)?",
                re.IGNORECASE,
            )
            for match in explicit.finditer(question):
                direction = (
                    "asc" if match.group(2).casefold() in {"오름차순", "asc"} else "desc"
                )
                found.append(
                    (
                        match.start(),
                        SortSpec(field=match.group(1), direction=direction),
                        range(match.start(), match.end()),
                    )
                )
            top_ranking = re.compile(
                rf"{context_prefix}({re.escape(alias)})(?:이|가|은|는)?\s*(?:기준\s*)?"
                r"(TOP|상위|하위)\s*\d+",
                re.IGNORECASE,
            )
            for match in top_ranking.finditer(question):
                found.append(
                    (
                        match.start(),
                        SortSpec(
                            field=match.group(1),
                            direction=(
                                "asc"
                                if match.group(2).casefold() == "하위"
                                else "desc"
                            ),
                        ),
                        range(match.start(), match.end()),
                    )
                )
        # Long aliases win overlapping matches ("1년 수익률" over "수익률").
        selected: list[tuple[int, SortSpec, range]] = []
        for candidate in sorted(
            found, key=lambda item: (item[0], -(item[2].stop - item[2].start))
        ):
            span = candidate[2]
            if any(
                span.start < existing.stop and span.stop > existing.start
                for _, _, existing in selected
            ):
                continue
            selected.append(candidate)
        selected.sort(key=lambda item: item[0])
        return [item[1] for item in selected], [item[2] for item in selected]

    def _extract_requested_fields(
        self,
        question: str,
        sort_spans: list[range],
        filters: list[FilterSpec],
        *,
        region_scope_span: range | None = None,
    ) -> list[str]:
        requested: list[tuple[int, int, str]] = []
        filter_spans = [
            range(*self._filter_span(
                question,
                item,
                region_scope_span=region_scope_span,
            ))
            for item in filters
        ]
        for alias in self._field_aliases:
            for match in re.finditer(re.escape(alias), question, re.IGNORECASE):
                if alias.casefold() in {"지역", "region"}:
                    suffix = question[match.end():]
                    if suffix.startswith("별") or self._find_aliases(
                        re.split(
                            r"알려줘|알려주세요|찾아줘|보여줘|조회",
                            suffix,
                            maxsplit=1,
                        )[0],
                        self._product_type_aliases,
                    ):
                        continue
                if (
                    alias == "통화"
                    and question[max(0, match.start() - 2):match.start()]
                    in {"표시", "거래"}
                ):
                    continue
                if any(match.start() in span for span in sort_spans):
                    continue
                if any(match.start() in span for span in filter_spans):
                    continue
                if self._inside_numeric_condition(question, match.start()):
                    continue
                requested.append((match.start(), match.end(), match.group(0)))
        # Generic return uses the same reviewed default-period policy in a
        # possessive lookup or coordinated comparison projection.
        for match in re.finditer(r"수익률", question):
            projection_context = ("비교" in question
                                  or bool(re.search(r"의\s*", question[:match.start()]))
                                  or bool(requested) and bool(re.search(r"과|와|,", question)))
            availability = re.match(r"(?:이|가)?\s*제공된", question[match.end():])
            if (projection_context and not availability and not any(match.start() in span for span in sort_spans)
                    and not any(start <= match.start() < end for start, end, _ in requested)
                    and not self._inside_numeric_condition(question, match.start())):
                requested.append((match.start(), match.end(), match.group()))
        selected: list[tuple[int, int, str]] = []
        for candidate in sorted(
            requested, key=lambda item: (item[0], -(item[1] - item[0]))
        ):
            start, end, _ = candidate
            if any(
                start < selected_end and end > selected_start
                for selected_start, selected_end, _ in selected
            ):
                continue
            selected.append(candidate)
        return self._deduplicate(value for _, _, value in sorted(selected))

    @staticmethod
    def _requested_field_span(question: str, value: str) -> tuple[int, int]:
        start, end = _find_span(question, value)
        if "수익률" not in value:
            return start, end
        prefix = re.search(r"최근\s*$", question[:start])
        return (prefix.start(), end) if prefix is not None else (start, end)

    @staticmethod
    def _inside_numeric_condition(question: str, position: int) -> bool:
        return bool(re.match(
            r"[^\d]{0,8}[\d,]+(?:\.\d+)?\s*(?:%|원|억|조)\s*"
            r"(?:이상|이하|초과|미만)", question[position:position + 40]))

    def _extract_entity_mentions(self, question: str) -> list[EntityMention]:
        coordinated = self._coordinated_entity_mentions(question)
        if coordinated:
            return coordinated
        candidates: list[tuple[int, int, str, str]] = []
        manager = self._management_collection_match(question)
        if manager is not None:
            candidates.append((manager.start(), manager.end(), manager.group(1).strip(), "management_company"))
        patterns = (
            (
                r"^(.+?)\s+(?:펀드\s*)?클래스\s+정보(?:를)?\s*(?:알려줘|보여줘|조회)",
                "fund_share_class",
            ),
            (r"^(.+?)(?:에는|에)\s*어떤\s*(?:펀드\s*)?클래스", "fund"),
            (r"^(.+?)(?:이|가)\s*운용하는", "management_company"),
            (r"^(.+?)\s*상품\s*중", "management_company"),
            (
                r"^(.+?)의\s*"
                r"(?:(?:최근\s*)?(?:1일|1개월|3개월|6개월|1년|1D|1M|3M|6M|1Y|YTD)?\s*수익률|"
                r"위험\s*정보|위험도|리스크|위험|위험요인|구조|투자전략|특징|동향)",
                "product",
            ),
            (r"^(.+?)의\s*(?:운용사|발행사|기초지수|추종지수|벤치마크)", "product"),
            (
                r"^(.+?)의\s*(?:운용규모|순자산|AUM|총보수|보수율|"
                r"기준가격|NAV|가격|티커|ticker|ISIN)",
                "product",
            ),
            (r"^(.+?)의\s*(?:판매\s*LOT|판매\s*로트)", "product"),
            (r"^(.+?)\s+정보(?:를)?\s*(?:알려줘|보여줘|조회)", "product"),
            (
                r"^(.+?\s+(?:ETF|ETN|ETP|펀드|채권|상품))\s*"
                r"(?:알려줘|알려주세요|보여줘|조회(?:해줘)?)",
                "product",
            ),
            (
                r"^(.+?\s+(?:ETF|ETN|ETP|펀드|채권|상품))(?:에)?\s*대해\s*"
                r"(?:설명해줘|설명해주세요)",
                "product",
            ),
            (
                r"^(.+?)(?:을|를)\s*"
                r"(?:알려줘|보여줘|조회(?:해줘)?)[?.!]?\s*$",
                "product",
            ),
            (r"[A-Za-z0-9가-힣]+자산운용", "management_company"),
        )
        for pattern, entity_type in patterns:
            for match in re.finditer(pattern, question, re.IGNORECASE):
                raw_text = match.group(1) if match.lastindex else match.group(0)
                candidates.append((match.start(), match.end(), raw_text.strip(), entity_type))
        accepted: list[tuple[int, int, str, str]] = []
        for candidate in sorted(candidates):
            start, end, raw_text, entity_type = candidate
            if entity_type == "product" and re.search(
                r"(?:편입한|편입된|보유한|추종하는|운용하는|발행한)",
                raw_text,
                re.IGNORECASE,
            ):
                continue
            if entity_type == "product" and self._is_structured_product_expression(
                raw_text
            ):
                continue
            if any(start < other_end and end > other_start
                   for other_start, other_end, *_ in accepted):
                continue
            accepted.append((
                start,
                end,
                _strip_entity_type_suffix(raw_text, entity_type),
                entity_type,
            ))
        return [EntityMention(raw_text=_strip_korean_particle(raw_text),
                              entity_type=entity_type)
                for _, _, raw_text, entity_type in accepted]

    def _management_collection_match(self, question: str):
        """A collection prefix proposes a manager identity; resolver proves it."""
        match = re.search(
            r"^(.+?)\s+((?:(?:국내|해외)\s*)?(?:ETF|ETN|펀드|채권))\s*중(?:에서)?",
            question, re.IGNORECASE,
        )
        if match is None:
            return None
        prefix = match.group(1).strip()
        if self._is_structured_product_expression(prefix + " ETF"):
            return None
        # Existing provider scopes already have their own identity contract.
        universe, scope_span = self._extract_product_universe(match.group())
        if universe and scope_span and scope_span.start == match.start():
            return None
        return match

    @staticmethod
    def _extract_peer_selectors(question: str) -> list[PeerSelector]:
        # A requested "other ... ETF" category is not a named product. Keep
        # its original words; no peers/index/exposure are invented here.
        return [PeerSelector(raw_text=match.group(), source_span=SourceSpan(start=match.start(), end=match.end()))
                for match in re.finditer(r"다른\s+[^,\n]+?\s+ETF(?=의|와|과|를|을|\s|$)", question, re.IGNORECASE)]

    @staticmethod
    def _coordinated_entity_mentions(question: str) -> list[EntityMention]:
        """Separate explicit entity lists; resolution still owns their identity."""
        if not re.search(r"비교|가장|최고|최저|다른\s+.*ETF", question, re.IGNORECASE):
            return []
        prefix = re.split(r"의\s*|\s+중(?:에서)?\s*", question, maxsplit=1)[0]
        if prefix == question or re.search(r"운용하는|보유한|편입한|이상|이하", prefix):
            return []
        # Conjunctions require a boundary on the right, avoiding syllables
        # inside official names; quoted names also retain their full text.
        parts = re.split(r"\s*[,、]\s*|(?:와|과)\s+|\s+(?:및|그리고|and)\s+", prefix, flags=re.IGNORECASE)
        if len(parts) < 2 or any(not part.strip() for part in parts):
            return []
        return [EntityMention(raw_text=part.strip().strip("\"'"), entity_type="product")
                for part in parts]

    def _is_structured_product_expression(self, raw_text: str) -> bool:
        """Distinguish collection constraints from a named product prefix."""
        classification_text = re.sub(
            r"\s*\d+\s*개(?:만)?\s*$", "", raw_text.strip()
        )
        classification_text = re.sub(
            r"(?:을|를|은|는|이|가)?\s*상품\s*$", "", classification_text
        ).strip()
        if classification_text.casefold() in _BOND_TYPE_QUERY_ALIASES_NORMALIZED:
            return True
        if any(
            classification_text.casefold() == alias.casefold()
            for alias in (
                *self._asset_type_aliases,
                *self._region_aliases,
                *_OFFERING_TYPE_QUERY_ALIASES,
                *_SUBSCRIPTION_STATUS_QUERY_ALIASES,
                *_FUND_ELIGIBILITY_QUERY_ALIASES,
            )
        ):
            return True
        if _TRADING_CURRENCY_PATTERN.search(raw_text):
            return True
        possessive = re.match(r"^(.+?)의(?:\s|$|[A-Za-z가-힣])", raw_text)
        if possessive is not None and self._is_structured_product_expression(
            possessive.group(1).strip()
        ):
            # ``ETF의 <field>`` denotes a collection projection, not a named
            # product. The suffix must remain available to field extraction or
            # fail-closed residual tracking instead of disappearing into an
            # unresolved entity mention.
            return True
        if self._find_aliases(raw_text, self._ranking_field_aliases) or re.search(
            r"(?:편입한|편입된|보유한|추종하는|운용하는|발행한|상장된|상장한|\s중(?:에서)?\b)",
            raw_text,
            re.IGNORECASE,
        ):
            return True
        if re.search(r"(?:^|\s)(?:안전한|좋은|유망한)\s+(?:ETF|ETN|펀드|상품)", raw_text, re.IGNORECASE):
            return True
        product_types = self._extract_product_types(raw_text)
        if not product_types:
            return False

        # Entity extraction intentionally runs before filter extraction.  Use
        # the same deterministic filter contracts here to mask collection
        # modifiers which have already-defined semantics.  A real product
        # name still survives because any unconsumed name material remains
        # (for example ``TIGER`` and ``S&P500`` in a named ETF lookup).
        masked = list(raw_text)
        for item in self._extract_filters(raw_text, product_types):
            start, end = self._filter_span(raw_text, item)
            masked[start:end] = " " * (end - start)
        remainder = "".join(masked)
        aliases = sorted(
            {
                *self._product_type_aliases,
                *self._region_aliases,
                *self._asset_type_aliases,
                *_OFFERING_TYPE_QUERY_ALIASES,
                *_SUBSCRIPTION_STATUS_QUERY_ALIASES,
                *_FUND_ELIGIBILITY_QUERY_ALIASES,
            },
            key=len,
            reverse=True,
        )
        for alias in aliases:
            remainder = re.sub(
                re.escape(alias), " ", remainder, flags=re.IGNORECASE
            )
        remainder = re.sub(
            r"(?:을|를|이|가|은|는)?\s*"
            r"(?:제외한|제외|아닌|에\s*투자하는|투자하는)",
            " ",
            remainder,
        )
        remainder = re.sub(r"(?:또는|그리고|및|과|와|중)", " ", remainder)
        remainder = re.sub(r"[\s,./()\[\]{}-]+", "", remainder)
        return not remainder

    def _extract_relations(self, question: str) -> list[RelationMention]:
        manager = self._management_collection_match(question)
        chain = re.search(
            r"^(.+?)(?:이|가)\s*운용하는\s+(ETF|ETN).*?의\s*(기초지수|추종지수)",
            question, re.IGNORECASE,
        )
        if chain is not None:
            return [
                RelationMention(raw_text="운용하는", direction=RelationDirection.INCOMING,
                                subject_type="AssetManager", chain_id="CHAIN-1",
                                path_position=0),
                RelationMention(raw_text=chain.group(3), direction=RelationDirection.OUTGOING,
                                subject_type=chain.group(2).upper(), chain_id="CHAIN-1",
                                path_position=1),
            ]
        target_patterns = (
            (
                re.compile(r"^(.+?)\s*(상품\s*중)", re.IGNORECASE),
                "운용사",
                "FinancialProduct",
                "AssetManager",
                lambda value: _strip_korean_particle(value),
            ),
            (re.compile(
                r"^(?:검증된\s*)?(?:iShares|아이셰어즈)"
                r"(?:\s*(?:해외\s*)?ETF)?\s*(?:보유종목\s*)?(?:범위|스코프)"
                r"(?:에서)?\s*(.+?)(?:을|를)\s*보유한\s*(?:상품|ETF)",
                re.IGNORECASE,
            ), "보유한", "FinancialProduct", "Security",
             lambda value: _strip_korean_particle(value)),
            (re.compile(
                r"^(?:검증된\s*)?(?:KODEX|TIGER|iShares|아이셰어즈)"
                r"(?:\s*/\s*(?:KODEX|TIGER|iShares|아이셰어즈))*"
                r"(?:\s*(?:long[- ]?only|롱온리|호환))?\s*"
                r"(?:ETF\s*중|(?:ETF\s*)?범위(?:에서)?)\s*"
                r"(.+?)(?:을|를)\s*보유한\s*(?:상품|ETF)",
                re.IGNORECASE,
            ), "보유한", "FinancialProduct", "Security",
             lambda value: _strip_korean_particle(value)),
            (re.compile(
                r"^(.+?)(?:을|를)\s*보유한\s*"
                r"(?:(?:국내\s*/\s*해외|국내|해외)\s*)?"
                r"(?:검증된\s*KODEX(?:\s*(?:long[- ]?only|롱온리|호환))?\s*)?"
                r"(?:(?:iShares|아이셰어즈)\s*)?"
                r"(?:ETF|ETN|공모\s*펀드|펀드)",
                re.IGNORECASE,
            ), "보유한", "FinancialProduct", "Security",
             lambda value: _strip_korean_particle(value)),
            (re.compile(
                r"^(.+?)(?:을|를|이|가)\s*(?:편입한|편입된)\s*"
                r"(?:(?:국내\s*/\s*해외|국내|해외)\s*)?"
                r"(?:[^\s]+\s*)?"
                r"(?:ETF|ETN|공모\s*펀드|펀드)",
                re.IGNORECASE,
            ), "편입", "FinancialProduct", "Organization",
             lambda value: _strip_korean_particle(value)),
            (re.compile(r"^(.+?)(?:이|가)\s*발행한\s*채권"), "발행한", "Bond",
             "Organization", lambda value: _strip_korean_particle(value)),
            (re.compile(r"발행사가\s*(.+?)인\s*채권"), "발행사", "Bond", "Issuer",
             lambda value: value),
            (re.compile(
                r"^(.+?)(?:을|를)\s*추종하는\s*(?:ETF|ETN)", re.IGNORECASE
             ), "추종하는", "ExchangeTradedProduct", "Index",
             lambda value: _strip_korean_particle(value)),
            (re.compile(r"기초지수가\s*(.+?)인\s*(?:ETF|ETN)", re.IGNORECASE),
             "기초지수", "ExchangeTradedProduct", "Index", lambda value: value),
            (re.compile(r"표시통화가\s*([A-Za-z]{3})인\s*(?:ETF|ETN|채권)", re.IGNORECASE),
             "표시통화", "FinancialProduct", "Currency", lambda value: value.upper()),
            (re.compile(r"\b([A-Za-z]{3})\s*표시통화(?:인|의)?\s*(?:ETF|ETN|채권)", re.IGNORECASE),
             "표시통화", "FinancialProduct", "Currency", lambda value: value.upper()),
            (re.compile(
                r"위험등급(?:이|가)?\s*([0-9]+(?:등급)?)(?:인\s*|\s+)(?:ETF|ETN|채권)"
            ),
             "위험등급", "FinancialProduct", "RiskGrade",
             lambda value: value.removesuffix("등급")),
        )
        target_results: list[tuple[int, range, RelationMention]] = []
        if manager is not None:
            target = manager.group(1).strip()
            target_results.append((manager.start(), range(manager.start(), manager.end()), RelationMention(
                raw_text=question[manager.start(2):manager.end()], semantic_key="운용사",
                direction=RelationDirection.OUTGOING, subject_type="FinancialProduct",
                target_raw_text=target, target_type="AssetManager", target_value=target,
            )))
        for pattern, alias, subject_type, target_type, normalize in target_patterns:
            for match in pattern.finditer(question):
                if any(
                    match.start() < existing.stop and match.end() > existing.start
                    for _, existing, _ in target_results
                ):
                    continue
                target_value = normalize(match.group(1).strip())
                if alias == "편입" and re.search(
                    r"의\s*자회사$", target_value
                ):
                    # A subsidiary expression requires a reviewed two-hop
                    # organization relation; it is not a direct holding name.
                    continue
                if alias in {"보유한", "편입"}:
                    # A six-digit KRX ticker or an exchange-qualified global
                    # ticker identifies a Security.  A
                    # lexical company target denotes the issuing Organization;
                    # the planner must therefore require the reviewed two-hop
                    # Security -> Organization path.  This is syntax-driven and
                    # contains no company-name special cases.
                    target_type = (
                        "EquitySecurity"
                        if re.fullmatch(
                            r"(?:\d{6}|[A-Z]{1,5}|[A-Z]{4}:[A-Z0-9.\-]{1,16})",
                            target_value.upper(),
                        )
                        else "Organization"
                    )
                raw_relation = alias
                semantic_key = None
                if alias == "운용사" and len(match.groups()) >= 2:
                    raw_relation = match.group(2)
                    semantic_key = alias
                if alias == "편입":
                    relation_match = re.search(r"편입(?:한|된)", match.group(0))
                    if relation_match is not None:
                        raw_relation = relation_match.group(0)
                target_results.append((
                    match.start(), range(match.start(), match.end()),
                    RelationMention(raw_text=raw_relation, semantic_key=semantic_key,
                                    direction=RelationDirection.OUTGOING,
                                    subject_type=subject_type,
                                    target_raw_text=match.group(1), target_type=target_type,
                                    target_value=target_value),
                ))
        aliases = (
            ("펀드 클래스", RelationDirection.OUTGOING),
            ("판매 LOT", RelationDirection.OUTGOING),
            ("판매 로트", RelationDirection.OUTGOING),
            ("추종지수", RelationDirection.OUTGOING),
            ("기초지수", RelationDirection.OUTGOING),
            ("벤치마크", RelationDirection.OUTGOING),
            ("운용하는", RelationDirection.INCOMING),
            ("발행한", RelationDirection.INCOMING),
            ("운용사", RelationDirection.OUTGOING),
            ("발행사", RelationDirection.OUTGOING),
            ("표시통화", RelationDirection.OUTGOING),
        )
        # Bare risk-grade mentions are requested fields, not implicit Graph
        # traversals. Explicit grade targets are preserved by target_patterns
        # above and remain subject to the risk selection capability gate.
        results = [(start, item) for start, _, item in target_results]
        occupied = [span for _, span, _ in target_results]
        for alias, direction in aliases:
            for match in re.finditer(re.escape(alias), question, re.IGNORECASE):
                if any(match.start() in span for span in occupied):
                    continue
                occupied.append(range(match.start(), match.end()))
                results.append((match.start(), RelationMention(
                    raw_text=match.group(0), direction=direction)))
        return [item for _, item in sorted(results, key=lambda item: item[0])]

    @staticmethod
    def _extract_limit(question: str) -> tuple[ResultLimit | None, range | None]:
        for pattern in (r"(?:TOP|Top|top)\s*(\d+)", r"(?:상위|하위)\s*(\d+)\s*개", r"(\d+)\s*개만",
                        r"가장\s+(?:큰|낮은|높은|작은)\s*(\d+)\s*개",
                        r"(?:상품|종목)?\s*(\d+)\s*개(?:를|만|의)?(?=\s|[?.!]|$)"):
            match = re.search(pattern, question)
            if match is not None:
                return (ResultLimit(value=int(match.group(1)), raw_text=match.group(0)),
                        range(match.start(), match.end()))
        return None, None

    @staticmethod
    def _extract_product_universe(
        question: str,
    ) -> tuple[ProductUniverseUnion | None, range | None]:
        matches: list[tuple[int, int, str]] = []
        occupied: list[range] = []

        combined_ready = re.search(
            r"검증된\s*((?:(?:KODEX|TIGER|iShares|아이셰어즈)\s*/\s*)+"
            r"(?:KODEX|TIGER|iShares|아이셰어즈))\s*(?:ETF\s*)?범위",
            question, re.IGNORECASE,
        )
        if combined_ready:
            provider_scopes = {
                "kodex": "KODEX_LONG_ONLY_COMPATIBLE",
                "tiger": "TIGER_LONG_ONLY_COMPATIBLE",
                "ishares": "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
                "아이셰어즈": "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
            }
            matches.extend(
                (
                    combined_ready.start() + position, combined_ready.end(),
                    provider_scopes[provider.strip().casefold()],
                )
                for position, provider in enumerate(
                    re.split(r"\s*/\s*", combined_ready.group(1))
                )
            )
            occupied.append(range(combined_ready.start(), combined_ready.end()))

        kodex_scope = re.search(
            r"(?:검증된\s*KODEX(?:\s*(?:long[- ]?only|롱온리|호환))?\s*(?:ETF\s*)?범위|"
            r"KODEX_LONG_ONLY_COMPATIBLE)",
            question,
            re.IGNORECASE,
        )
        if kodex_scope and not any(kodex_scope.start() in span for span in occupied):
            matches.append((
                kodex_scope.start(), kodex_scope.end(),
                "KODEX_LONG_ONLY_COMPATIBLE",
            ))
            occupied.append(range(kodex_scope.start(), kodex_scope.end()))

        kodex_full = re.search(r"KODEX\s*ETF", question, re.IGNORECASE)
        if kodex_full and kodex_scope is None:
            matches.append((kodex_full.start(), kodex_full.end(), "KODEX_FULL"))
            occupied.append(range(kodex_full.start(), kodex_full.end()))

        tiger_scope = re.search(
            r"(?:검증된\s*TIGER(?:\s*(?:long[- ]?only|롱온리|호환))?\s*(?:ETF\s*)?범위|"
            r"TIGER_LONG_ONLY_COMPATIBLE)", question, re.IGNORECASE,
        )
        if tiger_scope and not any(tiger_scope.start() in span for span in occupied):
            matches.append((
                tiger_scope.start(), tiger_scope.end(), "TIGER_LONG_ONLY_COMPATIBLE",
            ))
            occupied.append(range(tiger_scope.start(), tiger_scope.end()))
        tiger_full = re.search(r"TIGER\s*ETF", question, re.IGNORECASE)
        if tiger_full and tiger_scope is None and combined_ready is None:
            matches.append((tiger_full.start(), tiger_full.end(), "TIGER_FULL"))
            occupied.append(range(tiger_full.start(), tiger_full.end()))

        ishares_scope = re.search(
            r"(?:검증된\s*)?(?:iShares|아이셰어즈)"
            r"(?:\s*(?:해외\s*ETF|ETF))?\s*(?:보유종목\s*)?(?:범위|스코프)",
            question, re.IGNORECASE,
        )
        if ishares_scope:
            matches.append((
                ishares_scope.start(), ishares_scope.end(),
                "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
            ))
            occupied.append(range(ishares_scope.start(), ishares_scope.end()))
        ishares_full = re.search(
            r"(?:iShares|아이셰어즈)\s*(?:해외\s*)?ETF",
            question, re.IGNORECASE,
        )
        if ishares_full and ishares_scope is None:
            matches.append((ishares_full.start(), ishares_full.end(), "ISHARES_US_FULL"))
            occupied.append(range(ishares_full.start(), ishares_full.end()))

        combined = re.search(r"\uad6d\ub0b4\s*/\s*\ud574\uc678\s*(ETF|ETN|ETP)", question, re.IGNORECASE)
        if combined:
            suffix = combined.group(1).upper()
            domestic = f"Domestic{suffix}"
            foreign = f"Foreign{suffix}"
            matches.extend(
                [
                    (combined.start(), combined.end(), domestic),
                    (combined.start(), combined.end(), foreign),
                ]
            )
            occupied.append(range(combined.start(), combined.end()))

        for pattern, operand in (
            (r"\uad6d\ub0b4\s*ETF", "DomesticETF"),
            (r"\ud574\uc678\s*ETF", "ForeignETF"),
            (r"\uad6d\ub0b4\s*ETN", "DomesticETN"),
            (r"\ud574\uc678\s*ETN", "ForeignETN"),
            (r"\uad6d\ub0b4\s*ETP", "DomesticETP"),
            (r"\ud574\uc678\s*ETP", "ForeignETP"),
            (r"공모\s*펀드", "PublicFund"),
        ):
            for match in re.finditer(pattern, question, re.IGNORECASE):
                if any(match.start() in span for span in occupied):
                    continue
                matches.append((match.start(), match.end(), operand))
                occupied.append(range(match.start(), match.end()))

        for match in re.finditer(r"ETP", question, re.IGNORECASE):
            if any(match.start() in span for span in occupied):
                continue
            matches.extend(
                [
                    (match.start(), match.end(), "DomesticETP"),
                    (match.start(), match.end(), "ForeignETP"),
                ]
            )
            occupied.append(range(match.start(), match.end()))

        for pattern, operand in ((r"ETF", "ETF"), (r"펀드", "Fund")):
            for match in re.finditer(pattern, question, re.IGNORECASE):
                if (
                    kodex_scope is not None
                    or tiger_scope is not None
                    or ishares_scope is not None
                    or combined_ready is not None
                ) and operand == "ETF":
                    continue
                if any(match.start() in span for span in occupied):
                    continue
                matches.append((match.start(), match.end(), operand))

        operands = list(dict.fromkeys(item[2] for item in sorted(matches)))
        has_source_scope = any(
            item in {
                "DomesticETF", "ForeignETF", "KODEX_LONG_ONLY_COMPATIBLE",
                "DomesticETN", "ForeignETN", "DomesticETP", "ForeignETP",
                "KODEX_FULL", "TIGER_LONG_ONLY_COMPATIBLE", "TIGER_FULL",
                "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS", "ISHARES_US_FULL",
            }
            for item in operands
        )
        if len(operands) < 2 and not has_source_scope:
            return None, None
        if not matches:
            return None, None
        return (
            ProductUniverseUnion(operands=operands),
            range(min(item[0] for item in matches), max(item[1] for item in matches)),
        )

    @staticmethod
    def _extract_aggregation(question: str) -> tuple[AggregationSpec | None, range | None]:
        match = re.search(r"몇\s*개(?:인가|야|인지)?|개수", question)
        if match is None:
            return None, None
        return (AggregationSpec(operator=AggregationOperator.COUNT,
                                raw_text=match.group(0)),
                range(match.start(), match.end()))

    @staticmethod
    def _extract_temporal(question: str) -> tuple[TemporalConstraint | None, range | None]:
        match = re.search(r"(\d{4})년\s*(말|초|\d{1,2}월)?\s*기준", question)
        if match is None:
            return None, None
        snapshot = f"{match.group(1)}-12-31" if match.group(2) == "말" else None
        return (TemporalConstraint(raw_text=match.group(0), requested_snapshot=snapshot),
                range(match.start(), match.end()))

    def _filter_span(
        self, question: str, item: FilterSpec, *, region_scope_span: range | None = None,
    ) -> tuple[int, int]:
        if item.field in {"만기", "만기일"}:
            for pattern in MATURITY_YEAR_PATTERNS:
                for match in pattern.finditer(question):
                    if (
                        match.group("field") in {"만기", "만기일"}
                        and match.group("year") == item.value
                    ):
                        return match.start(), match.end()
        if isinstance(item.value, TypedScalarValue):
            field_alias = {"aum": r"(?:순자산|AUM|운용규모)",
                           "expense_ratio": r"(?:총보수|보수율|운용보수)"}[item.field]
            match = re.search(field_alias + r".*?" + re.escape(item.value.raw)
                              + r"\s*(?:이상|이하|초과|미만)(?:인)?",
                              question, re.IGNORECASE)
            if match is not None:
                return match.start(), match.end()
        if item.field == "만기일" and item.operator is FilterOperator.BETWEEN:
            match = re.search(
                r"(?:만기|만기일)(?:이|가|은|는)?\s*\d{4}년(?:인|의)?",
                question,
            )
            if match is not None:
                return match.start(), match.end()
        if item.field == "asset_type":
            values = item.value if isinstance(item.value, list) else [item.value]
            if any(
                re.search(re.escape(str(value)), question, re.IGNORECASE) is None
                for value in values
            ):
                normalized_values = {str(value).casefold() for value in values}
                alias_matches = [
                    match
                    for alias in self._asset_type_aliases
                    if _normalize_asset_type_query_value(alias).casefold()
                    in normalized_values
                    for match in re.finditer(
                        re.escape(alias), question, re.IGNORECASE
                    )
                ]
                if alias_matches:
                    start = min(match.start() for match in alias_matches)
                    end = max(match.end() for match in alias_matches)
                    modifier = re.match(
                        r"(?:을|를|이|가)?\s*(?:제외한?|아닌|또는)?",
                        question[end:end + 12],
                    )
                    return (
                        start,
                        end + (modifier.end() if modifier else 0),
                    )
        special_patterns = {
            "listing_country": r"(?:미국|한국|국내)\s*(?:증시|시장|거래소)(?:에)?\s*상장(?:된|한)?",
            "currency": r"원화\s*채권",
            "trading_currency": (
                _TRADING_CURRENCY_PATTERN.pattern
            ),
            "credit_rating": r"(?:신용등급\s*)?[A-Z]{1,4}(?:[+\-0])?\s*(?:이상|이하|초과|미만)",
            "current_sale_available": r"현재\s*판매\s*가능(?:한)?",
            "current_bond_purchase_eligible": (
                r"(?:(?:현재|지금)\s*)?구매\s*가능(?:한)?"
                r"|(?:상장\s*폐지|리스팅\s*종료)"
                r"(?:\s*또는\s*(?:상장\s*폐지|리스팅\s*종료))?"
                r"\s*채권(?:을|은|는)?\s*제외"
            ),
            "bond_market_presence": r"(?:장내|장외)\s*채권",
            "has_sale_lot": (
                r"(?:미래에셋\s*)?(?:판매조건|판매\s*(?:LOT|로트))"
                r"(?:이|은|는|가)?\s*(?:있는|없지만|없는)"
            ),
            "has_multiple_sale_lots": (
                r"(?:하나의\s*종목에\s*)?(?:여러|복수)\s*"
                r"(?:판매조건|판매\s*(?:LOT|로트))(?:이|은|는|가)?\s*있는"
            ),
            "has_trade_price_and_buy_yield_sale_lot": (
                r"매매단가(?:와|과)\s*수익률(?:이|가)?\s*제공된"
            ),
            "current_etp_sale_eligible": r"(?:\ud604\uc7ac|\uc9c0\uae08)?\s*(?:\uad6c\ub9e4|\ub9e4\uc218|\ud310\ub9e4)\s*(?:\uac00\ub2a5|\uc911)(?:\ud558\uc9c0\ub9cc|\ud55c|\uc778)?\s*(?:\uad6d\ub0b4|\ud574\uc678)?\s*(?:ETF|ETN|ETP|\uc0c1\uc7a5\uc9c0\uc218\w*)?",
            "etp_trading_status": r"\uac70\ub798\s*\uc815\uc9c0(?:\uac00|\ub294)?\s*\uc544\ub2cc|\uac70\ub798\uc815\uc9c0\uac00\s*\uc544\ub2cc|\uac70\ub798\uc815\uc9c0\s*\uc81c\uc678",
            "etp_listing_ended": r"\uc0c1\uc7a5\s*(?:\uc885\ub8cc|\ud3d0\uc9c0).*?\uc81c\uc678",
            "latest_etp_price_available": r"\ucd5c\uc2e0\s*(?:\uac00\uaca9|\uc885\uac00)(?:\uc774|\uac00)?\s*(?:\uc788\ub294|\ubcf4\uc720)",
            "stale_etp_price_warning": r"(?:\uac00\uaca9|\uc885\uac00)(?:\uc774|\uac00)?\s*\uc624\ub798\ub41c|\uc624\ub798\ub41c\s*(?:\uac00\uaca9|\uc885\uac00)",
            "etp_insufficient_info": r"\uc815\ubcf4(?:\uac00)?\s*\ubd80\uc871(?:\ud55c|\ud574)?(?:\s*\ucd94\ucc9c\ud558\uae30\s*\uc5b4\ub824\uc6b4)?|\ucd94\ucc9c\ud558\uae30\s*\uc5b4\ub824\uc6b4",
            "current_fund_subscription_eligible": r"(?:(?:(?:현재|지금)\s*)?(?:미래에셋(?:에서)?\s*)?(?:가입|신규\s*가입|추가매수)(?:할\s*수\s*있는|\s*가능(?:한)?)|미래에셋(?:에서)?\s*판매\s*중(?:인)?)",
            "subscription_status": (
                _SUBSCRIPTION_STATUS_FUND_PATTERN.pattern
                + r"(?:는|를)?(?:\s*제외)?"
            ),
            "latest_fund_price_available": r"최신\s*기준가(?:가)?\s*(?:있는|보유)",
            "bond_type": "|".join(
                re.escape(value)
                for value in sorted(self._bond_type_aliases, key=len, reverse=True)
            ),
            "market_scope": (
                r"시장범위(?:가|는|은)?\s*(?:국내외혼합|국내|해외)(?:인|인\s+상품)?"
                r"|(?:국내외혼합|국내|해외)\s*시장범위(?:인)?"
            ),
        }
        if item.field in special_patterns:
            match = re.search(special_patterns[item.field], question, re.IGNORECASE)
            if match is not None:
                return match.start(), match.end()
        values = item.value if isinstance(item.value, list) else [item.value]
        spans = []
        for value in values:
            matches = list(re.finditer(re.escape(str(value)), question, re.IGNORECASE))
            if item.field == "region" and region_scope_span is not None:
                matches = [match for match in matches if match.start() not in region_scope_span]
            spans.append((matches[0].start(), matches[0].end()) if matches else _find_span(question, str(value)))
        start, end = min(value[0] for value in spans), max(value[1] for value in spans)
        modifier = re.match(r"(?:을|를|이|가)?\s*(?:제외한?|아닌|또는)?",
                            question[end:end + 12])
        return start, end + (modifier.end() if modifier else 0)

    @staticmethod
    def _relation_span(question: str, item: RelationMention) -> tuple[int, int]:
        if item.target_raw_text is not None:
            relation_start, relation_end = _find_span(question, item.raw_text)
            target_start, target_end = _find_span(question, item.target_raw_text)
            suffix = re.search(r"인", question[target_end:target_end + 4])
            return (
                min(relation_start, target_start),
                max(relation_end, target_end + (suffix.end() if suffix else 0)),
            )
        return _find_span(question, item.raw_text)

    def _is_complex_structured_boolean(self, question: str) -> bool:
        if "또는" not in question:
            return False
        return (len(self._find_aliases(question, self._product_type_aliases)) > 1
                and len(self._find_aliases(question, self._region_aliases)) > 1)

    def _unparsed_material_spans(self, question: str,
                                 occupied: list[range]) -> list[UnparsedMaterialSpan]:
        masked = list(question)
        for span in occupied:
            for index in span:
                if 0 <= index < len(masked):
                    masked[index] = " "
        remainder = "".join(masked)
        results: list[UnparsedMaterialSpan] = []
        token_pattern = re.compile(
            r"[A-Za-z][A-Za-z0-9&+.-]*|[가-힣]+|\d+(?:,\d+)*(?:\.\d+)?%?"
        )
        ignored = {value.casefold() for value in self._non_material_tokens}
        for match in token_pattern.finditer(remainder):
            if match.group(0).casefold() in ignored:
                continue
            if re.fullmatch(
                r"것|함께|(?:과|와)함께|(?:을|를|은|는|이|가|도)?(?:알려줘|보여줘|찾아줘|추천해줘)",
                match.group(),
            ):
                continue
            results.append(UnparsedMaterialSpan(
                source_span=SourceSpan(start=match.start(), end=match.end()),
                raw_text=question[match.start():match.end()],
            ))
        return results

    def _semantic_query_text(
        self,
        question: str,
        structured_spans: list[range],
    ) -> str:
        """Keep only the semantic residual after lossless structured parsing."""
        masked = list(question)
        for span in structured_spans:
            for index in span:
                if 0 <= index < len(masked):
                    masked[index] = " "
        remainder = "".join(masked)
        token_pattern = re.compile(
            r"[A-Za-z][A-Za-z0-9&+.-]*|[가-힣]+|\d+(?:,\d+)*(?:\.\d+)?%?"
        )
        ignored = {value.casefold() for value in self._non_material_tokens}
        tokens: list[str] = []
        for match in token_pattern.finditer(remainder):
            token = match.group(0)
            if token.casefold() in ignored:
                continue
            if re.fullmatch(r"[가-힣]+", token):
                for suffix in ("을", "를", "은", "는", "과", "와", "이", "가"):
                    if token.endswith(suffix) and len(token) > len(suffix):
                        token = token[: -len(suffix)]
                        break
            if token.casefold() not in ignored:
                tokens.append(token)
        return " ".join(tokens) or question

    def _find_aliases(
        self,
        question: str,
        aliases: tuple[str, ...],
        *,
        excluded_spans: list[range] | None = None,
    ) -> list[str]:
        matches: list[tuple[int, int, str]] = []
        for alias in aliases:
            for match in re.finditer(re.escape(alias), question, re.IGNORECASE):
                start, end = match.span()
                if any(start >= span.start and end <= span.stop
                       for span in excluded_spans or []):
                    continue
                if any(start >= existing_start and end <= existing_end
                       for existing_start, existing_end, _ in matches):
                    continue
                matches = [item for item in matches
                           if not (item[0] >= start and item[1] <= end)]
                matches.append((start, end, match.group(0)))
        return self._deduplicate(value for _, _, value in sorted(matches))

    @staticmethod
    def _deduplicate(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        normalized_seen: set[str] = set()
        for value in values:
            normalized = value.casefold()
            if normalized not in normalized_seen:
                normalized_seen.add(normalized)
                result.append(value)
        return result


def _materialize_constraints(
    drafts: list[_Draft],
) -> tuple[list[SemanticConstraint], dict[tuple[str, int], str]]:
    constraints: list[SemanticConstraint] = []
    refs: dict[tuple[str, int], str] = {}
    ordered = sorted(enumerate(drafts),
                     key=lambda item: (item[1].start, item[1].end, item[0]))
    for number, (_, draft) in enumerate(ordered, start=1):
        constraint_id = f"C{number}"
        constraints.append(SemanticConstraint(
            constraint_id=constraint_id,
            source_span=SourceSpan(start=draft.start, end=draft.end),
            raw_text=draft.raw_text, semantic_type=draft.semantic_type,
            status=draft.status, payload=draft.payload,
            unsupported_reason=draft.unsupported_reason,
        ))
        if draft.ref is not None:
            refs[draft.ref] = constraint_id
    return constraints, refs


def _boolean_expression(filters: list[FilterSpec],
                        constraints: list[SemanticConstraint]) -> BooleanExpression | None:
    nodes = [BooleanExpression(node_type=BooleanNodeType.PREDICATE,
                               constraint_id=item.constraint_id)
             for item in constraints
             if item.semantic_type is ConstraintSemanticType.PRODUCT_TYPE]
    for item in filters:
        if item.constraint_id is None:
            continue
        predicate = BooleanExpression(node_type=BooleanNodeType.PREDICATE,
                                      constraint_id=item.constraint_id)
        nodes.append(predicate)
    if not nodes:
        return None
    if len(nodes) == 1:
        return nodes[0]
    return BooleanExpression(node_type=BooleanNodeType.AND, children=nodes)


def _find_span(question: str, value: str) -> tuple[int, int]:
    match = re.search(re.escape(value), question, re.IGNORECASE)
    if match is None:
        raise ValueError(f"parsed material is absent from question: {value}")
    return match.start(), match.end()


def _strip_korean_particle(value: str) -> str:
    for suffix in ("으로", "에서", "에게", "의", "을", "를", "은", "는"):
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[:-len(suffix)]
    return value


def _strip_entity_type_suffix(value: str, entity_type: str) -> str:
    """Separate a grammatical type suffix from a named entity mention."""

    stripped = _strip_korean_particle(value.strip())
    if entity_type == "product":
        stripped = re.sub(
            r"\s+(?:ETF|ETN|펀드|채권|상품)$",
            "",
            stripped,
            flags=re.IGNORECASE,
        ).strip()
    return stripped

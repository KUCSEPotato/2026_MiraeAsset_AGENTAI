import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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
        "상장지수펀드", "상장지수증권", "공모 펀드", "공모펀드", "ETF", "ETN", "펀드", "채권"
    )
    _region_aliases = (
        "United States", "글로벌", "국내", "한국", "미국", "일본", "중국",
        "아시아", "인도", "USA", "Asia", "India",
    )
    _asset_type_aliases = (
        "Commodity", "Mixed Assets", "Money Market", "Alternatives", "주식형",
        "채권형", "혼합자산", "채권혼합", "주식혼합", "단기자금", "대체자산", "부동산", "원자재",
        "Equity", "주식", "Bond", "채권", "통화", "기타",
    )
    _field_aliases = (
        "운용규모", "순자산", "AUM", "총보수", "보수율", "기준가격", "NAV",
        "가격", "티커", "ticker", "ISIN",
    )
    _semantic_markers = (
        "관련", "전략", "친환경", "폭넓게", "테마", "산업", "혁신형",
        "covered call",
    )
    _descending_words = {"큰", "높은", "많은"}
    _ascending_words = {"낮은", "작은", "적은"}
    _non_material_tokens = {
        "에", "에는", "에서", "에게", "으로", "중", "중에서", "의", "이", "가",
        "은", "는", "을", "를", "와", "과", "그리고", "모두", "만", "어떤",
        "상품", "상품을", "상품은", "상품이", "알려줘", "찾아줘", "보여줘", "정보",
        "정보를", "조회", "있어", "있는", "가진", "투자", "투자하는", "투자한", "관련된",
        "해줘", "인가", "기준",
        "비교", "비교해줘", "추천", "추천해줘", "클래스",
    }

    async def analyze(self, question: str) -> ParsedQuery:
        entities = self._extract_entity_mentions(question)
        entity_spans = [range(*_find_span(question, item.raw_text)) for item in entities]
        product_types = self._extract_product_types(question, entity_spans)
        filters = self._extract_filters(question, product_types, entity_spans)
        sort, sort_spans = self._extract_sort(question)
        requested_fields = self._extract_requested_fields(question, sort_spans)
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
        result_limit, limit_span = self._extract_limit(question)
        aggregation, aggregation_span = self._extract_aggregation(question)
        temporal, temporal_span = self._extract_temporal(question)
        intent = self._classify_intent(question, entities)
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
            start, end = _find_span(question, value)
            add(start, end, ConstraintSemanticType.PRODUCT_TYPE,
                payload={"value": value}, ref=("product_type", index))

        for index, item in enumerate(filters):
            start, end = self._filter_span(question, item)
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
            start, end = _find_span(question, value)
            add(start, end, ConstraintSemanticType.REQUESTED_FIELD,
                payload={"field": value}, ref=("requested_field", index))

        for index, item in enumerate(entities):
            start, end = _find_span(question, item.raw_text)
            add(start, end, ConstraintSemanticType.ENTITY,
                payload={"entity_type": item.entity_type}, ref=("entity", index))

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
            ranking_field_missing = (
                result_limit.raw_text.startswith(("상위", "가장")) and not sort
            )
            add(
                limit_span.start,
                limit_span.stop,
                ConstraintSemanticType.LIMIT,
                payload={"value": result_limit.value},
                ref=("limit", 0),
                status=(
                    ConstraintStatus.UNSUPPORTED
                    if ranking_field_missing
                    else ConstraintStatus.PARSED
                ),
                reason=("ranking_field_missing" if ranking_field_missing else None),
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
        unsupported_intent = intent in {
            QueryIntent.COMPARE_PRODUCTS, QueryIntent.RECOMMEND_PRODUCT,
            QueryIntent.UNKNOWN,
        }
        add(
            intent_start, intent_end, ConstraintSemanticType.INTENT,
            raw_text=intent.value, payload={"intent": intent.value}, ref=("intent", 0),
            status=(ConstraintStatus.UNSUPPORTED if unsupported_intent
                    else ConstraintStatus.PARSED),
            reason=("intent_execution_not_implemented" if unsupported_intent else None),
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
            add(
                residual.source_span.start, residual.source_span.end,
                ConstraintSemanticType.SEMANTIC, raw_text=residual.raw_text,
                payload={"query_text": residual.raw_text},
                status=ConstraintStatus.UNSUPPORTED,
                reason="unparsed_material_clause",
            )

        constraints, ref_ids = _materialize_constraints(drafts)
        filters = [item.model_copy(update={"constraint_id": ref_ids.get(("filter", i))})
                   for i, item in enumerate(filters)]
        sort = [item.model_copy(update={"constraint_id": ref_ids.get(("sort", i))})
                for i, item in enumerate(sort)]
        entities = [item.model_copy(update={"constraint_id": ref_ids.get(("entity", i))})
                    for i, item in enumerate(entities)]
        relations = [
            item.model_copy(update={"constraint_id": ref_ids.get(("relation", i))})
            for i, item in enumerate(relations)
        ]
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
        return ParsedQuery(
            original_question=question, intent=intent, product_types=product_types,
            entities=entities, filters=filters, relations=relations, sort=sort,
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
        )

    def _classify_intent(self, question: str, entities: list[EntityMention]) -> QueryIntent:
        if "비교" in question:
            return QueryIntent.COMPARE_PRODUCTS
        if "추천" in question:
            return QueryIntent.RECOMMEND_PRODUCT
        if any(token in question for token in ("알려줘", "찾아줘", "보여줘", "있어")):
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
        matches = [
            value for value in matches
            if not re.search(rf"{re.escape(value)}(?:이|가)?\s*(?:아닌|제외)",
                             question, re.IGNORECASE)
        ]
        non_bond_product = any(
            value.upper() in {"ETF", "ETN"}
            or value in {"상장지수펀드", "상장지수증권"}
            or "펀드" in value for value in matches
        )
        if non_bond_product:
            matches = [value for value in matches if value != "채권"]
        return matches

    def _extract_filters(
        self,
        question: str,
        product_types: list[str],
        excluded_spans: list[range] | None = None,
    ) -> list[FilterSpec]:
        filters: list[FilterSpec] = []
        regions = self._find_aliases(
            question, self._region_aliases, excluded_spans=excluded_spans,
        )
        assets = self._find_aliases(
            question, self._asset_type_aliases, excluded_spans=excluded_spans,
        )
        if "표시통화" in question:
            assets = [value for value in assets if value != "통화"]
        if product_types == ["채권"]:
            assets = [value for value in assets if value != "채권"]
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
                filters.append(FilterSpec(field="asset_type", operator="ne", value=negated))
            elif len(assets) > 1 and "또는" in question:
                filters.append(FilterSpec(field="asset_type", operator="in", value=assets))
            else:
                filters.append(FilterSpec(field="asset_type", operator="eq", value=assets[0]))
        for value in self._find_aliases(
            question, self._product_type_aliases, excluded_spans=excluded_spans,
        ):
            if re.search(rf"{re.escape(value)}(?:이|가)?\s*(?:아닌|제외)",
                         question, re.IGNORECASE):
                filters.append(FilterSpec(field="product_type", operator="ne", value=value))
        if re.search(r"공모\s*펀드", question, re.IGNORECASE):
            filters.append(
                FilterSpec(field="offering_type", operator="eq", value="공모")
            )
        filters.extend(self._extract_numeric_filters(question))
        return filters

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
        adjective_pattern = "큰|높은|많은|낮은|작은|적은|크고|높고|많고|낮고|작고|적고"
        for alias in self._field_aliases:
            pattern = re.compile(
                rf"({re.escape(alias)})(?:이|가|은|는)?\s*(?:가장\s*)?"
                rf"({adjective_pattern})(?:고|며)?", re.IGNORECASE,
            )
            for match in pattern.finditer(question):
                direction = (
                    "desc"
                    if match.group(2) in self._descending_words
                    or match.group(2) in {"크고", "높고", "많고"}
                    else "asc"
                )
                found.append((match.start(), SortSpec(field=match.group(1), direction=direction),
                              range(match.start(), match.end())))
        found.sort(key=lambda item: item[0])
        return [item[1] for item in found], [item[2] for item in found]

    def _extract_requested_fields(self, question: str, sort_spans: list[range]) -> list[str]:
        requested: list[tuple[int, str]] = []
        for alias in self._field_aliases:
            for match in re.finditer(re.escape(alias), question, re.IGNORECASE):
                if any(match.start() in span for span in sort_spans):
                    continue
                if self._inside_numeric_condition(question, match.start()):
                    continue
                requested.append((match.start(), match.group(0)))
        return self._deduplicate(value for _, value in sorted(requested))

    @staticmethod
    def _inside_numeric_condition(question: str, position: int) -> bool:
        return bool(re.match(
            r"[^\d]{0,8}[\d,]+(?:\.\d+)?\s*(?:%|원|억|조)\s*"
            r"(?:이상|이하|초과|미만)", question[position:position + 40]))

    def _extract_entity_mentions(self, question: str) -> list[EntityMention]:
        candidates: list[tuple[int, int, str, str]] = []
        patterns = (
            (
                r"^(.+?)\s+(?:펀드\s*)?클래스\s+정보(?:를)?\s*(?:알려줘|보여줘|조회)",
                "fund_share_class",
            ),
            (r"^(.+?)(?:에는|에)\s*어떤\s*(?:펀드\s*)?클래스", "fund"),
            (r"^(.+?)(?:이|가)\s*운용하는", "management_company"),
            (r"^(.+?)의\s*(?:운용사|발행사|기초지수|추종지수|벤치마크)", "product"),
            (
                r"^(.+?)의\s*(?:운용규모|순자산|AUM|총보수|보수율|"
                r"기준가격|NAV|가격|티커|ticker|ISIN)",
                "product",
            ),
            (r"^(.+?)의\s*(?:판매\s*LOT|판매\s*로트)", "product"),
            (r"^(.+?)\s+정보(?:를)?\s*(?:알려줘|보여줘|조회)", "product"),
            (r"[A-Za-z0-9가-힣]+자산운용", "management_company"),
        )
        for pattern, entity_type in patterns:
            for match in re.finditer(pattern, question, re.IGNORECASE):
                raw_text = match.group(1) if match.lastindex else match.group(0)
                candidates.append((match.start(), match.end(), raw_text.strip(), entity_type))
        accepted: list[tuple[int, int, str, str]] = []
        for candidate in sorted(candidates):
            start, end, raw_text, entity_type = candidate
            if entity_type == "product" and self._is_structured_product_expression(
                raw_text
            ):
                continue
            if any(start < other_end and end > other_start
                   for other_start, other_end, *_ in accepted):
                continue
            accepted.append(candidate)
        return [EntityMention(raw_text=_strip_korean_particle(raw_text),
                              entity_type=entity_type)
                for _, _, raw_text, entity_type in accepted]

    def _is_structured_product_expression(self, raw_text: str) -> bool:
        """Distinguish collection constraints from a named product prefix."""
        if not self._find_aliases(raw_text, self._product_type_aliases):
            return False

        remainder = raw_text
        aliases = sorted(
            {
                *self._product_type_aliases,
                *self._region_aliases,
                *self._asset_type_aliases,
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
        for pattern, alias, subject_type, target_type, normalize in target_patterns:
            for match in pattern.finditer(question):
                target_results.append((
                    match.start(), range(match.start(), match.end()),
                    RelationMention(raw_text=alias, direction=RelationDirection.OUTGOING,
                                    subject_type=subject_type,
                                    target_raw_text=match.group(1), target_type=target_type,
                                    target_value=normalize(match.group(1).strip())),
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
            ("위험등급", RelationDirection.OUTGOING),
        )
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
        for pattern in (r"상위\s*(\d+)\s*개", r"(\d+)\s*개만",
                        r"가장\s+(?:큰|낮은|높은|작은)\s*(\d+)\s*개"):
            match = re.search(pattern, question)
            if match is not None:
                return (ResultLimit(value=int(match.group(1)), raw_text=match.group(0)),
                        range(match.start(), match.end()))
        return None, None

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

    def _filter_span(self, question: str, item: FilterSpec) -> tuple[int, int]:
        if isinstance(item.value, TypedScalarValue):
            field_alias = {"aum": r"(?:순자산|AUM|운용규모)",
                           "expense_ratio": r"(?:총보수|보수율|운용보수)"}[item.field]
            match = re.search(field_alias + r".*?" + re.escape(item.value.raw)
                              + r"\s*(?:이상|이하|초과|미만)",
                              question, re.IGNORECASE)
            if match is not None:
                return match.start(), match.end()
        values = item.value if isinstance(item.value, list) else [item.value]
        spans = [_find_span(question, str(value)) for value in values]
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
        if item.operator is FilterOperator.NE:
            predicate = BooleanExpression(node_type=BooleanNodeType.NOT,
                                          children=[predicate])
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

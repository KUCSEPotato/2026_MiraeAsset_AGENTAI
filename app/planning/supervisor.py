import re

from app.domain.models import (
    ConstraintSemanticType,
    ConstraintStatus,
    GroundedQuery,
    PlannerType,
    QueryOperation,
    QueryPlan,
    QueryStep,
    ResolutionStatus,
    RetrievalSource,
    GroundingStatus,
    RelationDirection,
)
from app.planning.serialization import has_structured_inputs, structured_query_inputs


class DeterministicSupervisorPlanner:
    """Produce one structured plan; it never executes tools or retrievers."""

    def __init__(self, *, candidate_limit: int = 10_000) -> None:
        self._candidate_limit = candidate_limit

    async def create_plan(self, query: GroundedQuery) -> QueryPlan:
        steps: list[QueryStep] = []
        semantic_step_id: str | None = None
        structured_inputs = structured_query_inputs(query)
        constraint_ids = _constraint_ids(query)

        semantic_search = bool(
            query.parsed_query.requires_semantic_search
            or query.parsed_query.semantic_terms
            or query.unresolved_concepts
        )
        restrict_semantic_candidates = bool(
            structured_inputs["filters"]
            or structured_inputs["entity_ids"]
        )
        needs_rdb_fields = bool(
            structured_inputs["sort"] or structured_inputs["requested_fields"]
        )
        has_resolved_relations = any(
            relation.status is GroundingStatus.RESOLVED
            and relation.canonical_relation is not None
            for relation in query.grounded_relations
        )
        relation_only_product_identity = bool(
            has_resolved_relations
            and structured_inputs["entity_ids"]
            and not structured_inputs["product_types"]
            and not structured_inputs["filters"]
            and not needs_rdb_fields
        )
        relation_domain_covers_product_type = _relation_domain_covers_product_type(
            query
        )
        needs_rdb = has_structured_inputs(structured_inputs) and (
            not semantic_search
            or restrict_semantic_candidates
            or needs_rdb_fields
        ) and not relation_only_product_identity and not relation_domain_covers_product_type
        if needs_rdb:
            rdb_inputs = dict(structured_inputs)
            if semantic_search or has_resolved_relations:
                rdb_inputs["limit"] = self._candidate_limit
            elif structured_inputs["result_limit"] is not None:
                rdb_inputs["limit"] = structured_inputs["result_limit"]
            steps.append(
                QueryStep(
                    step_id="rdb-candidates",
                    source=RetrievalSource.RDB,
                    operation=QueryOperation.SEARCH_PRODUCTS,
                    inputs=rdb_inputs,
                    covers_constraint_ids=_ids_for(
                        constraint_ids,
                        {
                            ConstraintSemanticType.PRODUCT_TYPE,
                            ConstraintSemanticType.FILTER,
                            ConstraintSemanticType.SORT,
                            ConstraintSemanticType.REQUESTED_FIELD,
                            ConstraintSemanticType.ENTITY,
                        },
                    ),
                )
            )

        if semantic_search:
            raw_terms = (
                query.parsed_query.semantic_terms or query.unresolved_concepts
            )
            terms = _extract_semantic_terms(raw_terms)
            source = (
                RetrievalSource.BM25
                if _is_lexical_strategy_query(terms)
                else RetrievalSource.VECTOR
            )
            semantic_inputs = {
                "query_terms": terms,
                "metadata_filters": _semantic_metadata_filters(structured_inputs),
            }
            if structured_inputs["result_limit"] is not None:
                semantic_inputs["top_k"] = structured_inputs["result_limit"]
            dependencies: list[str] = []
            if needs_rdb and restrict_semantic_candidates:
                dependencies = ["rdb-candidates"]
                semantic_inputs["candidate_ids_from"] = dependencies
            semantic_step_id = (
                "bm25-strategy"
                if source is RetrievalSource.BM25
                else "vector-semantic"
            )
            steps.append(
                QueryStep(
                    step_id=semantic_step_id,
                    source=source,
                    operation=QueryOperation.SEMANTIC_SEARCH,
                    inputs=semantic_inputs,
                    depends_on=dependencies,
                    covers_constraint_ids=_ids_for(
                        constraint_ids,
                        {
                            ConstraintSemanticType.SEMANTIC,
                            *(
                                {ConstraintSemanticType.PRODUCT_TYPE}
                                if not needs_rdb
                                else set()
                            ),
                        },
                    ),
                )
            )

        graph_paths = _graph_paths(query)
        resolved_source_ids = [
            entity.canonical_id
            for entity in query.resolved_entities
            if entity.resolution_status is ResolutionStatus.RESOLVED
            and entity.canonical_id is not None
            and (
                entity.entity_type in {"product", "fund"}
                or any(
                    path["directions"][0]
                    == RelationDirection.INCOMING.value
                    for path in graph_paths
                )
            )
        ]
        graph_dependencies = (
            [semantic_step_id]
            if semantic_step_id is not None
            else ["rdb-candidates"]
            if needs_rdb
            else []
        )
        has_target_anchor = any(
            any(value is not None for value in path["target_values"])
            for path in graph_paths
        )
        if graph_paths and (
            resolved_source_ids or graph_dependencies or has_target_anchor
        ):
            graph_limit = structured_inputs["result_limit"]
            steps.append(
                QueryStep(
                    step_id="graph-relations",
                    source=RetrievalSource.GRAPH,
                    operation=QueryOperation.RELATIONSHIP_SEARCH,
                    inputs={
                        "paths": graph_paths,
                        "source_node_ids": resolved_source_ids,
                        "candidate_ids_from": graph_dependencies,
                        **({"limit": graph_limit} if graph_limit is not None else {}),
                    },
                    depends_on=graph_dependencies,
                    covers_constraint_ids=[
                        constraint_id
                        for path in graph_paths
                        for constraint_id in path["constraint_ids"]
                    ]
                    + (
                        _ids_for(
                            constraint_ids,
                            {ConstraintSemanticType.PRODUCT_TYPE},
                        )
                        if relation_domain_covers_product_type
                        else []
                    )
                    + _ids_for(
                        constraint_ids,
                        {ConstraintSemanticType.ENTITY},
                    ),
                )
            )

        unresolved_mentions = [
            entity.raw_text
            for entity in query.resolved_entities
            if entity.resolution_status
            in {ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS}
        ]
        if unresolved_mentions:
            steps.append(
                QueryStep(
                    step_id="bm25-entity-lookup",
                    source=RetrievalSource.BM25,
                    operation=QueryOperation.SEMANTIC_SEARCH,
                    inputs={"entity_mentions": unresolved_mentions},
                    covers_constraint_ids=_ids_for(
                        constraint_ids,
                        {ConstraintSemanticType.ENTITY},
                    ),
                )
            )

        if not steps:
            steps.append(
                QueryStep(
                    step_id="bm25-question-search",
                    source=RetrievalSource.BM25,
                    operation=QueryOperation.SEMANTIC_SEARCH,
                    inputs={"query_terms": [query.parsed_query.original_question]},
                )
            )

        retrieval_step_ids = [step.step_id for step in steps]
        if len(retrieval_step_ids) > 1:
            has_sort = bool(structured_inputs["sort"])
            steps.append(
                QueryStep(
                    step_id=(
                        "rank-candidates" if has_sort else "filter-candidates"
                    ),
                    source=RetrievalSource.INTERNAL,
                    operation=(
                        QueryOperation.RANK_CANDIDATES
                        if has_sort
                        else QueryOperation.FILTER_CANDIDATES
                    ),
                    inputs={
                        "sort": structured_inputs["sort"],
                        "limit": structured_inputs["result_limit"],
                    },
                    depends_on=retrieval_step_ids,
                )
            )

        terminal_ids = _ids_for(
            constraint_ids,
            {ConstraintSemanticType.INTENT, ConstraintSemanticType.LIMIT},
        )
        if terminal_ids:
            terminal = steps[-1]
            steps[-1] = terminal.model_copy(
                update={
                    "covers_constraint_ids": list(
                        dict.fromkeys(
                            [*terminal.covers_constraint_ids, *terminal_ids]
                        )
                    )
                }
            )

        return QueryPlan(
            planner=PlannerType.SUPERVISOR,
            steps=steps,
            unsupported_constraint_ids=[
                item.constraint_id
                for item in query.semantic_constraints
                if item.status is ConstraintStatus.UNSUPPORTED
            ],
            constraint_coverage_required=bool(query.semantic_constraints),
        )


def _constraint_ids(query: GroundedQuery) -> dict[ConstraintSemanticType, list[str]]:
    result: dict[ConstraintSemanticType, list[str]] = {}
    for item in query.semantic_constraints:
        result.setdefault(item.semantic_type, []).append(item.constraint_id)
    return result


def _ids_for(
    values: dict[ConstraintSemanticType, list[str]],
    kinds: set[ConstraintSemanticType],
) -> list[str]:
    return [item for kind in kinds for item in values.get(kind, [])]


def _graph_paths(query: GroundedQuery) -> list[dict]:
    resolved = [
        relation
        for relation in query.grounded_relations
        if relation.status is GroundingStatus.RESOLVED
        and relation.canonical_relation is not None
    ]
    paths: list[dict] = []
    chained: dict[str, list] = {}
    for relation in resolved:
        if relation.chain_id is None:
            paths.append(_path([relation]))
        else:
            chained.setdefault(relation.chain_id, []).append(relation)
    for chain_id in sorted(chained):
        relations = sorted(
            chained[chain_id],
            key=lambda item: item.path_position if item.path_position is not None else 0,
        )
        paths.append(_path(relations))
    return paths


def _path(relations: list) -> dict:
    return {
        "relations": [item.canonical_relation for item in relations],
        "directions": [item.direction.value for item in relations],
        "raw_relations": [item.raw_text for item in relations],
        "target_values": [item.target_value for item in relations],
        "target_types": [item.target_type for item in relations],
        "constraint_ids": [
            item.constraint_id
            for item in relations
            if item.constraint_id is not None
        ],
    }


def _relation_domain_covers_product_type(query: GroundedQuery) -> bool:
    return query.parsed_query.product_types == ["채권"] and any(
        relation.canonical_relation == "issuedBy"
        and relation.target_value is not None
        and relation.status is GroundingStatus.RESOLVED
        for relation in query.grounded_relations
    )


def _semantic_metadata_filters(structured_inputs: dict) -> dict:
    filters = {
        "source_dataset": ["foreign_etf"],
        "source_field": ["product.strategy_description"],
    }
    if structured_inputs["product_types"]:
        filters["product_type"] = structured_inputs["product_types"]
    for item in structured_inputs["filters"]:
        field = item.get("canonical_field")
        value = item.get("canonical_value")
        if field == "product.region" and value is not None:
            filters["region"] = [value]
        elif field == "product.asset_type" and value is not None:
            filters["asset_type"] = [value]
    return filters


def _is_lexical_strategy_query(terms: list[str]) -> bool:
    words = re.findall(r"[A-Za-z]{3,}", " ".join(terms))
    ignored = {"etf", "etn", "find", "strategy"}
    return len([word for word in words if word.casefold() not in ignored]) >= 2


def _extract_semantic_terms(terms: list[str]) -> list[str]:
    extracted: list[str] = []
    for original in terms:
        term = original
        if " 중 " in term:
            clauses = term.split(" 중 ")
            term = next(
                (
                    clause
                    for clause in clauses
                    if any(
                        marker in clause
                        for marker in (
                            "관련",
                            "전략",
                            "산업",
                            "테마",
                            "친환경",
                            "혁신",
                        )
                    )
                ),
                clauses[-1],
            )
        else:
            product_match = re.search(r"(?:ETF|ETN)", term, re.IGNORECASE)
            if product_match is not None:
                term = term[: product_match.start()]
        term = re.sub(
            r"(?:상품을?\s*)?(?:알려줘|찾아줘|보여줘|추천해줘)[.!?]?\s*$",
            "",
            term,
        )
        term = re.sub(r"(?:ETF|ETN)", " ", term, flags=re.IGNORECASE)
        term = re.sub(r"(?:상품|해외)(?:을|를|은|는|의)?", " ", term)
        term = re.sub(r"(?:을|를)?\s*가진\s*$", "", term)
        normalized = re.sub(r"\s+", " ", term).strip()
        extracted.append(normalized or original)
    return extracted

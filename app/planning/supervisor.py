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
from app.data.metric_capabilities import MetricCapabilityRegistry
from app.data.holdings_coverage import HoldingsCoverageRegistry


class DeterministicSupervisorPlanner:
    """Produce one structured plan; it never executes tools or retrievers."""

    def __init__(self, *, candidate_limit: int = 10_000) -> None:
        self._candidate_limit = candidate_limit

    async def create_plan(self, query: GroundedQuery) -> QueryPlan:
        steps: list[QueryStep] = []
        semantic_step_id: str | None = None
        structured_inputs = structured_query_inputs(query)
        structured_inputs, capability_unsupported = MetricCapabilityRegistry().prepare(
            structured_inputs
        )
        constraint_ids = _constraint_ids(query)
        holdings_relations = [
            relation for relation in query.grounded_relations
            if relation.canonical_relation in {"holds", "securityIssuedBy"}
        ]
        holdings_scope_ready = (
            not holdings_relations
            or HoldingsCoverageRegistry().is_ready_exact_scope(
                query.parsed_query.product_universe.operands
                if query.parsed_query.product_universe is not None
                else None
            )
        )
        coverage_unsupported = [
            relation.constraint_id
            for relation in holdings_relations
            if not holdings_scope_ready and relation.constraint_id is not None
        ]

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
        ) and not relation_only_product_identity and not (
            relation_domain_covers_product_type and not needs_rdb_fields
        )
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
                            ConstraintSemanticType.PRODUCT_UNIVERSE,
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

        graph_paths = _graph_paths(query, allow_holdings=holdings_scope_ready)
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
            # A financial Top-K belongs after every relation/filter. Restricting
            # graph paths to K here can discard the highest-ranked product.
            graph_limit = (
                self._candidate_limit if needs_rdb or semantic_search or len(graph_paths) > 1
                else structured_inputs["result_limit"]
            )
            steps.append(
                QueryStep(
                    step_id="graph-relations",
                    source=RetrievalSource.GRAPH,
                    operation=QueryOperation.RELATIONSHIP_SEARCH,
                    inputs={
                        "paths": graph_paths,
                        "source_node_ids": resolved_source_ids,
                        "candidate_ids_from": graph_dependencies,
                        "path_operator": "and",
                        "require_complete_candidates": bool(needs_rdb or semantic_search or len(graph_paths) > 1),
                        **({"result_limit": structured_inputs["result_limit"]}
                           if len(graph_paths) > 1 and not needs_rdb and not semantic_search
                           and structured_inputs["result_limit"] is not None else {}),
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

        # Routing depends on the grounded operators and their anchors. An
        # anchored graph can select IDs first; the same RDB filter/projection/
        # ordering implementation then operates on precisely that candidate set.
        if needs_rdb and needs_rdb_fields:
            graph_first = bool(
                graph_paths and not semantic_search
                and (resolved_source_ids or all(
                    any(value is not None for value in path["target_values"])
                    for path in graph_paths
                ))
            )
            semantic_first = bool(
                semantic_step_id == "bm25-strategy" and not has_resolved_relations
                and not restrict_semantic_candidates
            )
            candidate_step_id = (
                "graph-relations" if graph_first else
                semantic_step_id if semantic_first else None
            )
            if candidate_step_id is not None:
                reordered: list[QueryStep] = []
                for step in steps:
                    if step.step_id == candidate_step_id:
                        inputs = dict(step.inputs)
                        inputs.pop("candidate_ids_from", None)
                        inputs["require_complete_candidates"] = True
                        if semantic_first:
                            inputs["top_k"] = self._candidate_limit
                        reordered.insert(0, step.model_copy(update={
                            "inputs": inputs, "depends_on": [],
                        }))
                    elif step.step_id == "rdb-candidates":
                        reordered.append(step.model_copy(update={
                            "inputs": {
                                **step.inputs,
                                "candidate_ids_from": [candidate_step_id],
                            },
                            "depends_on": [candidate_step_id],
                        }))
                    else:
                        reordered.append(step)
                steps = reordered

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
            if has_sort:
                # The merge consumes the deterministic RDB ordering even when
                # another store produced that RDB step's candidate IDs.
                retrieval_step_ids.sort(key=lambda value: value != "rdb-candidates")
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
                        "require_complete_candidates": bool(has_sort or graph_paths),
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
            ] + capability_unsupported + coverage_unsupported,
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


def _graph_paths(query: GroundedQuery, *, allow_holdings: bool = True) -> list[dict]:
    resolved = [
        relation
        for relation in query.grounded_relations
        if relation.status is GroundingStatus.RESOLVED
        and relation.canonical_relation is not None
        and (
            allow_holdings
            or relation.canonical_relation not in {"holds", "securityIssuedBy"}
        )
    ]
    paths: list[dict] = []
    chained: dict[str, list] = {}
    for relation in resolved:
        if relation.chain_id is None:
            paths.extend(_allow_listed_paths(query, relation))
        else:
            chained.setdefault(relation.chain_id, []).append(relation)
    for chain_id in sorted(chained):
        relations = sorted(
            chained[chain_id],
            key=lambda item: item.path_position if item.path_position is not None else 0,
        )
        paths.append(_path(relations, query))
    return paths


def _allow_listed_paths(query: GroundedQuery, relation) -> list[dict]:
    """Expand only reviewed bounded semantic paths; never infer arbitrary hops."""

    if (
        relation.canonical_relation == "holds"
        and relation.target_value is not None
    ):
        target_value = _resolved_target_value(query, relation)
        common = {
            "raw_relations": [relation.raw_text],
            "constraint_ids": [relation.constraint_id] if relation.constraint_id else [],
        }
        if relation.target_type == "Organization":
            return [{
                **common,
                "relations": ["holds", "securityIssuedBy"],
                "directions": [
                    RelationDirection.OUTGOING.value,
                    RelationDirection.OUTGOING.value,
                ],
                "raw_relations": [relation.raw_text, "증권 발행사"],
                "target_values": [None, target_value],
                "target_types": ["EquitySecurity", "Organization"],
            }]
        if relation.target_type in {"Security", "EquitySecurity"}:
            return [{
                **common,
                "relations": ["holds"],
                "directions": [RelationDirection.OUTGOING.value],
                "target_values": [target_value],
                "target_types": ["EquitySecurity"],
            }]
        return []
    return [_path([relation], query)]


def _path(relations: list, query: GroundedQuery) -> dict:
    return {
        "relations": [item.canonical_relation for item in relations],
        "directions": [item.direction.value for item in relations],
        "raw_relations": [item.raw_text for item in relations],
        "target_values": [
            _resolved_target_value(query, item) for item in relations
        ],
        "target_types": [item.target_type for item in relations],
        "constraint_ids": [
            item.constraint_id
            for item in relations
            if item.constraint_id is not None
        ],
    }


def _resolved_target_value(query: GroundedQuery, relation) -> str | None:
    """Prefer a validated canonical relation target over a raw graph label."""

    target = relation.target_value
    if target is None:
        return None
    raw_values = {
        str(value).casefold()
        for value in (relation.target_value, relation.target_raw_text)
        if value is not None
    }
    for entity in query.resolved_entities:
        if (
            entity.resolution_status is ResolutionStatus.RESOLVED
            and entity.canonical_id is not None
            and entity.raw_text.casefold() in raw_values
        ):
            return entity.canonical_id
    return target


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

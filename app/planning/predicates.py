"""Extract a store-local predicate tree without changing Boolean meaning."""

from app.domain.models import BooleanExpression, BooleanNodeType, GroundedQuery
from app.planning.exceptions import UnsupportedQuerySemanticsError


def structured_predicate(query: GroundedQuery) -> BooleanExpression | None:
    """AND permits independent stores; OR/NOT may never drop a foreign leaf."""

    filter_ids = {
        item.raw_filter.constraint_id for item in query.grounded_filters
        if item.raw_filter.constraint_id is not None
    }
    known_ids = {item.constraint_id for item in query.semantic_constraints}

    def visit(node: BooleanExpression) -> BooleanExpression | None:
        if node.node_type is BooleanNodeType.PREDICATE:
            if node.constraint_id in filter_ids:
                return node
            if node.constraint_id not in known_ids:
                raise UnsupportedQuerySemanticsError(["unknown_boolean_predicate"])
            return None
        children = [visit(child) for child in node.children]
        retained = [child for child in children if child is not None]
        if node.node_type is not BooleanNodeType.AND:
            if len(retained) != len(children):
                raise UnsupportedQuerySemanticsError(["cross_store_boolean_unsupported"])
            if node.node_type is BooleanNodeType.NOT:
                raise UnsupportedQuerySemanticsError(["predicate_not_requires_missingness_contract"])
        if not retained:
            return None
        if len(retained) == 1:
            return retained[0]
        return BooleanExpression(node_type=node.node_type, children=retained)

    tree = query.parsed_query.boolean_expression
    return visit(tree) if tree is not None else None

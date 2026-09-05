"""Executable operator contracts, independent of question/product spellings."""

from datetime import UTC, date, datetime
import json

import pytest
from sqlalchemy import Column, Date, MetaData, Numeric, String, Table, create_engine, select
from sqlalchemy.dialects import postgresql

from app.data.metric_capabilities import MetricCapabilityRegistry, PREF01_AUM
from app.domain.models import (
    ExecutionContext, QueryPlan, QueryStep, RetrievalRecord, StepExecutionResult,
)
from app.retrieval.exceptions import IncompleteCandidateSetError, RDBQueryCompilationError
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry, CanonicalV2QueryCompiler, V2SnapshotSelection,
    V2SnapshotUnavailableError, _bind_dependency_candidates,
    _validate_candidate_snapshots,
)


SNAPSHOT = V2SnapshotSelection(
    date(2026, 8, 24), "260824", "merged-optical-1.4", ("pref01",), ("PREF01N001",),
)


def _compiler():
    return CanonicalV2QueryCompiler(CanonicalV2FieldRegistry(), default_limit=100)


def _filter(value, *, operator="eq", field="product.name"):
    return {"canonical_field": field, "canonical_value": value,
            "raw": {"field": field, "operator": operator, "value": value}}


def _leaf(identifier):
    return {"node_type": "predicate", "constraint_id": identifier}


def _step(inputs):
    return QueryStep(step_id="rdb", source="rdb", operation="search_products", inputs=inputs)


def test_nested_predicates_execute_or_then_and_without_flattening():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    products = Table("products", metadata,
                     Column("entity_id", String), Column("normalized_preferred_name", String))
    metadata.create_all(engine)
    inputs = {
        "filters": [_filter("alpha", operator="contains"),
                    _filter("beta", operator="contains"),
                    _filter("alphaold", operator="ne")],
        "filter_constraint_ids": ["a", "b", "c"],
        "boolean_expression": {"node_type": "and", "children": [
            {"node_type": "or", "children": [_leaf("a"), _leaf("b")]}, _leaf("c")
        ]},
    }
    with engine.begin() as connection:
        connection.execute(products.insert(), [
            {"entity_id": value, "normalized_preferred_name": value}
            for value in ["alpha", "beta", "alphaold", "gamma"]
        ])
        predicates = _compiler()._compile_predicates(inputs, products.c.entity_id, products, SNAPSHOT)
        result = connection.scalars(select(products.c.entity_id).where(*predicates)).all()
    assert result == ["alpha", "beta"]


def test_contains_treats_sql_wildcards_and_quotes_as_literal_data():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    products = Table("products", metadata,
                     Column("entity_id", String), Column("normalized_preferred_name", String))
    metadata.create_all(engine)
    term = "a%_'or1=1"
    inputs = {"filters": [_filter(term, operator="contains")]}
    predicates = _compiler()._compile_predicates(inputs, products.c.entity_id, products, SNAPSHOT)
    statement = select(products.c.entity_id).where(*predicates)
    assert term not in str(statement.compile())
    with engine.begin() as connection:
        connection.execute(products.insert(), [
            {"entity_id": "literal", "normalized_preferred_name": term},
            {"entity_id": "wildcard", "normalized_preferred_name": "abcdef'or1=1"},
        ])
        assert connection.scalars(statement).all() == ["literal"]


@pytest.mark.parametrize("expression,ids", [
    (_leaf("missing"), ["a", "b"]),
    ({"node_type": "or", "children": [_leaf("a"), _leaf("a")]}, ["a", "b"]),
    ({"node_type": "not", "children": [_leaf("a")]}, ["a", "b"]),
    ({"node_type": "or", "children": [_leaf("a")]}, ["a", "b"]),
    (_leaf("a"), ["a", "a"]),
    (_leaf("a"), ["a"]),
])
def test_invalid_boolean_constraints_fail_before_query_execution(expression, ids):
    with pytest.raises(RDBQueryCompilationError):
        _compiler().compile(_step({
            "filters": [_filter("alpha"), _filter("beta")],
            "filter_constraint_ids": ids, "boolean_expression": expression,
        }), SNAPSHOT)


def test_credit_rating_between_uses_approved_ordinal_scale():
    compiled = _compiler().compile(_step({
        "filters": [_filter(["AA-", "AAA"], operator="between", field="product.credit_rating")],
    }), SNAPSHOT)
    sql = compiled.statement.compile(dialect=postgresql.dialect())
    assert " BETWEEN " in str(sql)
    assert 12 in sql.params.values() and 15 in sql.params.values()


@pytest.mark.parametrize("field", ["product.expense_ratio", "product.aum", "product.six_month_return"])
def test_numeric_predicates_stay_disabled_without_filter_contract(field):
    with pytest.raises(RDBQueryCompilationError, match="filtering is semantically disabled"):
        _compiler().compile(_step({"filters": [_filter(0.5, operator="lte", field=field)]}), SNAPSHOT)


def test_multiple_comparison_fields_receive_independent_contracts():
    prepared = MetricCapabilityRegistry().verified_inputs({
        "product_universe": {"operation": "UNION", "operands": ["DomesticETF"]},
        "comparison": {"mode": "fieldwise", "fields": ["product.aum", "product.six_month_return"]},
    })
    assert [contract["canonical_field"] for contract in prepared["comparison_contracts"]] == [
        "product.aum", "product.six_month_return",
    ]
    compiled = _compiler().compile(_step(prepared), SNAPSHOT)
    assert set(compiled.projected_fields) == {"product.aum", "product.six_month_return"}


def test_ordered_vocabulary_contract_survives_json_plan_serialization():
    registry = MetricCapabilityRegistry()
    prepared, unsupported = registry.prepare({
        "comparison": {"mode": "fieldwise", "fields": ["product.risk_grade"]},
    })
    assert not unsupported
    registry.verified_inputs(json.loads(json.dumps(prepared)))


@pytest.mark.parametrize("fields,universe,reason", [
    (["product.aum", "product.expense_ratio"], ["DomesticETF"], "expense_ratio_scale_unverified"),
    (["product.one_year_return"], ["DomesticETF", "ForeignETF"], "foreign_etf_return"),
    (["product.one_year_return"], [], "product_scope_not_verified"),
    (["product.invented"], ["DomesticETF"], "ordered_comparison_not_supported"),
])
def test_comparison_rejects_any_unverified_field_even_without_constraint_ledger(fields, universe, reason):
    with pytest.raises(RDBQueryCompilationError, match=reason):
        _compiler().compile(_step({
            "product_universe": {"operation": "UNION", "operands": universe},
            "comparison": {"mode": "fieldwise", "fields": fields},
        }), SNAPSHOT)


@pytest.mark.parametrize("mutation", [
    {"scale": "invented"}, {"datasets": ["PREF01N001", "PREF02N001"]},
    {"cross_dataset_comparability": True},
])
def test_supplied_contract_cannot_expand_deterministic_authorization(mutation):
    with pytest.raises(RDBQueryCompilationError, match="unverified comparison contract"):
        _compiler().compile(_step({
            "product_universe": {"operation": "UNION", "operands": ["DomesticETF"]},
            "comparison": {"mode": "fieldwise", "fields": ["product.aum"]},
            "comparison_contracts": [{**PREF01_AUM.as_plan_input(), **mutation}],
        }), SNAPSHOT)


def test_conditional_currency_branch_cannot_authorize_global_aum_comparison():
    with pytest.raises(RDBQueryCompilationError, match="aum_scope"):
        _compiler().compile(_step({
            "filters": [_filter("KRW", field="product.currency"), _filter("US", field="product.listing_country")],
            "filter_constraint_ids": ["krw", "us"],
            "boolean_expression": {"node_type": "or", "children": [_leaf("krw"), _leaf("us")]},
            "comparison": {"mode": "fieldwise", "fields": ["product.aum"]},
        }), SNAPSHOT)


def _context(records, *, complete=1, status="success", total=None, returned=None):
    upstream = QueryStep(step_id="graph", source="graph", operation="relationship_search")
    target = _step({"candidate_ids_from": ["graph"]}).model_copy(update={"depends_on": ["graph"]})
    context = ExecutionContext(plan=QueryPlan(planner="supervisor", steps=[upstream, target]))
    context.step_results["graph"] = StepExecutionResult(
        step_id="graph", source="graph", operation="relationship_search", status=status,
        started_at=datetime.now(UTC), finished_at=datetime.now(UTC), duration_seconds=0,
        records=records, retrieval_metadata={
            "counts": {"candidate_set_complete": complete} if complete is not None else {},
            "total_matches": total, "returned_count": returned,
        },
    )
    return target, context


def _record(identifier="fixture-1", **metadata):
    return RetrievalRecord(step_id="graph", source="graph", source_id=identifier,
                           entity_id=identifier, payload={}, metadata={
                               "dataset_snapshot": "2026-08-24", "generation": "260824", **metadata,
                           })


def test_complete_graph_candidates_bind_identity_once_and_preserve_empty_scope():
    step, context = _context([_record("fixture-1"), _record("fixture-2")])
    step = step.model_copy(update={"inputs": {**step.inputs, "entity_ids": ["fixture-2"]}})
    bound = _bind_dependency_candidates(step, context)
    assert bound.inputs["entity_ids"] == ["fixture-2"]
    _validate_candidate_snapshots(bound, SNAPSHOT)
    empty_step, empty_context = _context([])
    empty = _bind_dependency_candidates(empty_step, empty_context)
    sql = str(_compiler().compile(empty, SNAPSHOT).statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "1 != 1" in sql


def test_empty_dependency_preserves_previously_resolved_domestic_entity_scope():
    step, context = _context([])
    prepared, unsupported = MetricCapabilityRegistry().prepare({
        **step.inputs, "entity_ids": ["etf_kr:fixture"],
        "comparison": {"mode": "fieldwise", "fields": ["product.one_year_return"]},
    })
    assert not unsupported
    bound = _bind_dependency_candidates(step.model_copy(update={"inputs": prepared}), context)
    assert bound.inputs["entity_ids"] == []
    _compiler().compile(bound, SNAPSHOT)


@pytest.mark.parametrize("complete,status,total,returned", [
    (0, "success", 1, 1), (None, "success", None, None),
    (None, "success", 2, 1), (1, "failed", 1, 1),
])
def test_partial_or_failed_dependency_never_becomes_unrestricted_search(complete, status, total, returned):
    step, context = _context([_record()], complete=complete, status=status, total=total, returned=returned)
    with pytest.raises(IncompleteCandidateSetError):
        _bind_dependency_candidates(step, context)


@pytest.mark.parametrize("metadata", [
    {"dataset_snapshot": "2026-08-23"}, {"generation": "old"},
    {"snapshot_identity": "old:2026-08-24"},
    {"dataset_snapshot": None, "generation": None},
])
def test_dependency_snapshot_mismatch_fails_before_rdb_execution(metadata):
    step, context = _context([_record(**metadata)])
    bound = _bind_dependency_candidates(step, context)
    with pytest.raises(V2SnapshotUnavailableError):
        _compiler().compile(bound, SNAPSHOT)


def test_metric_projection_ranking_and_provenance_select_the_same_valid_fact(monkeypatch):
    """Execute actual SQL against minimal tables with competing observations."""
    import app.retrieval.rdb_v2 as repository

    metadata = MetaData()
    schemas = {
        "canonical_entities": ["entity_id", "preferred_name"],
        "financial_products": ["product_id", "product_type_code"],
        "canonical_facts": ["fact_id", "subject_entity_id", "snapshot_id", "resolution_status"],
        "metric_definitions": ["metric_code"],
        "metric_observations": ["subject_entity_id", "metric_code", "fact_id", "raw_value", "numeric_value", "unit", "scale_basis", "currency", "observed_on", "quality_status", "comparability_status"],
        "dataset_snapshots": ["snapshot_id", "dataset_id"],
        "source_datasets": ["dataset_id", "dataset_code"],
        "fact_evidence_links": ["fact_id", "assertion_id"],
    }
    tables = {}
    for name, columns in schemas.items():
        tables[name] = Table(name, metadata, *[
            Column(column, Date if column == "observed_on" else Numeric if column == "numeric_value" else String)
            for column in columns
        ])
        monkeypatch.setattr(repository, name, tables[name])
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(tables["canonical_entities"].insert(), {"entity_id": "fixture", "preferred_name": "Fixture"})
        connection.execute(tables["financial_products"].insert(), {"product_id": "fixture", "product_type_code": "ETF"})
        connection.execute(tables["metric_definitions"].insert(), {"metric_code": "AUM"})
        connection.execute(tables["dataset_snapshots"].insert(), {"snapshot_id": "pref01", "dataset_id": "source"})
        connection.execute(tables["source_datasets"].insert(), {"dataset_id": "source", "dataset_code": "PREF01N001"})
        # Newer invalid, unresolved and unevidenced facts must not replace the
        # latest valid, resolved and supported observation.
        for identifier, value, day, quality, resolution, evidenced in [
            ("old", 10, 20, "VALID", "RESOLVED", True),
            ("latest", 25, 21, "VALID", "RESOLVED", True),
            ("invalid", 99, 22, "INVALID", "RESOLVED", True),
            ("unresolved", 200, 23, "VALID", "UNRESOLVED", True),
            ("orphan", 500, 24, "VALID", "RESOLVED", False),
        ]:
            connection.execute(tables["canonical_facts"].insert(), {
                "fact_id": identifier, "subject_entity_id": "fixture", "snapshot_id": "pref01", "resolution_status": resolution,
            })
            connection.execute(tables["metric_observations"].insert(), {
                "subject_entity_id": "fixture", "metric_code": "AUM", "fact_id": identifier,
                "raw_value": str(value), "numeric_value": value, "unit": "CURRENCY_AMOUNT",
                "scale_basis": "CURRENCY_UNIT", "currency": "KRW", "observed_on": date(2026, 8, day),
                "quality_status": quality, "comparability_status": "COMPARABLE",
            })
            if evidenced:
                connection.execute(tables["fact_evidence_links"].insert(), {"fact_id": identifier, "assertion_id": identifier + "-source"})
        contracts = [PREF01_AUM.as_plan_input()]
        projected = repository.CanonicalV2RDBRetriever._project(connection, ["fixture"], ["product.aum"], SNAPSHOT, contracts)
        details = repository.CanonicalV2RDBRetriever._metric_details(connection, ["fixture"], ["product.aum"], SNAPSHOT, contracts)
        metric = repository.CanonicalV2QueryCompiler._metric_value("fixture", "AUM", contracts[0], SNAPSHOT)
        assert projected[("fixture", "product.aum")] == connection.scalar(select(metric)) == 25
        assert details[("fixture", "product.aum")]["field_fact_id"] == "latest"
        assert details[("fixture", "product.aum")]["metric_numeric_value"] == "25.0000000000"


def test_plan_validation_rejects_omitted_comparison_and_projection_despite_coverage_labels():
    import asyncio
    from app.planning.metadata import RoutingMetadataRegistry
    from app.planning.validator import QueryPlanValidationError, StructuredQueryPlanValidator
    from tests.test_m10_9_c1_structured_operations import _plan

    _, grounded, plan = asyncio.run(_plan("국내 ETF의 수익률과 AUM을 비교해줘"))
    steps = [step.model_copy(update={"inputs": {
        key: value for key, value in step.inputs.items()
        if key not in {"comparison", "comparison_contracts", "requested_fields", "requested_field_details"}
    }}) for step in plan.steps]
    with pytest.raises(QueryPlanValidationError):
        StructuredQueryPlanValidator(RoutingMetadataRegistry()).validate(plan.model_copy(update={"steps": steps}), grounded)

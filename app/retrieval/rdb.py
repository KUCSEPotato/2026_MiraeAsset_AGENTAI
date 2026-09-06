import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, Select, asc, case, desc, select
from sqlalchemy.exc import SQLAlchemyError

from app.data.metric_capabilities import RISK_GRADE_UNVERIFIED_REASON
from app.data.schema import canonical_products, fund_classes
from app.domain.models import (
    ExecutionContext,
    FilterOperator,
    QueryOperation,
    QueryStep,
    RetrievalRecord,
    RetrievalSource,
)
from app.retrieval.exceptions import (
    RDBQueryCompilationError,
    RetrieverUnavailableError,
)
from app.ontology.storage_adapter import FundShareClassStorageAdapter


@dataclass(frozen=True)
class RDBFieldMapping:
    canonical_field: str
    column_name: str
    numeric: bool = False


class RDBFieldRegistry:
    def __init__(self) -> None:
        self._fields = {
            item.canonical_field: item
            for item in (
                RDBFieldMapping("product.name", "product_name"),
                RDBFieldMapping("product.short_name", "short_name"),
                RDBFieldMapping("product.ticker", "ticker"),
                RDBFieldMapping("product.isin", "isin"),
                RDBFieldMapping("product.asset_manager", "asset_manager"),
                RDBFieldMapping("product.issuer", "issuer"),
                RDBFieldMapping("product.product_type", "product_type"),
                RDBFieldMapping("product.region", "region"),
                RDBFieldMapping("product.asset_type", "asset_type"),
                RDBFieldMapping("product.risk_grade", "risk_grade"),
                RDBFieldMapping("product.currency", "currency"),
                RDBFieldMapping("product.aum", "aum", numeric=True),
                RDBFieldMapping(
                    "product.expense_ratio",
                    "expense_ratio",
                    numeric=True,
                ),
                RDBFieldMapping("product.nav", "nav", numeric=True),
                RDBFieldMapping("product.price", "price", numeric=True),
                RDBFieldMapping("product.base_index", "base_index"),
                RDBFieldMapping("product.observed_at", "observed_at"),
            )
        }

    def get(self, canonical_field: str) -> RDBFieldMapping:
        mapping = self._fields.get(canonical_field)
        if mapping is None:
            raise RDBQueryCompilationError(
                f"unsupported canonical RDB field: {canonical_field}"
            )
        return mapping

    @property
    def canonical_fields(self) -> frozenset[str]:
        return frozenset(self._fields)


@dataclass(frozen=True)
class CompiledRDBQuery:
    statement: Select
    projected_fields: tuple[str, ...]
    ranking_applied: bool = False


class RDBQueryCompiler:
    def __init__(
        self,
        field_registry: RDBFieldRegistry,
        *,
        default_limit: int,
        snapshot_date: str,
        max_limit: int | None = None,
    ) -> None:
        self._fields = field_registry
        self._default_limit = default_limit
        self._snapshot_date = snapshot_date
        self._max_limit = max_limit or default_limit

    def compile(self, step: QueryStep) -> CompiledRDBQuery:
        comparison = step.inputs.get("comparison")
        if (
            isinstance(comparison, dict)
            and "product.risk_grade" in comparison.get("fields", [])
        ):
            raise RDBQueryCompilationError(RISK_GRADE_UNVERIFIED_REASON)
        if step.source is not RetrievalSource.RDB:
            raise RDBQueryCompilationError("RDB compiler requires an RDB step")
        if step.operation is not QueryOperation.SEARCH_PRODUCTS:
            raise RDBQueryCompilationError(
                f"unsupported RDB operation: {step.operation.value}"
            )
        conditions = [
            canonical_products.c.dataset_snapshot == self._snapshot_date
        ]
        product_types = step.inputs.get("product_types", [])
        if product_types:
            allowed = {
                "FinancialProduct.ETF",
                "FinancialProduct.ETN",
                "FinancialProduct.Bond",
                "FinancialProduct.Fund",
                "FinancialProduct.PublicFund",
            }
            if not isinstance(product_types, list) or not set(product_types) <= allowed:
                raise RDBQueryCompilationError("unsupported product type mapping")
            conditions.append(canonical_products.c.product_type.in_(product_types))

        entity_ids = step.inputs.get("entity_ids", [])
        if entity_ids:
            if not isinstance(entity_ids, list):
                raise RDBQueryCompilationError("entity_ids must be a list")
            conditions.append(
                canonical_products.c.canonical_product_id.in_(entity_ids)
            )

        for item in step.inputs.get("filters", []):
            conditions.append(self._compile_filter(item))

        requested = list(step.inputs.get("requested_fields", []))
        sort_items = list(step.inputs.get("sort", []))
        projected = list(
            dict.fromkeys(
                [
                    *requested,
                    *(
                        item.get("canonical_field")
                        for item in sort_items
                        if isinstance(item, dict)
                    ),
                ]
            )
        )
        projected = [field for field in projected if field is not None]
        if not projected:
            projected = ["product.name"]
        for field in projected:
            self._fields.get(field)

        statement = select(canonical_products).where(*conditions)
        order_by = []
        for item in sort_items:
            if not isinstance(item, dict):
                raise RDBQueryCompilationError("sort item must be structured")
            field = item.get("canonical_field")
            if field == "product.risk_grade":
                raise RDBQueryCompilationError(RISK_GRADE_UNVERIFIED_REASON)
            raw = item.get("raw", {})
            direction = raw.get("direction") if isinstance(raw, dict) else None
            mapping = self._fields.get(field)
            column = canonical_products.c[mapping.column_name]
            if direction not in {"asc", "desc"}:
                raise RDBQueryCompilationError("unsupported sort direction")
            order_by.extend(
                [
                    case((column.is_(None), 1), else_=0),
                    desc(column) if direction == "desc" else asc(column),
                ]
            )
        order_by.append(asc(canonical_products.c.canonical_product_id))
        limit = step.inputs.get("limit", self._default_limit)
        if not isinstance(limit, int) or limit <= 0:
            raise RDBQueryCompilationError("RDB limit must be a positive integer")
        limit = min(limit, self._max_limit)
        return CompiledRDBQuery(
            statement=statement.order_by(*order_by).limit(limit),
            projected_fields=tuple(projected),
            ranking_applied=bool(sort_items),
        )

    def _compile_filter(self, item: Any):
        if not isinstance(item, dict):
            raise RDBQueryCompilationError("filter item must be structured")
        field = item.get("canonical_field")
        if field == "product.risk_grade":
            raise RDBQueryCompilationError(RISK_GRADE_UNVERIFIED_REASON)
        mapping = self._fields.get(field)
        raw = item.get("raw")
        if not isinstance(raw, dict):
            raise RDBQueryCompilationError("filter raw representation is missing")
        try:
            operator = FilterOperator(raw.get("operator"))
        except (TypeError, ValueError) as exc:
            raise RDBQueryCompilationError("unsupported filter operator") from exc
        value = item.get("canonical_value")
        if value is None:
            value = raw.get("value")
        if mapping.numeric:
            value = _numeric_value(value, operator)
        column = canonical_products.c[mapping.column_name]
        operators = {
            FilterOperator.EQ: lambda: column == value,
            FilterOperator.NE: lambda: column != value,
            FilterOperator.LT: lambda: column < value,
            FilterOperator.LTE: lambda: column <= value,
            FilterOperator.GT: lambda: column > value,
            FilterOperator.GTE: lambda: column >= value,
            FilterOperator.IN: lambda: column.in_(value),
            FilterOperator.BETWEEN: lambda: column.between(value[0], value[1]),
        }
        if operator in {FilterOperator.IN, FilterOperator.BETWEEN} and not isinstance(
            value, (list, tuple)
        ):
            raise RDBQueryCompilationError(
                f"{operator.value.upper()} filter requires a list value"
            )
        return operators[operator]()


class RealRDBRetriever:
    def __init__(self, engine: Engine, compiler: RDBQueryCompiler) -> None:
        self._engine = engine
        self._compiler = compiler

    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        del context
        compiled = self._compiler.compile(step)
        try:
            return await asyncio.to_thread(self._retrieve_sync, step, compiled)
        except SQLAlchemyError as exc:
            raise RetrieverUnavailableError("RDB retrieval failed") from exc

    def _retrieve_sync(
        self,
        step: QueryStep,
        compiled: CompiledRDBQuery,
    ) -> list[RetrievalRecord]:
        with self._engine.connect() as connection:
            rows = connection.execute(compiled.statement).mappings().all()
            fund_ids = {
                str(row["canonical_product_id"])
                for row in rows
                if row["source_dataset"] == "public_fund"
            }
            fund_class_metadata = {
                str(row["canonical_product_id"]): row
                for row in (
                    connection.execute(
                        select(fund_classes).where(
                            fund_classes.c.dataset_snapshot
                            == self._compiler._snapshot_date,
                            fund_classes.c.canonical_product_id.in_(fund_ids),
                        )
                    ).mappings()
                    if fund_ids
                    else ()
                )
            }
        records: list[RetrievalRecord] = []
        fund_adapter = FundShareClassStorageAdapter()
        for row in rows:
            semantic_identity = None
            fund_class_row = fund_class_metadata.get(
                str(row["canonical_product_id"])
            )
            if fund_class_row is not None:
                semantic_identity = fund_adapter.adapt(
                    row, fund_class_row
                ).as_dict()
            for field in compiled.projected_fields:
                mapping = self._compiler._fields.get(field)
                source_id = (
                    f"{row['source_dataset']}:{row['source_record_key']}:{field}"
                )
                records.append(
                    RetrievalRecord(
                        step_id=step.step_id,
                        source=RetrievalSource.RDB.value,
                        source_id=source_id,
                        entity_id=row["canonical_product_id"],
                        payload={
                            "field": field,
                            "value": row[mapping.column_name],
                            "text": row["product_name"],
                        },
                        metadata={
                            "source_dataset": row["source_dataset"],
                            "source_file": row["source_file"],
                            "source_row_number": row["source_row_number"],
                            "source_record_key": row["source_record_key"],
                            "dataset_snapshot": row["dataset_snapshot"],
                            "observed_at": row["observed_at"],
                            "product_type": row["product_type"],
                            "semantic_identity": semantic_identity,
                            "real_rdb": True,
                            "ranking_applied": compiled.ranking_applied,
                        },
                    )
                )
        return records


def _numeric_value(value: Any, operator: FilterOperator) -> Any:
    try:
        if operator is FilterOperator.IN:
            if not isinstance(value, (list, tuple)):
                return value
            return [float(item) for item in value]
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RDBQueryCompilationError("numeric filter value is invalid") from exc

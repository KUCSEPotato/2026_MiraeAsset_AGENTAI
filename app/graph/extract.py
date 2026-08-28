from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine, func, select

from app.data.schema import canonical_products, fund_classes, funds
from app.domain.models import CanonicalConcept
from app.graph.identity import (
    canonical_concept_id,
    explicit_source_id,
    graph_edge_id,
    source_scoped_name_id,
)
from app.graph.models import GraphBuildData, GraphBuildStats, GraphEdge, GraphNode


_PRODUCT_LABELS = {
    CanonicalConcept.FINANCIAL_PRODUCT_ETF.value: "ETF",
    CanonicalConcept.FINANCIAL_PRODUCT_ETN.value: "ETN",
    CanonicalConcept.FINANCIAL_PRODUCT_BOND.value: "Bond",
}


class CanonicalGraphExtractor:
    """Build graph-shaped records from the canonical relational snapshot."""

    def __init__(
        self,
        engine: Engine,
        *,
        snapshot: str,
        version: str = "legacy",
    ) -> None:
        if version not in {"legacy", "v7"}:
            raise ValueError("graph extractor version must be 'legacy' or 'v7'")
        self._engine = engine
        self._snapshot = snapshot
        self._version = version
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._stats = GraphBuildStats()

    def extract(self) -> GraphBuildData:
        with self._engine.connect() as connection:
            self._stats.source_rows = {
                str(source): int(count)
                for source, count in connection.execute(
                    select(
                        canonical_products.c.source_dataset,
                        func.count(),
                    )
                    .where(
                        canonical_products.c.dataset_snapshot == self._snapshot
                    )
                    .group_by(canonical_products.c.source_dataset)
                )
            }
            products = connection.execute(
                select(canonical_products)
                .where(canonical_products.c.dataset_snapshot == self._snapshot)
                .order_by(canonical_products.c.canonical_product_id)
            ).mappings()
            for row in products:
                self._extract_product(row)

            fund_rows = connection.execute(
                select(
                    fund_classes.c.fund_id,
                    fund_classes.c.canonical_product_id,
                    fund_classes.c.class_code,
                    funds.c.fund_name,
                    funds.c.source_fund_id,
                    canonical_products.c.issuer,
                    canonical_products.c.base_index,
                    canonical_products.c.source_record_key,
                )
                .join(
                    funds,
                    (funds.c.fund_id == fund_classes.c.fund_id)
                    & (
                        funds.c.dataset_snapshot
                        == fund_classes.c.dataset_snapshot
                    ),
                )
                .join(
                    canonical_products,
                    (
                        canonical_products.c.canonical_product_id
                        == fund_classes.c.canonical_product_id
                    )
                    & (
                        canonical_products.c.dataset_snapshot
                        == fund_classes.c.dataset_snapshot
                    ),
                )
                .where(fund_classes.c.dataset_snapshot == self._snapshot)
                .order_by(fund_classes.c.canonical_product_id)
            ).mappings()
            for row in fund_rows:
                self._extract_fund_class(row)

        self._finalize_stats()
        edges = tuple(
            GraphEdge(
                edge_id=graph_edge_id(subject, edge_type, object_id, self._snapshot),
                subject_id=subject,
                edge_type=edge_type,
                object_id=object_id,
                properties={
                    **data["properties"],
                    "source_record_keys": sorted(data["source_record_keys"]),
                    "source_fields": sorted(data["source_fields"]),
                },
            )
            for (subject, edge_type, object_id), data in sorted(self._edges.items())
        )
        return GraphBuildData(
            nodes=tuple(self._nodes[key] for key in sorted(self._nodes)),
            edges=edges,
            stats=self._stats,
        )

    def _extract_product(self, row: Mapping[str, Any]) -> None:
        product_type = str(row["product_type"])
        label = _PRODUCT_LABELS.get(product_type)
        if label is None:
            return  # public-fund classes are processed with their family rows
        product_id = str(row["canonical_product_id"])
        self._add_node(
            product_id,
            label,
            ("M10Entity", "FinancialProduct", label),
            {
                "display_name": row["product_name"],
                "product_type": product_type,
                "source_dataset": row["source_dataset"],
                "source_record_key": row["source_record_key"],
            },
        )
        source_dataset = str(row["source_dataset"])
        source_key = str(row["source_record_key"])

        if self._version == "v7":
            if label == "ETF":
                self._name_relation(
                    product_id,
                    "MANAGED_BY",
                    "AssetManagementCompany",
                    row["asset_manager"],
                    source_dataset,
                    source_key,
                    "canonical_products.asset_manager",
                )
                self._name_relation(
                    product_id,
                    "TRACKS_INDEX",
                    "Index",
                    row["base_index"],
                    source_dataset,
                    source_key,
                    "canonical_products.base_index",
                )
            return

        if label in {"ETF", "ETN"}:
            self._name_relation(
                product_id,
                "MANAGED_BY",
                "AssetManager",
                row["asset_manager"],
                source_dataset,
                source_key,
                "canonical_products.asset_manager",
            )
            self._name_relation(
                product_id,
                "TRACKS",
                "Index",
                row["base_index"],
                source_dataset,
                source_key,
                "canonical_products.base_index",
            )
            self._concept_relation(
                product_id,
                "INVESTS_IN_REGION",
                "Region",
                row["region"],
                source_dataset,
                source_key,
                "canonical_products.region",
            )
            self._concept_relation(
                product_id,
                "HAS_ASSET_TYPE",
                "AssetType",
                row["asset_type"],
                source_dataset,
                source_key,
                "canonical_products.asset_type",
            )
            self._concept_relation(
                product_id,
                "HAS_RISK_GRADE",
                "RiskGrade",
                row["risk_grade"],
                source_dataset,
                source_key,
                "canonical_products.risk_grade",
            )
            return

        self._name_relation(
            product_id,
            "ISSUED_BY",
            "Issuer",
            row["issuer"],
            source_dataset,
            source_key,
            "canonical_products.issuer",
        )
        risk = _clean(row["risk_grade"])
        if risk == "0":
            self._skip("HAS_RISK_GRADE", sentinel=True)
        else:
            self._concept_relation(
                product_id,
                "HAS_RISK_GRADE",
                "RiskGrade",
                risk,
                source_dataset,
                source_key,
                "canonical_products.risk_grade",
            )
        currency = _clean(row["currency"])
        if currency is not None and (
            currency == "000" or len(currency) != 3 or not currency.isalpha()
        ):
            self._skip("DENOMINATED_IN", sentinel=True)
        else:
            self._concept_relation(
                product_id,
                "DENOMINATED_IN",
                "Currency",
                currency,
                source_dataset,
                source_key,
                "canonical_products.currency",
            )

    def _extract_fund_class(self, row: Mapping[str, Any]) -> None:
        fund_id = str(row["fund_id"])
        class_id = str(row["canonical_product_id"])
        source_key = str(row["source_record_key"])
        if self._version == "v7":
            self._add_node(
                fund_id,
                "Fund",
                ("M10Entity", "FinancialProduct", "Fund"),
                {
                    "display_name": row["fund_name"],
                    "source_dataset": "public_fund",
                    "source_record_key": row["source_fund_id"],
                },
            )
            self._add_node(
                class_id,
                "FundShareClass",
                ("M10Entity", "FundShareClass"),
                {
                    "display_name": (
                        f"{row['fund_name']} ({row['class_code']})"
                    ),
                    "class_code": row["class_code"],
                    "source_dataset": "public_fund",
                    "source_record_key": source_key,
                },
            )
            self._add_edge(
                fund_id,
                "HAS_SHARE_CLASS",
                class_id,
                "public_fund",
                source_key,
                "fund_classes.fund_id",
            )
            return

        self._add_node(
            fund_id,
            "Fund",
            ("M10Entity", "FinancialProduct", "Fund"),
            {
                "display_name": row["fund_name"],
                "source_dataset": "public_fund",
                "source_record_key": row["source_fund_id"],
            },
        )
        self._add_node(
            class_id,
            "FundClass",
            ("M10Entity", "FinancialProduct", "FundClass"),
            {
                "display_name": (
                    f"{row['fund_name']} ({row['class_code']})"
                ),
                "class_code": row["class_code"],
                "source_dataset": "public_fund",
                "source_record_key": source_key,
            },
        )
        self._add_edge(
            fund_id,
            "HAS_CLASS",
            class_id,
            "public_fund",
            source_key,
            "fund_classes.fund_id",
        )

        manager_code = _clean(row["issuer"])
        if manager_code is None:
            self._skip("MANAGED_BY")
        else:
            manager_id = explicit_source_id(
                "asset_manager", "public_fund", manager_code
            )
            self._add_node(
                manager_id,
                "AssetManager",
                ("M10Entity", "AssetManager"),
                {
                    "display_name": manager_code,
                    "source_dataset": "public_fund",
                    "identifier_type": "external_institution_code",
                    "identifier_value": manager_code,
                },
            )
            self._add_edge(
                fund_id,
                "MANAGED_BY",
                manager_id,
                "public_fund",
                source_key,
                "canonical_products.issuer",
            )

        benchmark = _clean(row["base_index"])
        if benchmark is None:
            self._skip("REFERENCES_BENCHMARK")
        else:
            benchmark_id = source_scoped_name_id(
                "benchmark", "public_fund", benchmark
            )
            self._add_node(
                benchmark_id,
                "Benchmark",
                ("M10Entity", "Benchmark"),
                {
                    "display_name": benchmark,
                    "source_dataset": "public_fund",
                    "identity_basis": "source_scoped_exact_normalized_label",
                },
            )
            self._add_edge(
                fund_id,
                "REFERENCES_BENCHMARK",
                benchmark_id,
                "public_fund",
                source_key,
                "canonical_products.base_index",
            )

    def _name_relation(
        self,
        subject_id: str,
        edge_type: str,
        node_type: str,
        value: Any,
        source_dataset: str,
        source_key: str,
        source_field: str,
    ) -> None:
        label = _clean(value)
        if label is None:
            self._skip(edge_type)
            return
        object_id = source_scoped_name_id(node_type, source_dataset, label)
        self._add_node(
            object_id,
            node_type,
            ("M10Entity", node_type),
            {
                "display_name": label,
                "source_dataset": source_dataset,
                "identity_basis": "source_scoped_exact_normalized_label",
            },
        )
        self._add_edge(
            subject_id,
            edge_type,
            object_id,
            source_dataset,
            source_key,
            source_field,
        )

    def _concept_relation(
        self,
        subject_id: str,
        edge_type: str,
        node_type: str,
        value: Any,
        source_dataset: str,
        source_key: str,
        source_field: str,
    ) -> None:
        canonical = _clean(value)
        if canonical is None:
            self._skip(edge_type)
            return
        object_id = canonical_concept_id(node_type, canonical)
        self._add_node(
            object_id,
            node_type,
            ("M10Entity", node_type),
            {
                "display_name": canonical,
                "canonical_value": canonical,
                "identity_basis": "canonical_concept",
            },
        )
        self._add_edge(
            subject_id,
            edge_type,
            object_id,
            source_dataset,
            source_key,
            source_field,
        )

    def _add_node(
        self,
        entity_id: str,
        node_type: str,
        labels: tuple[str, ...],
        properties: dict[str, Any],
    ) -> None:
        if entity_id in self._nodes:
            return
        self._nodes[entity_id] = GraphNode(
            entity_id=entity_id,
            node_type=node_type,
            labels=labels,
            properties={
                **properties,
                "dataset_snapshot": self._snapshot,
                "node_type": node_type,
            },
        )

    def _add_edge(
        self,
        subject_id: str,
        edge_type: str,
        object_id: str,
        source_dataset: str,
        source_key: str,
        source_field: str,
    ) -> None:
        key = (subject_id, edge_type, object_id)
        existing = self._edges.setdefault(
            key,
            {
                "properties": {
                    "dataset_snapshot": self._snapshot,
                    "edge_type": edge_type,
                    "source_dataset": source_dataset,
                },
                "source_record_keys": set(),
                "source_fields": set(),
            },
        )
        existing["source_record_keys"].add(source_key)
        existing["source_fields"].add(source_field)

    def _skip(self, edge_type: str, *, sentinel: bool = False) -> None:
        counts = (
            self._stats.skipped_sentinel_relations
            if sentinel
            else self._stats.skipped_null_relations
        )
        counts[edge_type] = counts.get(edge_type, 0) + 1

    def _finalize_stats(self) -> None:
        self._stats.nodes_by_type = dict(
            Counter(node.node_type for node in self._nodes.values())
        )
        self._stats.edges_by_relation = dict(
            Counter(edge_type for _, edge_type, _ in self._edges)
        )


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None

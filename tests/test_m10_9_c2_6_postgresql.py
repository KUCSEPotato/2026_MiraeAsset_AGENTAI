from __future__ import annotations

import asyncio
import os
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
import httpx
import pytest
from sqlalchemy import create_engine, func, insert, inspect, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url

from app.data.security_issuers import (
    KRX_ISSUER_CANONICAL_SNAPSHOT_ID,
    KRX_ISSUER_TRANSFORMER_VERSION,
    TrustedSecurityIssuerIntegrator,
    load_trusted_issuer_snapshot,
)
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA,
    canonical_entities,
    canonical_facts,
    dataset_snapshots,
    entity_identifiers,
    entity_relations,
    external_security_issuer_records,
    fact_evidence_links,
    identifier_schemes,
    organizations,
    securities,
    source_field_assertions,
    source_records,
)
from app.external_data.issuers.krx_kind import build_krx_kind_issuer_snapshot
from app.external_data.manifest import SnapshotWorkspace
from app.graph.v2 import CanonicalV2GraphExtractor


pytestmark = pytest.mark.postgresql
ROOT = Path(__file__).resolve().parents[1]
VERSION_TABLE = "alembic_version_m10_9_c2_6"


def _url() -> str:
    value = os.getenv("M10_9_C2_DATABASE_URL") or os.getenv(
        "POSTGRES_TEST_DATABASE_URL"
    )
    if not value:
        pytest.skip("M10_9_C2_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("C2.6 integration requires PostgreSQL")
    database = (parsed.database or "").casefold()
    if "test" not in database and "c2" not in database:
        pytest.fail("C2.6 test refuses a non-disposable database")
    return value


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.set_main_option("version_table", VERSION_TABLE)
    return config


@pytest.fixture(scope="module")
def engine():
    url = _url()
    result = create_engine(url, future=True)
    with result.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {CANONICAL_V2_SCHEMA} CASCADE"))
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
    command.upgrade(_config(url), "head")
    yield result
    command.downgrade(_config(url), "base")
    with result.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
    result.dispose()


@pytest.fixture
def connection(engine):
    with engine.connect() as value:
        transaction = value.begin()
        yield value
        if transaction.is_active:
            transaction.rollback()


def _html(rows: list[tuple[str, str, str]]) -> bytes:
    body = "".join(
        "<tr>"
        f"<td><a title='{name}' onclick=\"companysummary_open('{issuer}')\">{name}</a></td>"
        "<td>주권</td>"
        f"<td>{ticker}</td><td>2000-01-01</td></tr>"
        for issuer, name, ticker in rows
    )
    return f"<html><table>{body}</table></html>".encode("euc-kr")


def _snapshot(tmp_path: Path):
    payloads = {
        "STK": _html([
            ("00593", "삼성전자", "005930"),
            ("00066", "SK하이닉스", "000660"),
        ]),
        "KSQ": _html([("03542", "NAVER", "035420")]),
        "KNX": _html([("95020", "테스트", "950200")]),
    }

    class Client:
        async def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            request = httpx.Request("POST", url)
            return httpx.Response(
                200, content=payloads[data["mktId"]], request=request
            )

    workspace = SnapshotWorkspace(
        tmp_path,
        snapshot_id="krx-issuer-postgresql",
        snapshot_date=date(2026, 8, 31),
        crawler_version="test",
        data_cutoff_date=date(2026, 8, 24),
    )
    asyncio.run(build_krx_kind_issuer_snapshot(
        Client(), workspace, scoped_tickers={"005930", "000660"}
    ))
    return load_trusted_issuer_snapshot(workspace.path)


def _seed(connection) -> None:
    connection.execute(
        pg_insert(identifier_schemes).values(
            scheme_code="TICKER",
            label="Exchange ticker",
            default_namespace="exchange",
            is_globally_unique=False,
        ).on_conflict_do_nothing()
    )
    for security_id, name, ticker in (
        ("security:005930", "삼성전자 보통주", "005930"),
        ("security:000660", "SK하이닉스 보통주", "000660"),
    ):
        connection.execute(insert(canonical_entities).values(
            entity_id=security_id,
            entity_kind="SECURITY",
            preferred_name=name,
            normalized_preferred_name=name,
            name_status="AUTHORITATIVE",
            identity_status="VALIDATED",
            query_eligible=True,
        ))
        connection.execute(insert(securities).values(
            security_id=security_id,
            security_type="EQUITY",
            ticker=ticker,
            exchange="KRX",
        ))
        connection.execute(insert(entity_identifiers).values(
            entity_id=security_id,
            scheme_code="TICKER",
            namespace="KRX",
            raw_value=ticker,
            normalized_value=ticker,
            validation_status="VALIDATED",
            resolution_status="RESOLVED",
            conflict_status="NONE",
            is_primary=True,
        ))
    connection.execute(insert(canonical_entities).values(
        entity_id="organization:existing:samsung",
        entity_kind="ORGANIZATION",
        preferred_name="삼성전자",
        normalized_preferred_name="삼성전자",
        name_status="AUTHORITATIVE",
        identity_status="VALIDATED",
        query_eligible=False,
    ))
    connection.execute(insert(organizations).values(
        organization_id="organization:existing:samsung",
        organization_type="ISSUER",
    ))


def test_migration_contains_source_issuer_contract(engine) -> None:
    assert "external_security_issuer_records" in set(
        inspect(engine).get_table_names(schema=CANONICAL_V2_SCHEMA)
    )


def test_authoritative_issuer_integration_identity_provenance_and_idempotency(
    connection, tmp_path: Path,
) -> None:
    _seed(connection)
    snapshot = _snapshot(tmp_path)
    integrator = TrustedSecurityIssuerIntegrator(connection)
    first = integrator.integrate(snapshot)
    second = integrator.integrate(snapshot)

    assert first.eligible_records == 2
    assert first.security_resolved == 2
    assert first.organization_existing == 1
    assert first.organization_created == 1
    assert first.canonical_facts == 2
    assert second.canonical_facts == 0
    assert second.deduplicated == 2
    assert connection.scalar(
        select(dataset_snapshots.c.transformer_version).where(
            dataset_snapshots.c.snapshot_id
            == KRX_ISSUER_CANONICAL_SNAPSHOT_ID
        )
    ) == KRX_ISSUER_TRANSFORMER_VERSION
    assert connection.scalar(
        select(func.count()).select_from(entity_relations).where(
            entity_relations.c.relation_type == "SECURITY_ISSUED_BY"
        )
    ) == 2
    assert connection.scalar(
        select(func.count()).select_from(fact_evidence_links)
    ) == 2
    statuses = set(connection.execute(select(
        external_security_issuer_records.c.security_identity_status,
        external_security_issuer_records.c.issuer_identity_status,
        external_security_issuer_records.c.relation_validation_status,
    )).all())
    assert statuses == {("RESOLVED", "RESOLVED", "RESOLVED")}

    chain = connection.execute(
        select(
            canonical_facts.c.fact_id,
            source_field_assertions.c.assertion_id,
            source_records.c.source_record_id,
        )
        .join(
            fact_evidence_links,
            fact_evidence_links.c.fact_id == canonical_facts.c.fact_id,
        )
        .join(
            source_field_assertions,
            source_field_assertions.c.assertion_id
            == fact_evidence_links.c.assertion_id,
        )
        .join(
            source_records,
            source_records.c.source_record_id
            == source_field_assertions.c.source_record_id,
        )
        .where(canonical_facts.c.semantic_key.like("securityIssuedBy:%"))
    ).all()
    assert len(chain) == 2
    assert all(row.source_record_id.startswith("normalized:issuerrec_") for row in chain)

    connection.commit()
    data = CanonicalV2GraphExtractor(
        connection.engine,
        snapshot_ids=(snapshot.canonical_snapshot_id,),
        snapshot="2026-08-24",
    ).extract()
    assert data.stats.edges_by_relation == {"SECURITY_ISSUED_BY": 2}
    assert all(
        edge.properties["evidence_assertion_ids"]
        and edge.properties["source_fields"] == ["isurCd+representative_ticker"]
        and edge.properties["source_record_keys"]
        for edge in data.edges
    )
    security_nodes = [node for node in data.nodes if node.node_type == "EquitySecurity"]
    assert {node.properties["identifier_value"] for node in security_nodes} == {
        "005930", "000660",
    }


def test_source_conflicts_are_never_first_row_resolved(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    first = snapshot.issuer_records[0]
    conflict = first.model_copy(update={
        "issuer_record_id": "issuerrec_conflict",
        "issuer_source_id": "99999",
        "issuer_name_raw": "다른회사",
    })
    ticker_conflicts, _ = TrustedSecurityIssuerIntegrator._source_conflicts(
        (first, conflict)
    )
    assert ticker_conflicts == {first.security_ticker}

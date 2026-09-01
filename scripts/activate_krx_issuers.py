"""Integrate one reviewed KRX KIND issuer snapshot into canonical_v2."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, distinct, func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.security_issuers import (  # noqa: E402
    KRX_ISSUER_CANONICAL_SNAPSHOT_ID,
    MULTI_PROVIDER_ISSUER_CANONICAL_SNAPSHOT_ID,
    MULTI_PROVIDER_ISSUER_SCOPE,
    TrustedSecurityIssuerIntegrator,
    load_trusted_issuer_snapshot,
)
from app.data.v2_schema import (  # noqa: E402
    canonical_facts,
    entity_relations,
    external_security_issuer_records,
    fact_evidence_links,
    holding_fact_details,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Activate authoritative KRX Security -> Organization evidence"
    )
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--report-file", type=Path)
    parser.add_argument(
        "--multi-provider", action="store_true",
        help="activate the reviewed KODEX+TIGER issuer scope as immutable v2",
    )
    args = parser.parse_args()

    snapshot = load_trusted_issuer_snapshot(
        args.snapshot_root,
        canonical_snapshot_id=(
            MULTI_PROVIDER_ISSUER_CANONICAL_SNAPSHOT_ID
            if args.multi_provider else KRX_ISSUER_CANONICAL_SNAPSHOT_ID
        ),
        coverage_scope=(
            MULTI_PROVIDER_ISSUER_SCOPE
            if args.multi_provider else "KODEX_LONG_ONLY_COMPATIBLE"
        ),
    )
    engine = create_engine(args.database_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            first = TrustedSecurityIssuerIntegrator(connection).integrate(snapshot)
            second = TrustedSecurityIssuerIntegrator(connection).integrate(snapshot)
        with engine.connect() as connection:
            issuer_relation = entity_relations.alias("issuer_relation")
            statuses = {
                str(status): int(count)
                for status, count in connection.execute(
                    select(
                        external_security_issuer_records.c.relation_validation_status,
                        func.count(),
                    ).group_by(
                        external_security_issuer_records.c.relation_validation_status
                    )
                )
            }
            issuer_facts = int(connection.scalar(
                select(func.count())
                .select_from(entity_relations.join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_relations.c.fact_id,
                ))
                .where(
                    entity_relations.c.relation_type == "SECURITY_ISSUED_BY",
                    canonical_facts.c.snapshot_id == snapshot.canonical_snapshot_id,
                )
            ) or 0)
            issuer_evidence = int(connection.scalar(
                select(func.count(distinct(fact_evidence_links.c.fact_id)))
                .select_from(
                    fact_evidence_links.join(
                        canonical_facts,
                        canonical_facts.c.fact_id == fact_evidence_links.c.fact_id,
                    ).join(
                        entity_relations,
                        entity_relations.c.fact_id == canonical_facts.c.fact_id,
                    )
                )
                .where(entity_relations.c.relation_type == "SECURITY_ISSUED_BY")
            ) or 0)
            weighted_total = int(connection.scalar(
                select(func.count()).select_from(holding_fact_details)
            ) or 0)
            weighted_resolved = int(connection.scalar(
                select(func.count())
                .select_from(
                    holding_fact_details
                    .join(
                        entity_relations,
                        entity_relations.c.fact_id == holding_fact_details.c.fact_id,
                    )
                    .join(
                        issuer_relation,
                        issuer_relation.c.subject_entity_id
                        == entity_relations.c.object_entity_id,
                    )
                )
                .where(
                    entity_relations.c.relation_type == "HOLDS",
                    issuer_relation.c.relation_type == "SECURITY_ISSUED_BY",
                )
            ) or 0)
        result = {
            "external_snapshot_id": snapshot.external_snapshot_id,
            "canonical_snapshot_id": snapshot.canonical_snapshot_id,
            "eligible_records": first.eligible_records,
            "security_resolved": first.security_resolved,
            "security_ambiguous": first.security_ambiguous,
            "security_conflict": first.security_conflict,
            "security_unresolved": first.security_unresolved,
            "organization_existing": first.organization_existing,
            "organization_created": first.organization_created,
            "organization_ambiguous": first.organization_ambiguous,
            "organization_conflict": first.organization_conflict,
            "organization_unresolved": first.organization_unresolved,
            "canonical_facts_inserted_first": first.canonical_facts,
            "canonical_facts_inserted_second": second.canonical_facts,
            "deduplicated_second": second.deduplicated,
            "stored_relation_statuses": statuses,
            "stored_issuer_facts": issuer_facts,
            "evidenced_issuer_facts": issuer_evidence,
            "holds_facts_total": weighted_total,
            "holds_facts_with_issuer": weighted_resolved,
        }
    finally:
        engine.dispose()

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

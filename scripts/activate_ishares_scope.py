"""Build and activate the reviewed iShares foreign-ETF READY scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.holdings import (  # noqa: E402
    TrustedHoldingsCanonicalIntegrator,
    ensure_holdings_canonical_snapshot,
)
from app.data.v2_schema import (  # noqa: E402
    canonical_facts,
    entity_relations,
    fact_evidence_links,
    securities,
)
from app.external_data.holdings.ishares_scope import (  # noqa: E402
    build_ishares_ready_scope,
    load_trusted_ishares_scope,
)
from app.external_data.holdings.provider_contracts import ISHARES_CONTRACT  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Activate the reviewed iShares READY scope")
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()
    manifest_path, manifest = build_ishares_ready_scope(args.snapshot_root)
    result: dict[str, object] = {
        "scope_manifest": str(manifest_path),
        "scope": manifest.scope,
        "status": manifest.status,
        "portfolio_effective_date": manifest.portfolio_effective_date.isoformat(),
        "ready_product_count": manifest.ready_product_count,
        "blocked_product_count": manifest.blocked_product_count,
        "portfolio_row_count": manifest.portfolio_row_count,
        "eligible_security_row_count": manifest.eligible_security_row_count,
        "non_security_row_count": manifest.non_security_row_count,
        "unique_security_identities": manifest.unique_security_identities,
        "classification_counts": manifest.classification_counts,
    }
    if args.database_url:
        engine = create_engine(args.database_url, future=True, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                snapshot_id = ensure_holdings_canonical_snapshot(
                    connection,
                    contract=ISHARES_CONTRACT,
                    scope_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    portfolio_row_count=manifest.portfolio_row_count,
                )
                snapshot = load_trusted_ishares_scope(
                    args.snapshot_root, canonical_snapshot_id=snapshot_id,
                )
                first = TrustedHoldingsCanonicalIntegrator(connection).integrate(snapshot)
                second = TrustedHoldingsCanonicalIntegrator(connection).integrate(snapshot)
            with engine.connect() as connection:
                holds = int(connection.scalar(
                    select(func.count()).select_from(
                        entity_relations.join(
                            canonical_facts,
                            canonical_facts.c.fact_id == entity_relations.c.fact_id,
                        )
                    ).where(
                        canonical_facts.c.snapshot_id == snapshot_id,
                        entity_relations.c.relation_type == "HOLDS",
                    )
                ) or 0)
                evidence = int(connection.scalar(
                    select(func.count()).select_from(
                        fact_evidence_links.join(
                            canonical_facts,
                            canonical_facts.c.fact_id == fact_evidence_links.c.fact_id,
                        )
                    ).where(canonical_facts.c.snapshot_id == snapshot_id)
                ) or 0)
                exchange_counts = {
                    str(exchange): int(count)
                    for exchange, count in connection.execute(
                        select(securities.c.exchange, func.count())
                        .where(securities.c.security_id.in_(
                            select(entity_relations.c.object_entity_id)
                            .join(
                                canonical_facts,
                                canonical_facts.c.fact_id == entity_relations.c.fact_id,
                            )
                            .where(
                                canonical_facts.c.snapshot_id == snapshot_id,
                                entity_relations.c.relation_type == "HOLDS",
                            )
                        ))
                        .group_by(securities.c.exchange)
                        .order_by(securities.c.exchange)
                    )
                }
            result["canonical"] = {
                "snapshot_id": snapshot_id,
                "product_resolved": first.product_resolved,
                "product_ambiguous": first.product_ambiguous,
                "product_unresolved": first.product_unresolved,
                "security_reused": first.security_resolved - first.security_created,
                "security_created": first.security_created,
                "security_ambiguous": first.security_ambiguous,
                "security_unresolved": first.security_unresolved,
                "non_security": first.non_security,
                "holds_first": first.canonical_holds_facts,
                "holds_second": second.canonical_holds_facts,
                "deduplicated_second": second.deduplicated,
                "stored_holds": holds,
                "evidence_links": evidence,
                "securities_by_exchange": exchange_counts,
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

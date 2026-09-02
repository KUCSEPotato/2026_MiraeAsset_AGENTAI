"""Activate the reviewed iShares one-year NAV total-return scope."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.external_metrics import (  # noqa: E402
    TrustedExternalMetricIntegrator,
    ensure_ishares_return_canonical_snapshot,
    load_trusted_ishares_return_snapshot,
)
from app.data.v2_schema import (  # noqa: E402
    canonical_facts,
    fact_evidence_links,
    metric_observations,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Activate the reviewed iShares one-year return scope"
    )
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()
    snapshot = load_trusted_ishares_return_snapshot(args.snapshot_root)
    engine = create_engine(args.database_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            snapshot_id = ensure_ishares_return_canonical_snapshot(
                connection,
                manifest_sha256=snapshot.manifest_sha256,
                observation_count=len(snapshot.observations),
            )
            first = TrustedExternalMetricIntegrator(connection).integrate(snapshot)
            second = TrustedExternalMetricIntegrator(connection).integrate(snapshot)
        with engine.connect() as connection:
            stored = int(connection.scalar(
                select(func.count()).select_from(metric_observations).join(
                    canonical_facts,
                    canonical_facts.c.fact_id == metric_observations.c.fact_id,
                ).where(
                    canonical_facts.c.snapshot_id == snapshot_id,
                    metric_observations.c.metric_code == "ONE_YEAR_RETURN",
                )
            ) or 0)
            evidence = int(connection.scalar(
                select(func.count()).select_from(fact_evidence_links).join(
                    canonical_facts,
                    canonical_facts.c.fact_id == fact_evidence_links.c.fact_id,
                ).where(canonical_facts.c.snapshot_id == snapshot_id)
            ) or 0)
        result = {
            "scope": "ISHARES_FOREIGN_ETF_ONE_YEAR_RETURN",
            "status": "READY",
            "snapshot_id": snapshot_id,
            "observations": first.observations,
            "product_resolved": first.product_resolved,
            "product_ambiguous": first.product_ambiguous,
            "product_unresolved": first.product_unresolved,
            "facts_first": first.canonical_metric_facts,
            "facts_second": second.canonical_metric_facts,
            "deduplicated_second": second.deduplicated,
            "stored_metric_facts": stored,
            "evidence_links": evidence,
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

# M10.9-C3.2 release baseline

## Current release status

`NOT_READY`. The repository intentionally does not contain the production
external-data snapshots or generated semantic index. The checked-in
`deployment/production-artifacts.json` records that unprovisioned state; it is
not accepted by the production startup validator.

No C3.2 change expands semantic coverage. In particular, domestic/iShares
cross-source one-year return ordering and Public Fund holdings remain
unsupported.

## Artifact provisioning contract

Production data is transferred as one access-controlled, immutable directory
bundle rather than committed to Git. The release operator places these paths
under one bundle root:

```text
external/kodex/
external/tiger/
external/krx-issuers/
external/ishares-holdings/
external/ishares-returns/
data/semantic_search.json
```

Each external directory contains the complete accepted snapshot, including
its raw artifacts, normalized outputs, SourceRecords, evidence links, and
provider manifest. Existing activation commands validate those internal
manifests and raw checksums before PostgreSQL integration.

After controlled upload or server-side acquisition, generate the tracked
artifact manifest with `scripts/build_production_artifact_manifest.py`. Supply one
`--artifact ROLE=VERSION=KIND=RELATIVE_PATH` option for each required role.
The generator computes deterministic file/tree SHA-256 values and refuses
missing roles. Once the source commit is final, package it with
`scripts/package_production_bundle.py`; that step creates untracked
`release.json` with the exact Git/image SHA.

In production, both `PRODUCTION_ARTIFACT_ROOT` and
`PRODUCTION_ARTIFACT_MANIFEST` are mandatory. Startup verifies the cutoff,
canonical/ontology/graph/semantic versions, required roles, safe relative
paths, and every checksum before `/health` can return READY.

## Cold-start sequence

1. Clone the exact release commit and copy `.env.example` to a server-local
   `.env`; replace placeholders without committing secrets.
2. Provision the immutable artifact bundle through controlled upload or run
   the existing cutoff-pinned crawlers on the server.
3. Generate and verify the artifact manifest, then package bundle-only
   `release.json` after the source commit is final.
4. Start PostgreSQL and Neo4j with `docker compose -f
   docker-compose.prod.yml up -d postgres neo4j`.
5. Run Alembic and the accepted `app.data.v2_rebuild` against the clean
   PostgreSQL database.
6. Activate KODEX, TIGER, iShares Holdings, KRX issuer, and iShares return
   snapshots with their existing activation scripts. No fixture fallback is
   allowed.
7. Build the canonical-v2 Neo4j projection and semantic index from that same
   selected snapshot. Verify exact `HOLDS` and `SECURITY_ISSUED_BY` PG/Neo4j
   counts.
8. Start `agent-api`. `/live` proves process liveness; `/health` must remain
   503 until PostgreSQL, graph, semantic index, scopes, issuer facts, metric
   facts, and the production artifact bundle all validate.
9. Run `GET /answer` smoke queries and the evaluation matrix below.

Only the API publishes a host port. PostgreSQL and Neo4j stay on the private
Compose backend network.

## Evaluation matrix

Supported probes:

- purchasable KRW Bond with explicit credit-rating constraint
- domestic ETF AUM Top-N within its source contract
- domestic ETF one-year return ranking
- KODEX and TIGER READY-scope Holdings
- company to Security to ETF traversal
- exchange-qualified/global Security identifiers
- selected iShares Holdings and scoped one-year return ranking
- bounded KODEX/TIGER/iShares Holdings union without cross-source return sort

Expected fail-closed probes:

- unknown or ambiguous entity
- generic DomesticETF or ForeignETF incomplete Holdings coverage
- PublicFund Holdings or fund-level one-year return
- domestic/iShares cross-source one-year return ordering
- invalid credit rating or missing issuer evidence

For every answerable structured probe, asserted answer facts must be a subset
of validated evidence serialized in `retrieved_context`. `think_trace` contains
only operational classification, resolution, store, structured-operation,
cardinality, and answerability summaries—not hidden reasoning.

## HyperCLOVA gates

The semantic parser uses deterministic rules first and HyperCLOVA only as its
configured fallback. Answer generation is a distinct client and is controlled
by `HYPERCLOVA_ANSWER_ENABLED`. Both use environment-only credentials; secrets
are redacted from operational logs. If answer generation is enabled without a
credential, startup fails. A live smoke remains mandatory on the deployment
host when credentials are available.

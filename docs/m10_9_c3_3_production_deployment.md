# M10.9-C3.3 production artifact and deployment runbook

## Recovery before generation

Recover the accepted immutable snapshots from the development machine that
produced the accepted C2/C3 results. Compare each recovered provider manifest,
raw artifact, SourceRecord, normalized output, and scope manifest with its
recorded internal checksum. Do not recrawl merely because the current checkout
lacks `external_data/`.

If recovery fails, stop the release. Regeneration is a separately reviewed
data-production action because current provider responses may differ from the
accepted cutoff artifacts.

## Bundle layout

```text
deployment_bundle/
  release.json
  manifests/production-artifacts.json
  material/ai-festival2026_금융상품Agent_DtataSet260824/
  ontology/
    common.ttl
    bond_kr.ttl
    etf_kr.ttl
    etf_gl.ttl
    fund_pub.ttl
  external/
    kodex/
    tiger/
    krx-issuers/
    ishares-holdings/
    ishares-returns/
  data/semantic_search.json
```

Generate the tracked READY artifact manifest only after every path is present. The manifest
builder requires artifact version, kind, path, effective date, compatibility
version, and the artifact's own source-manifest path. It records artifact and
source-manifest SHA-256 values. It deliberately does not record the release ID
or Git SHA, which would create a tracked-file self-reference loop.

After all source changes are committed and pushed, package the verified
directory with `scripts/package_production_bundle.py --release-id ...
--git-commit "$FINAL_CODE_SHA"`. It generates bundle-only `release.json`, which
wraps the artifact contract and binds it to the immutable source/image SHA. The
script writes a deterministic uncompressed tar and adjacent top-level SHA-256
file. Transfer those two files over SSH:

```bash
rsync -av --chmod=F600 release.tar release.tar.sha256 \
  deploy@SERVER:/opt/mirae-agent/incoming/
```

The bundle is never placed in Git or public object storage.

## Stable Naver Cloud layout

```text
/opt/mirae-agent/
  releases/<release-id>/app/
  releases/<release-id>/artifacts/
  incoming/<release-id>.tar
  incoming/<release-id>.tar.sha256
  env/production.env
  data/postgres/
  data/neo4j/
  current -> releases/<release-id>
```

Use a non-root deployment account with narrowly scoped Docker and directory
permissions. Credentials remain only in `env/production.env` or the platform's
secret store.

## Bootstrap gates

Before enabling GitHub deployment, operators must use the recovered bundle to:

1. migrate and rebuild a clean PostgreSQL database;
2. activate KODEX, TIGER, iShares Holdings, KRX issuer, and iShares return
   snapshots twice and record stable counts;
3. build Neo4j and prove exact HOLDS and SECURITY_ISSUED_BY reconciliation;
4. load and validate the pinned semantic index;
5. configure both HyperCLOVA paths and run a redaction-safe live smoke;
6. execute the complete production evaluation/evidence matrix and latency run.

No fixture is permitted in these gates.

## CI/CD and promotion

The workflow `.github/workflows/deploy-production.yml` runs tests, publishes an
image tagged with the exact Git SHA, and deploys only when the protected
repository/environment variable `DEPLOY_ENABLED` equals `true`. Deployment
uses `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, and a pinned
`DEPLOY_HOST_KEY`; application credentials stay on the server.

The server script verifies the transferred bundle checksum, uses the immutable
image tag, requires `/live`, `/health`, and `/answer`, and only then changes the
`current` symlink. Its error trap restores the prior release. Keep the previous
image and release directory until rollback rehearsal succeeds.

At freeze, tag the accepted SHA (recommended `submission-2026-09-06`), deploy
that exact tag, mirror the same source tree to the organizer repository, compare
tree hashes, and set `DEPLOY_ENABLED=false`.

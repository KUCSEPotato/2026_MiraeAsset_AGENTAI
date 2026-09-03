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

After all data-production changes are committed, package the verified directory
with `scripts/package_production_bundle.py --release-id ... --git-commit ...`.
It generates bundle-only `release.json`; `git_commit` records the code that
packaged that artifact release, but it is not the identity of every later
application image that consumes compatible artifacts. The script writes a
deterministic uncompressed tar and adjacent top-level SHA-256 file. Transfer
those two files over SSH once per artifact release:

```bash
rsync -av --chmod=F600 release.tar release.tar.sha256 \
  deploy@SERVER:/opt/mirae-agent/incoming/
```

The bundle is never placed in Git or public object storage.

## Stable Naver Cloud layout

```text
/opt/mirae-agent/
  .env
  releases/code-<git-sha>/app/
  releases/<artifact-release-id>/
  incoming/<artifact-release-id>.tar
  incoming/<artifact-release-id>.tar.sha256
  data/postgres/
  data/neo4j/
  current -> releases/code-<git-sha>
```

Use a non-root deployment account with narrowly scoped Docker and directory
permissions. Credentials remain only in `/opt/mirae-agent/.env` or the
platform's secret store. GitHub Actions never writes that file. If an existing
installation still references `env/production.env`, replace it once with a
symlink to `/opt/mirae-agent/.env`.

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
uses `NAVER_DEPLOY_HOST`, `NAVER_DEPLOY_USER`, `NAVER_DEPLOY_SSH_KEY`, and a
pinned `NAVER_DEPLOY_HOST_KEY`; application credentials stay on the server.
`ARTIFACT_RELEASE_ID` selects the already verified server-side data release.

The server script accepts `CODE_SHA IMAGE_REF ARTIFACT_RELEASE_ID`. It verifies
the retained artifact tar checksum without extracting or retransferring it,
checks that the image tag equals `CODE_SHA`, preserves the database/graph
volumes, requires `/live`, `/health`, and the exact five-field `/answer`
contract, and only then changes `current`. Its error trap reads the previous
code release's deployment state and restores that known-good application image.
Keep the previous image and code release directory until rollback rehearsal
succeeds.

## First continuous-deployment activation

1. Keep `DEPLOY_ENABLED` unset or unequal to `true` while merging these changes.
2. Install Docker and the Compose plugin and create `/opt/mirae-agent/{incoming,releases}`.
3. Create `/opt/mirae-agent/.env` manually with production database, Neo4j, and
   HyperCLOVA credentials. Never copy it into a code release.
4. Verify `submission-candidate-20260902-v4.tar` and its adjacent checksum in
   `incoming/`, and retain its extracted tree at
   `releases/submission-candidate-20260902-v4/`.
5. Complete the one-time PostgreSQL and Neo4j bootstrap against that artifact
   release; the named Compose volumes then survive code releases.
6. Configure the production environment secrets `NAVER_DEPLOY_HOST`,
   `NAVER_DEPLOY_USER`, `NAVER_DEPLOY_SSH_KEY`, and
   `NAVER_DEPLOY_HOST_KEY`. Configure variables `ARTIFACT_RELEASE_ID` and
   `DEPLOY_ENABLED`.
7. Set `ARTIFACT_RELEASE_ID=submission-candidate-20260902-v4`, enable deployment,
   and use `workflow_dispatch` for the first deployment.
8. Confirm health, smoke, promotion, and rollback metadata before relying on
   automatic deployments from later pushes to `main`.

At freeze, tag the accepted SHA (recommended `submission-2026-09-06`), deploy
that exact tag, mirror the same source tree to the organizer repository, compare
tree hashes, and set `DEPLOY_ENABLED=false`.

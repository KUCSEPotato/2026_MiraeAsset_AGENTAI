# Naver Cloud Docker Compose Deployment Runbook

Docker Compose is the authoritative production deployment mechanism. The
stack contains independent `frontend` (Nginx), `agent-api` (FastAPI),
PostgreSQL and Neo4j services. Only `frontend` publishes the existing public
port (`API_PORT`, default 8000); the API and databases are on the internal
Compose network. Application credentials are loaded only by `agent-api`.

Open `http://SERVER:8000/chat`. Nginx serves `/`, `/chat`, `/chat/` and
`/assets/` from its own image and proxies `/answer`, `/live`, `/health`,
`/docs` and `/openapi.json` to `agent-api:8000`. UI routes remain available
while the API restarts; Docker DNS is re-resolved when its address changes.
The backend image contains no frontend files and provides no UI routes.

CI builds and tags two images with the same Git SHA, tests the frontend
container including API replacement, and deploys both. The deployment state
records both image references so rollback restores both. The deploy script
also supports rollback to an earlier combined image during migration.
`/frontend-health` checks Nginx; `/health` checks backend readiness.

For local frontend container validation run
`docker build -t financial-semantic-frontend:test frontend` followed by
`bash scripts/test_frontend_container.sh financial-semantic-frontend:test`.
The latter uses an isolated disposable API stub; production deployment gates
always use the real API. Run JS tests with
`node --test tests/frontend/*.test.cjs`.

For API-only development, configure data stores as documented in the main
README and run `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`.
The older host Nginx and systemd examples are historical references; the
container config in `frontend/nginx.conf` is authoritative for the UI.

## Server prerequisites and stable paths

- Ubuntu 24.04 LTS, Docker Engine, and Docker Compose v2
- checkout: `/opt/financial-semantic-agent/current`
- server-local configuration: `/opt/financial-semantic-agent/current/.env`
- semantic bundle: `/srv/financial-semantic-agent/artifacts/260824`
- v1 artifact: `.../260824/v1/semantic_search.json`
- v2 artifact: `.../260824/canonical_v2/semantic_search.json`
- PostgreSQL and Neo4j: named persistent Compose volumes

The `.env` file must be created manually with mode `0600`. Never copy it into
the image, commit it, print it through `docker compose config`, or attach it to
an acceptance artifact. Start from `.env.example`, replace every placeholder,
and ensure `DATABASE_URL` uses host `postgres` and `NEO4J_URI` uses host
`neo4j`.

Copy both approved semantic artifacts to the stable versioned directory and
record their checksums:

```bash
sha256sum /srv/financial-semantic-agent/artifacts/260824/{v1,canonical_v2}/semantic_search.json
```

The bind mount is read-only. API startup validates the selected artifact
manifest and never regenerates it.

## Build and first deployment

```bash
docker compose -f docker-compose.prod.yml build --pull
docker compose -f docker-compose.prod.yml up -d postgres neo4j
docker compose -f docker-compose.prod.yml run --rm agent-api alembic upgrade head
```

Restore the approved v1 and v2 PostgreSQL data into `postgres-data`. Restore or
build both approved graph projections in `neo4j-data`. A fresh canonical v2
rebuild, when explicitly required, may use a read-only source mount:

```bash
docker compose -f docker-compose.prod.yml run --rm \
  --volume /srv/financial-source:/source:ro \
  agent-api python -m app.data.v2_rebuild --material-root /source

docker compose -f docker-compose.prod.yml run --rm \
  agent-api python -m app.graph.ingest_v2

# Initial projection preparation only: override the normal read-only artifact
# mount for this one command, then restore read-only API operation.
docker compose -f docker-compose.prod.yml run --rm \
  --volume "${SEMANTIC_ARTIFACT_ROOT}:/var/lib/financial-semantic-agent" \
  agent-api python -m app.search.index_v2
```

If a retained v1 bundle is not restored from the approved deployment backup,
prepare it before competition cutover with `python -m app.graph.ingest` and
`python -m app.search.index` using the same one-off artifact mount. Both v1 and
v2 assets must exist before rollback acceptance begins.

Do not perform rebuilds during restart or rollback. Once durable stores and
both semantic artifacts are present, start the API:

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
curl --fail http://127.0.0.1:${API_PORT:-8000}/health
```

Container `running` status is not readiness. The API healthcheck calls
`/health`, which returns HTTP 200 only after the selected RDB snapshot, graph
manifest, semantic manifest, ontology, snapshot, transformer, and projection
versions form one coherent READY bundle. Incompatible state fails closed.

## Persistence and safe lifecycle

Normal recreation preserves named volumes and the host artifact directory:

```bash
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
```

Never use `docker compose down --volumes` in production. Before deployment,
configure and test PostgreSQL/Neo4j backups independently of Compose.

## Restart recovery

```bash
docker compose -f docker-compose.prod.yml restart agent-api
docker compose -f docker-compose.prod.yml ps --wait agent-api
curl --fail http://127.0.0.1:${API_PORT:-8000}/health
```

`restart: unless-stopped` recovers the API after process or host restart.

## v1/v2 rollback

Both database generations, both graph projections, and both semantic artifacts
must remain available. Edit only this line in the server-local `.env`:

```text
RUNTIME_DATA_VERSION=v1
```

Compose `restart` alone does not reload an env file, so recreate only the API:

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate agent-api
curl --fail http://127.0.0.1:${API_PORT:-8000}/health
```

Change it back to `v2` and repeat the same command. No migration, source
ingestion, PostgreSQL rebuild, graph rebuild, or semantic rebuild is allowed in
this rehearsal.

## Acceptance sequence

1. Build from a clean checkout with `docker compose ... build --pull`.
2. Start with `docker compose ... up -d` and wait for service healthchecks.
3. Verify `/health` reports the exact selected bundle and READY stores.
4. Run `/answer` and `scripts/smoke_test.py` from outside the container.
5. Restart `agent-api` and verify readiness recovery.
6. Rehearse `v2 -> v1 -> v2` by API recreation only.
7. Run `down` then `up -d` without `--volumes`; reconcile counts and manifests.
8. Inspect image history/config and logs for secret leakage.
9. From outside Naver Cloud, test the public HTTPS `/health` and URL-encoded
   Korean `/answer` without custom authentication headers.

Suggested secret checks do not print the resolved Compose environment:

```bash
docker history --no-trunc "${AGENT_IMAGE}:${AGENT_IMAGE_TAG}"
docker compose -f docker-compose.prod.yml logs agent-api
git ls-files | grep -E '(^|/)(\.env|.*\.(pem|key))$' && exit 1 || true
```

## Firewall

Publish only the API/load-balancer port. Do not add `ports` entries to
PostgreSQL or Neo4j. Restrict SSH to administrator networks and use the Naver
Cloud firewall/security group for public 80/443 as appropriate. Record the
stable public HTTPS endpoint in README only after outside-network validation.

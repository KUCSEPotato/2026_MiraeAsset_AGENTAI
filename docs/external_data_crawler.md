# Trusted External Data Crawler

## Scope

This module is the source-acquisition boundary for external financial evidence.
It downloads and preserves source material, records provenance and produces
versioned source-level records. It does not create canonical products,
companies, organizations, ontology assertions, Neo4j edges or recommendations.

Current milestone: **Crawl-1 foundation plus a KODEX-only Crawl-2 holdings
adapter**. The adapter implements only the confirmed Samsung Asset Management
contract and must not be generalized to other managers. Public-fund holdings,
other ETF managers, corporate subsidiaries and temporal-document adapters are
not yet implemented.

## Data flow

```text
Trusted External Source
        ↓
robots.txt / access-policy check
        ↓
rate-limited conditional HTTP fetch
        ↓
immutable raw artifact + SHA-256
        ↓
deterministic source-level record
        ↓
validation / quality status
        ↓
versioned snapshot manifest
        ↓
future Main Agent canonicalization
```

The crawler never imports or writes the `canonical_v2` schema. Canonical Entity
Resolution and ontology mapping happen after this boundary.

## Directory structure

Generated data is ignored by Git and defaults to `external_data/`.

```text
external_data/
├── cache/http/                         # ETag/Last-Modified cache
├── objects/<sha-prefix>/<sha256>       # content-addressed immutable bytes
└── snapshots/YYYY-MM-DD/<snapshot-id>/
    ├── manifest.json
    └── <category>/
        ├── raw/<sha256>.<extension>     # hard link or safe copy of object
        └── normalized/source_records.jsonl
```

Snapshot directories are created with exclusive semantics. An existing
snapshot ID is never overwritten. Content-addressed objects let unchanged bytes
be reused across snapshots while every snapshot retains a direct artifact path.
Repeating the CLI with the same explicit snapshot ID, provider and URL returns
the existing `READY` manifest without new rows or files. Any differing or
unfinished request must use a new snapshot ID rather than mutate history.

## Common SourceRecord

Schema identifier: `external-source-record-v1`.

Required provenance fields:

- `source_record_id`
- `source_provider`
- `source_type`
- `source_trust_tier`
- `source_url` and `normalized_url`
- `retrieved_at`
- nullable `published_at` and `effective_date`
- nullable `source_title`
- `content_type` and nullable `http_status`
- `raw_content_hash`
- `parser_version` and `crawler_version`
- `snapshot_id`
- `quality_status`
- snapshot-relative `raw_artifact_path`
- optional `etag`, `last_modified` and source metadata

`source_record_id` is deterministic over provider, source type, normalized URL
and content hash. An unchanged document therefore has the same source record ID
across snapshots, but each record still carries its own snapshot and retrieval
time. The model forbids undeclared fields, including canonical Agent IDs.

Timestamp rules:

- `retrieved_at` is always a timezone-aware UTC instant.
- `published_at` is nullable and must be timezone-aware when present.
- `effective_date` is nullable.
- retrieval time is never substituted for publication/effective time.

Downstream schema versions:

- `external-holdings-v1`
- `external-corporate-v1`
- `external-document-v1`

`external-holdings-v1` is implemented only for the confirmed KODEX source
contract. The corporate and document schemas remain reserved for later
milestones.

## Source trust policy

Every source record carries one of these tiers:

1. `AUTHORITATIVE`: regulator, exchange, official issuer/manager/company
2. `TRUSTED_FINANCIAL`: established financial information provider
3. `SUPPORTING_WEB`: news or general web supporting temporal evidence

The tier is supplied by an explicit source configuration. The HTTP client does
not infer trust from a hostname. Later source adapters must be reviewed before a
provider/tier combination is activated.

Tier 3 evidence cannot independently establish holdings, subsidiaries,
canonical identity or numeric product facts.

## Manifest

Schema identifier: `external-snapshot-manifest-v1`.

The manifest is written atomically when the workspace is created and after each
state change. It contains:

- snapshot ID/date and creation/completion timestamps
- crawler and parser versions
- `BUILDING`, `READY`, `FAILED` or `PARTIAL` status
- providers and URLs
- every raw artifact path, checksum and byte count
- every normalized output checksum, schema version and row count
- source-record and failure counts
- structured fetch/parse/validation failures
- per-source quality reports
- explicit validation results

Only `READY` is eligible for later canonical ingestion. The generic Crawl-1 CLI
uses `READY` only when the requested source was fetched, preserved and validated
as a SourceRecord. Source adapters may define stricter policies; partial
snapshots must never silently become ready.

## HTTP client

Implementation: `app/external_data/http.py` using `httpx.AsyncClient`.

| Control | Default |
|---|---:|
| Timeout | 20 seconds |
| Retries after initial attempt | 3 |
| Backoff | 1, 2, 4 seconds; capped at 30 |
| Request interval | 1 second globally per client |
| Bounded concurrency | 2; hard validation maximum 8 |
| Maximum response | 50 MiB |
| Redirects | followed by httpx |
| robots.txt | enforced by default |

Retryable statuses are 429, 500, 502, 503 and 504. Timeout and network errors
are retryable. Other non-2xx statuses fail visibly. Authentication, CAPTCHA,
access controls and anti-bot mechanisms are never bypassed. A robots 401/403 or
robots fetch failure is handled conservatively as blocked; a normal missing
robots file permits access.

The user-agent is explicit and configurable. Browser impersonation, fingerprint
evasion and credential-bearing URLs are rejected.

## HTTP cache and incremental runs

Successful responses store body plus metadata in a URL-keyed cache. Subsequent
requests send `If-None-Match` and/or `If-Modified-Since`. A 304 reuses the cached
bytes after checksum verification. Without validators, the source is fetched
again and content hash determines whether the artifact bytes are unchanged.

Deduplication uses normalized URL plus content SHA-256 within a snapshot. Query
parameters are sorted only for the deduplication key; the original query order
is retained for the actual HTTP request. URL fragments are removed.

A changed body receives a new hash, object and deterministic source record ID.
Historical snapshots remain untouched.

## Raw artifacts

Raw bytes are stored before a normalized SourceRecord is written. Supported
content classification includes HTML, JSON, CSV, XLSX, PDF, XML, text and
binary. No semantic or numeric extraction occurs in Crawl-1.

Artifact filenames are content hashes, not untrusted server filenames. Category
and extension validation prevents path traversal. Writes use a same-directory
temporary file, `fsync` and atomic replace.

## Quality and failures

Source-level statuses:

- `VALID`
- `PARTIAL`
- `BLOCKED`
- `FETCH_FAILED`
- `PARSE_FAILED`
- `VALIDATION_FAILED`

Failures retain URL, normalized URL, provider, stage, status, error type,
message, retry count and timestamp. A failed source does not erase successful
artifacts from the same snapshot. Snapshot readiness remains explicit.

Each source quality report includes provider, tier, access method, data types,
refresh behavior, identity/timestamp availability, limitations, access
constraints and failure rate. Crawl-1 reports its limitation that no domain
parser has yet been applied.

## CLI

Inspect non-secret configuration:

```bash
uv run python scripts/crawl_external.py show-config
```

Create an empty `BUILDING` snapshot:

```bash
uv run python scripts/crawl_external.py \
  --output-dir external_data \
  init-snapshot \
  --snapshot-date 2026-08-30 \
  --snapshot-id manual-foundation-check
```

Fetch and preserve one explicitly reviewed source:

```bash
uv run python scripts/crawl_external.py \
  --output-dir external_data \
  --request-interval 1.0 \
  fetch \
  --snapshot-date 2026-08-30 \
  --provider "Official Provider" \
  --source-type ASSET_MANAGER \
  --trust-tier 1 \
  --category foundation \
  --url "https://official.example/data.json"
```

Publication/effective dates may be passed only when known from the source
contract:

```text
--published-at 2026-08-30T09:00:00+09:00
--effective-date 2026-08-29
```

The future `holdings`, `corporate`, `documents` and `all` commands are not
present yet because their source contracts have not been implemented.

## Configuration

All settings have `.env.example` placeholders and require no credentials:

- `EXTERNAL_CRAWLER_TIMEOUT_SECONDS`
- `EXTERNAL_CRAWLER_MAX_RETRIES`
- `EXTERNAL_CRAWLER_REQUEST_INTERVAL_SECONDS`
- `EXTERNAL_CRAWLER_MAX_CONCURRENCY`
- `EXTERNAL_CRAWLER_USER_AGENT`
- `EXTERNAL_CRAWLER_OUTPUT_DIR`
- `EXTERNAL_CRAWLER_RESPECT_ROBOTS`
- `EXTERNAL_CRAWLER_MAX_RESPONSE_BYTES`
- `EXTERNAL_CRAWLER_VERSION`

If a later official API requires a key, only its empty variable name should be
added to `.env.example`; the credential itself must remain outside Git.

## Offline tests

Fixtures cover HTML, JSON and CSV without Internet access. Tests use
`httpx.MockTransport` to verify:

- strict SourceRecord fields and nullable timestamps
- raw-before-normalized provenance
- snapshot immutability and deterministic IDs
- content-addressed cross-snapshot artifact reuse
- URL/content deduplication
- ETag conditional cache and 304 reuse
- robots allow/deny behavior
- bounded retry and exponential backoff
- failure recording and absence of canonical fields

Run:

```bash
uv run pytest tests/external_data/test_foundation.py
```

Live crawling is not part of the deterministic unit suite and must be opt-in in
future source-specific tests.

## Crawl-2 entry criteria

The foundation is ready for ETF Holdings source-contract work when:

- a specific Tier 1 provider and its terms/robots policy are reviewed;
- ETF and constituent source identifiers are documented;
- weight unit/scale semantics are documented rather than guessed;
- effective-date/current-vs-historical semantics are confirmed;
- representative raw fixtures may be retained legally;
- schema drift behavior is defined to fail visibly.

Meeting these criteria authorizes a holdings adapter; it does not authorize
canonical entity merging or canonical_v2 writes.

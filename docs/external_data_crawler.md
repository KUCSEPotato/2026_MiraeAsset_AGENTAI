# Trusted External Data Crawler

## Scope

This module is the source-acquisition boundary for external financial evidence.
It downloads and preserves source material, records provenance and produces
versioned source-level records. It does not create canonical products,
companies, organizations, ontology assertions, Neo4j edges or recommendations.

Current milestone: **Crawl-1 foundation plus reviewed KODEX and TIGER Crawl-2
holdings adapters**. Each adapter has an explicit provider contract; shared
normalization does not infer one provider's identifiers, weight units, or date
semantics from the other. Public-fund holdings, foreign ETF holdings, corporate
subsidiaries and temporal-document adapters are not yet implemented.

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

Snapshot directories are created with exclusive semantics. `READY` and
`FAILED` snapshots are immutable. A `BUILDING` or `PARTIAL` production-crawl
snapshot may be explicitly resumed: existing exact raw artifacts, SourceRecords
and semantic rows are loaded and validated before append-only acquisition
continues. Content-addressed objects let unchanged bytes be reused while every
snapshot retains a direct artifact path.

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

`external-holdings-v1` is implemented for the confirmed KODEX and TIGER source
contracts. The corporate and document schemas remain reserved for later
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

Run deterministic KODEX catalog discovery, exact PREF01 universe resolution and
historical holdings acquisition:

```bash
uv run python scripts/crawl_external.py \
  --output-dir external_data \
  --request-interval 1.0 \
  kodex-holdings \
  --snapshot-date 2026-08-31 \
  --snapshot-id kodex-production-20260824 \
  --cutoff 2026-08-24
```

The command never imports or writes `canonical_v2`. `--product-id` is an exact
fId subset intended only for reviewed live contract probes. The production run
omits it and crawls every catalog product deterministically matched to PREF01.

Run the independent TIGER historical adapter over its exact authoritative
PREF01/ISIN universe. Passing the accepted KODEX snapshot also derives the
reviewable TIGER long-only scope without writing PostgreSQL:

```bash
uv run python scripts/crawl_external.py \
  --output-dir external_data \
  --request-interval 1.0 \
  tiger-holdings \
  --snapshot-date 2026-09-01 \
  --snapshot-id tiger-production-20260824-v1 \
  --cutoff 2026-08-24 \
  --kodex-snapshot-root external_data/<kodex-production-snapshot>
```

The official request uses the exact product ISIN and requested effective date.
`retrieved_at` is never used as the portfolio date. TIGER reports portfolio
weights as percent of NAV; cash and other non-security rows remain raw evidence
and do not become `Security` or `HOLDS` facts.

KODEX semantic holding IDs use provider, fId, effective portfolio date,
stable constituent identity and holding grain. Exact raw hashes and
SourceRecord IDs are deliberately excluded. `holdings.jsonl` contains the
stable semantic projection; `holding_evidence_links.jsonl` maps one holding to
one or more exact SourceRecords. Volatile `curp`, `risep`, `rcvTime`, retrieval
time and raw hash therefore cannot change the semantic checksum.

Three consecutive fetch failures stop the current pass and finalize `PARTIAL`
instead of continuing to pressure a rate-limited provider. Per-pass product
results are persisted after every product, so the same command can resume after
a provider-safe cooldown without repeating successful terminal work.

### KODEX READY policy

A KODEX snapshot is `READY` only when all of these are true:

- strict manifest serialize/write/load round-trip succeeds;
- the full exact-resolved PREF01 universe has an accounted terminal status;
- at least one holding source succeeds;
- no schema/normalization or cutoff-unverified failures remain;
- accounted fetch failures are at most 5% of eligible products;
- a second pass has identical holding IDs, count and semantic checksum;
- every normalized holding has a valid evidence link to a stored SourceRecord;
- every normalized effective date is on or before 2026-08-24;
- catalog matched/ambiguous/unmatched coverage is explicit;
- `canonical_v2_writes` remains zero.

`IDENTITY_UNRESOLVED` catalog products and `NO_HOLDINGS` products are allowed
only when explicitly reported. Unknown/unattempted products, normalization
contract violations, or unverified temporal semantics force `PARTIAL`.

### KODEX long-only compatible scope

The full production snapshot remains `PARTIAL`. A separate logical scope,
`KODEX_LONG_ONLY_COMPATIBLE`, may be `READY` without copying or changing its
raw evidence. Build (and optionally integrate) that scope with:

```bash
uv run python scripts/activate_kodex_scope.py \
  --snapshot-root external_data/<snapshot> \
  --database-url "$DATABASE_URL"
```

Eligibility is product-level and all-or-nothing. The source response must be
complete and cutoff-compatible; every security row must have a deterministic
six-digit KRX identity, verified weight semantics, non-negative quantity and
evaluated value, and no unsupported derivative identifier. Cash rows are
preserved as non-security evidence. One incompatible or unresolved portfolio
row blocks the whole product; rows are never silently dropped to make a
portfolio appear complete.

The scoped manifest references the parent manifest, normalized holdings,
holding-evidence links, SourceRecords, and raw checksums. It does not duplicate
raw artifacts. Runtime holdings traversal is allowed only when the parsed
universe is exactly the READY scope. `KODEX_FULL` and `DomesticETF` remain
`PARTIAL`; generic `ForeignETF` is `PARTIAL` after C2.8 and `PublicFund`
remains `NOT_READY`.

### TIGER long-only compatible scope

`TIGER_LONG_ONLY_COMPATIBLE` uses the same product-level all-or-nothing safety
policy but its own provider contract and immutable scope manifest. Build and
optionally integrate it with:

```bash
uv run python scripts/activate_tiger_scope.py \
  --snapshot-root external_data/<tiger-production-snapshot> \
  --kodex-snapshot-root external_data/<kodex-production-snapshot> \
  --database-url "$DATABASE_URL"
```

Only exact six-digit KRX Security positions are eligible. Unsupported
derivative/short identities block the complete product rather than being
dropped. Runtime can select either READY provider scope or their bounded union;
`TIGER_FULL` and generic `DomesticETF` coverage remain `PARTIAL`.

### iShares foreign-ETF Security-holdings scope

The C2.8 adapter uses BlackRock/iShares' official date-qualified fund-document
CSV API. The response's `Fund Holdings as of` value is authoritative;
`retrieved_at` and a current `latest-holdings.csv` response are never accepted
as a historical date. Product identity is reconciled to PREF02 by ISIN first,
then ticker plus exchange. Crawl the reviewed source candidates with:

```bash
uv run python scripts/crawl_external.py \
  --output-dir external_data/c2_8 \
  ishares-holdings \
  --snapshot-date 2026-09-01 \
  --snapshot-id ishares-production-20260824-v1 \
  --cutoff 2026-08-24 \
  --portfolio-date 2026-07-31
```

Constituent identity is ISIN when supplied, otherwise official exchange/MIC
plus ticker. A bare foreign ticker or name cannot create a Security. Cash,
money-market, FX and derivative positions remain classified source evidence;
an unresolved Equity or unknown instrument blocks the entire product. The
source candidate snapshot remains `PARTIAL`, while the evidence-backed
`ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS` subset may be `READY`:

```bash
uv run python scripts/activate_ishares_scope.py \
  --snapshot-root external_data/<ishares-production-snapshot> \
  --database-url "$DATABASE_URL"
```

This scope does not activate all iShares or all `ForeignETF` products.
`ISHARES_US_FULL` and generic `ForeignETF` remain `PARTIAL`. Foreign issuer
relations are not inferred from a holding name. KRX constituents may reuse the
separate authoritative KRX KIND issuer relation when that snapshot is READY.

### KRX Security issuer evidence

Company-name holdings queries use a separate authoritative relation snapshot.
The crawler requests KRX KIND listed-company status for the exact evaluation
cutoff and preserves the KOSPI, KOSDAQ, and KONEX HTML responses before writing
source-grain `ExternalSecurityIssuerRecord` rows:

```bash
uv run python scripts/crawl_external.py \
  --output-dir external_data/c2_6_issuer \
  krx-security-issuers \
  --snapshot-date 2026-08-31 \
  --snapshot-id krx-kind-security-issuers-20260824-v2 \
  --kodex-snapshot-root external_data/<kodex-production-snapshot> \
  --tiger-snapshot-root external_data/<tiger-production-snapshot> \
  --cutoff 2026-08-24
```

Security identity is exact six-digit KRX ticker identity. Organization reuse
requires an exact-one deterministic legal-name match; otherwise an
authoritative KRX `isurCd` identity may create a source-scoped Organization.
Name collisions remain `AMBIGUOUS`, and non-representative preferred-share
tickers absent from the company-grain response remain `UNRESOLVED`. No ticker
prefix, fuzzy-name, or LLM inference is permitted.

After review, integrate the immutable snapshot with:

```bash
uv run python scripts/activate_krx_issuers.py \
  --snapshot-root external_data/c2_6_issuer/snapshots/<date>/<snapshot-id> \
  --multi-provider \
  --database-url "$DATABASE_URL"
```

The projection uses the existing canonical fact/evidence machinery and is
idempotent. Runtime company-name traversal additionally requires all four
issuer readiness gates and `TRUSTED_ISSUER_RUNTIME_ENABLED=1`.

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

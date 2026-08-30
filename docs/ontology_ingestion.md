# Ontology-guided ingestion

The ingestion layer treats the RDB as the source of truth. Every accepted Excel
row becomes a `source_records` row. Present values become sparse
`source_field_assertions`; missing values remain exactly recoverable from the
SourceRecord JSON payload plus the complete mapping registry. Typed
identifiers, observations and relations are derived only from the exact
`dataset + table + column` registry in
`ontology/mappings/column_mapping.csv`.

## Commands

```bash
# all datasets, no writes
uv run python -m app.data.ingest --material-root material --dry-run

# one dataset, no writes
uv run python -m app.data.ingest --material-root material \
  --datasets PREF01N001 --dry-run

# sample integration load
uv run python -m app.data.ingest --material-root material \
  --database-url postgresql+psycopg://financial_agent:change-me@localhost:5432/financial_agent \
  --limit 100

# full load plus ontology/SHACL gate and semantic-search document preparation
uv run python -m app.data.ingest --material-root material --shacl \
  --prepare-search-documents --graph-projection
```

`--datasets` accepts comma-separated source codes or canonical dataset names.
`--batch-size`, `--limit`, and `--report-file` control execution and reporting.
The graph flag does not require Neo4j: approved provenance-bearing relation rows
are prepared in RDB for a later adapter. Semantic documents contain source text,
never numeric observations. Re-running the same snapshot replaces that dataset
inside a transaction, preserving stable IDs and preventing duplicates.
Before replacement, the loader compares SHA-256 fingerprints for the data file,
schema file and mapping registry together with the transformer version, dataset
code and snapshot. An exact match records `SKIPPED_UNCHANGED` and performs no
canonical or derived-data writes.

`field_coverage_stats` stores total, present, missing, invalid and sentinel counts
for each dataset/snapshot/column. Coverage queries therefore do not scan the
assertion table. Product validation is dataset-specific: Korean bond/ETP/fund
keys require the official 12-character form, foreign RICs retain their distinct
2-32 character syntax, placeholder-only names are rejected, and ETP rows must
resolve unambiguously to ETF or ETN. Rejected rows retain run ID, row, source
key, raw name, code, reason and raw payload in `quarantine_records`.

## Identity and uncertainty

- Bond source records use `pd_no + pd_exg_mkt + info_base_dt + info_seq`; products
  use `pd_no`.
- Domestic and foreign ETP records/products use `pd_itm_no`.
- Fund records/share classes use `itm_no`. It is not promoted to a portfolio ID.
- `pd_itm_no_ma` (foreign RIC_PDF metadata), CIK, sentinels, and representative
  portfolio KSD IDs are preserved as assertions but do not identify the row's
  product.
- No fund portfolio/share-class edge is created because the latest source lacks
  a confirmed join key.
- Units and dates are retained only where the mapping/source supplies them.

The compatibility tables (`canonical_products`, product-specific tables and
`product_identifiers`) remain available to existing APIs. Time-varying values in
those tables are search projections; `metric_observations` plus provenance are
authoritative for evidence and latest-value selection.

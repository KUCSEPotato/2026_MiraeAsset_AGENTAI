# M10.9-C3 Foreign ETF one-year return contract

## Domestic source audit

PREF01N001 defines `du_er_1y` as `수익률_1Y`, numeric `(28,2)`, at
ExchangeTradedProduct grain. The organizer schema does not state whether this
is NAV return, market-price return, or total return, and does not define
distribution adjustment. Runtime therefore preserves the source percentage
points without rescaling and compares them only within PREF01N001.

PREF02N001 contains `du_er_1d` (`수익률_1D`, numeric `(28,6)`) but no one-year
return field. A 1D value is never relabelled as `ONE_YEAR_RETURN`.

## Reviewed iShares contract

| Dimension | Contract |
|---|---|
| Scope | `ISHARES_FOREIGN_ETF_ONE_YEAR_RETURN` |
| Products | EWY, IYW, SOXX |
| Provider | BlackRock/iShares official product performance data |
| Metric | issuer-published average annual 1-year return, `navSourced` |
| Grain | canonical ETF / FinancialProduct |
| Observation end | 2026-07-31 |
| Observation start | not separately published; no reconstruction performed |
| Unit | percent |
| Scale | published percentage points; no rescaling |
| Return basis | NAV total return |
| Distributions | included |
| Currency semantics | source products report USD; percentage return is not FX-converted |
| Product resolution | ISIN, then ticker+MIC, then approved provider source ID; exact only |
| Cutoff | observation end must be on or before 2026-08-24 |

The official methodology text, aligned return arrays, product ID, currency,
and requested observation date are strict schema checks. Any change fails the
acquisition instead of producing a canonical metric.

## Comparability decision

The iShares products are mutually comparable within the reviewed scope. They
share provider, metric basis, period, end date, unit, scale, grain, and
distribution treatment. Generic `ForeignETF` remains `PARTIAL` because only
three of the 5,972 PREF02 ETFs are covered.

Domestic PREF01 and iShares values are not authorized for one ordered ranking.
Although period, grain and unit are compatible, PREF01 does not document NAV
versus market-price basis or distribution treatment. The cross-source
ComparisonContract is therefore `NO` with reason
`domestic_vs_ishares_return_basis_not_comparable`.

## Provenance and persistence

```text
metric_observations
  -> canonical_facts
  -> fact_evidence_links
  -> source_field_assertions
  -> external_metric_records
  -> external_source_records
  -> external_raw_artifacts
  -> external_snapshot_manifests
```

The canonical semantic metric identity excludes `retrieved_at`. Repeated
integration uses one canonical fact and one evidence link per observation.
Raw artifacts remain outside Git under the immutable external snapshot root.

# M10.9-C3.1 domestic/iShares one-year return comparison

## Decision

Cross-source `ONE_YEAR_RETURN` ordering is `NOT_READY`. No READY
`ComparisonContract` is created for the KODEX, TIGER, and iShares union. The
existing PREF01-scoped domestic ranking and three-product iShares ranking stay
READY independently.

The stop condition is reached because the authoritative PREF01 contract does
not define several dimensions that materially affect ordering. `UNKNOWN` is
not treated as compatible.

## PREF01 `du_er_1y` audit

The preserved authoritative schema workbook
`PREF01N001_20260824_schema.xlsx` supplies only these facts:

- field: `du_er_1y`
- label: `수익률_1Y`
- type: `numeric(28,2)`
- product grain: PREF01 exchange-traded product row

The canonical loader preserves the supplied number as percentage points and
does not rescale it. The dataset does not document whether the value is
annualized or cumulative, NAV- or market-price-based, total or price return,
distribution-adjusted, or measured in a particular currency/valuation basis.
Those dimensions are therefore `UNKNOWN`, not inferred from the field name or
nearby NAV and price columns.

The return observation currently uses the row's `du_upt_dt` daily update date.
This is a source update/as-of proxy, not a documented performance period end.
In the preserved 2026-08-24 workbook the populated return rows are not all on
one update date: 1,257 are dated 2026-08-21, 95 have no `du_upt_dt`, and 65
carry other dates from 2026-04-10 through 2026-08-20. Consequently the source
also does not prove exact observation-date alignment for every candidate.

## iShares contract

The accepted C3.0 contract is unchanged:

- field: `performance.returns.average.oneYearAnnualized.navSourced`
- product grain: canonical ETF / FinancialProduct
- period: exact trailing one year, issuer-published one-year value
- basis: NAV total return
- distributions: included
- unit and scale: percent, published percentage points without rescaling
- observation date: 2026-07-31
- currency: product reporting currency USD; no FX conversion
- calculation: official published value, not reconstructed

## Compatibility matrix

| Dimension | Classification | Reason |
|---|---|---|
| Product grain | COMPATIBLE | Both observations attach to an ETF/ETP product. |
| Period | COMPATIBLE | Both source labels explicitly identify one year. |
| Annualized/cumulative | UNKNOWN | PREF01 does not define the calculation convention. |
| NAV/market-price basis | UNKNOWN | PREF01 does not identify the price basis. |
| Total/price return | UNKNOWN | PREF01 does not define reinvestment or total-return methodology. |
| Distribution treatment | UNKNOWN | PREF01 does not state whether distributions are included. |
| Unit | COMPATIBLE | Both are represented as percent. |
| Scale | COMPATIBLE | Both are stored as supplied percentage points without rescaling. |
| Observation timing | MATERIAL_DIFFERENCE | iShares is 2026-07-31; PREF01 uses non-uniform `du_upt_dt` values and does not document a common performance end. |
| Currency | UNKNOWN | PREF01 does not state the return currency semantics. |
| Valuation basis | UNKNOWN | PREF01 methodology is absent. |
| Other provider methodology | UNKNOWN | No organizer methodology sufficient for cross-provider ordering is preserved. |

No existing allowed observation-date tolerance applies, and none is invented
for C3.1. No FX normalization is introduced.

## Runtime and coverage consequence

`MetricCapabilityRegistry` continues to reject the exact three-scope union
with `domestic_vs_ishares_return_basis_not_comparable`. Therefore the requested
Samsung Electronics company probe and direct `005930` Security probe are
unsupported before execution; no candidate counts or ranking are emitted.
This is intentional fail-closed behavior rather than a zero-match result.

Coverage remains KODEX full `PARTIAL`, TIGER full `PARTIAL`, DomesticETF
`PARTIAL`, selected iShares scope `READY`, ForeignETF `PARTIAL`, and PublicFund
`NOT_READY`.

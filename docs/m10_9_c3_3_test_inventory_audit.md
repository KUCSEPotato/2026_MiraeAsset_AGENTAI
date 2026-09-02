# M10.9-C3.3 test inventory audit

## Result

No committed C3.0 test file is missing.

`git ls-tree -r 1b65bb3 tests` contains 30 `test_*.py` files. The current
workspace contains all 30, plus:

- `tests/test_rdb_retriever.py`, restored as the row-factory support module
  imported by three existing integration suites;
- `tests/test_m10_9_c3_2_release.py`, added for the production artifact gate.

The C3.2 collection after the repair was 294 tests. The exact committed C3.0
tree cannot collect independently in a clean checkout because its tracked
tests import `tests.data_helpers`, `tests.evidence_helpers`, and
`tests.test_rdb_retriever`, while `.gitignore` excluded those modules and
`tests/__init__.py`. C3.2 restored that support surface and produced zero
collection errors.

## File-level comparison

There are no paths in C3.0's test-file set that are absent now:

```text
C3.0-only test files: 0
current-only test files: 2
common test files: 30
```

Pytest configuration remains `testpaths = ["tests"]` with no default marker
filter. Markers only classify PostgreSQL, Neo4j, and live HyperCLOVA tests.
Environment variables affect runtime skip decisions, not discovery of test
files.

## Count discrepancy

The reported `442 passed, 119 skipped` cannot be a single clean collection of
commit `1b65bb3`: it totals 561 outcomes while that commit's available test
files, with the locally restored support modules, collect 291 cases before the
three C3.2 cases are added. No Git ref contains an additional committed test
inventory that accounts for the difference.

The only evidence-consistent explanation is that the earlier number was an
aggregate across multiple invocations/configurations (for example baseline,
PostgreSQL, Neo4j, and repeated acceptance runs) and depended on ignored local
test-support state. It must not be compared to one `pytest --collect-only`
result as if both were unique case counts.

C3.3 uses these non-overlapping report lines:

- collection: unique cases discovered by one collection command;
- full runnable suite: one unfiltered repository invocation;
- separately configured integrations: reported per invocation, never added to
  the unique collection count.

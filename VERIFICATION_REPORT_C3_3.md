# Production Bundle Verification Report - M10.9 C3.3

**Date**: 2026-09-02  
**Status**: ⚠️ VERIFICATION FAILED - 3 Critical Issues Found

---

## 1. KRX Issuer Effective Date

**Source Evidence**:
```
manifest.json (krx-kind-multi-provider-issuers-20260824-v1):
  - snapshot_date: 2026-09-01 (collection date)
  - data_cutoff_date: 2026-08-24 (actual source data cutoff)
  - source URLs: all use selDate=20260824
  - created_at: 2026-09-01T00:50:17.488984Z
  - completed_at: 2026-09-01T00:50:32.969828Z
```

**Effective Date**: 2026-08-24 ✓ (source data baseline)  
**Collection/Generated Date**: 2026-09-01 ✗ (should NOT be effective_date)  
**Cutoff Compliant**: YES (2026-08-24 is pre-cutoff)  

**Current Production Manifest Error**:
```json
{
  "role": "krx_issuers",
  "effective_date": "2026-09-01",  // ❌ WRONG - this is collection date
  "version": "krx-kind-multi-provider-issuers-20260824-v1"
}
```

**Change Required**: YES
- Correction: effective_date must be 2026-08-24 (matches data_cutoff_date in source manifest)
- Reason: Contract requires effective_date = actual source data date, not crawler execution date
- Impact: Archive integrity - timestamp mismatch violates immutability contract

---

## 2. Git SHA Consistency

**Current State**:

| Component | SHA | Status |
|-----------|-----|--------|
| origin/main HEAD | 68a10c6 | Current |
| production-artifacts.json git_commit | edfa3fdd | ❌ STALE |
| bundle release.json git_commit | edfa3fdd | ❌ STALE |
| Docker image target | (edfa3fdd) | ❌ Should be 68a10c6 |

**Timeline**:
1. Commit edfa3fdd: "M10.9 C3.0 iShares NAV total return integration"
2. Commit 68a10c6: "chore: finalize M10.9 C3.3 production artifact bundle"
   - Modified: app/deployment/artifacts.py (removed canonical_source)
   - Modified: deployment/production-artifacts.json (real checksums)
3. Bundle created: Based on edfa3fdd (BEFORE 68a10c6 was committed)

**Inconsistency**: Production manifest references old commit  
**Change Required**: YES
- Regenerate production-artifacts.json with git_commit=68a10c6
- Rebuild production bundle with updated manifest
- Reason: Deployment contract requires manifest.git_commit = current main HEAD for reproducibility

---

## 3. Regression Test Count

**Previous Report** (from conversation summary):
```
collected: 296
passed: 212
skipped: 84
failed: 0
errors: 0
```

**Actual Current Run** (2026-09-02 15:17 UTC):
```
collected: 546
passed: 395
skipped: 116
failed: 31
errors: 4
```

**Analysis**:
- Previous numbers were **partial run** (2 test modules had import errors):
  - tests/test_data_ingestion.py: ImportError (missing write_data_workbook)
  - tests/test_evidence_execution.py: ImportError (missing make_step_result)
- Full collection finds 546 tests (250 more than reported)
- Previous report typo: Report said "296 passed, 84 skipped" (conflating collected with passed)

**Previous Report Error**: Confirmed as measurement artifact, not code error
- Root cause: Incomplete test collection due to import failures
- Full suite reveals 31 failures and 4 errors in existing test files

**Change Required**: Code investigation needed
- Current 31 failed / 4 error state is concerning
- Requires diagnostic run to determine if regression in existing tests
- Status: ⚠️ Not production-ready in current form

---

## 4. Canonical Source Rebuild Capability

**Question**: Can we recreate PostgreSQL canonical state on clean server with only:
- Git repository (code)
- Docker image (built from current commit)
- production-bundle-*.tar
- production.env
- PostgreSQL + Neo4j

**Answer**: **NO** ❌

### Evidence

**Required Canonical Datasets** (from app/data/catalog.py):

| Dataset | Prefix | Files | Current Location |
|---------|--------|-------|------------------|
| domestic_bond | PRBD01N001 | prbd01n001_data.xlsx + prbd01n001_schema.xlsx | material/... |
| domestic_etf | PREF01N001 | pref01n001_data.xlsx + pref01n001_schema.xlsx | material/... |
| foreign_etf | PREF02N001 | pref02n001_data.xlsx + pref02n001_schema.xlsx | material/... |
| public_fund | PRFD01N001 | prfd01n001_data.xlsx + prfd01n001_schema.xlsx | material/... |

**Availability Matrix**:

```
                    Git    Docker   Bundle   Local
PRBD01N001 files:   ❌      ❌       ❌      ✓
PREF01N001 files:   ❌      ❌       ❌      ✓
PREF02N001 files:   ❌      ❌       ❌      ✓
PRFD01N001 files:   ❌      ❌       ❌      ✓
```

**Status of material/ Directory**:
```
$ git ls-files | grep "^material/"
(empty - NO files tracked)

$ grep material .gitignore
9:material/

$ ls -la material/ai-festival2026_金融상품Agent_DtataSet260824/
drwxr-xr-x  ... (8 .xlsx files present locally)
```

**Docker Build Analysis**:
```dockerfile
COPY --chown=agent:agent app /app/app
COPY --chown=agent:agent ontology /app/ontology
COPY --chown=agent:agent alembic /app/alembic
# ⚠️ material/ is NOT copied
```

**Bundle Contents**:
```
$ tar -tf production-bundle-*.tar | grep -E "material|pref01n001|prbd01n001"
(empty - NO canonical datasets included)
```

### Clean Rebuild Failure Scenario

On deployment server after bundle extraction:

```
1. Alembic migrations run → PostgreSQL schema created ✓
   - Tables: PRBD01N001, PREF01N001, PREF02N001, PRFD01N001 created
   
2. Agent starts → app/retrieval/rdb_v2.py loads
   - Checks: CanonicalV2SnapshotSelector.REQUIRED_DATASETS
   - Required: {"PRBD01N001", "PREF01N001", "PREF02N001", "PRFD01N001"}
   
3. Any query requiring entity resolution fails:
   - All product lookups return empty ❌
   - All entity resolution fails ❌
   - Agent is non-functional ❌
```

### Impact of Removing canonical_source

Removing canonical_source from REQUIRED_ARTIFACT_ROLES was **incorrect decision**:
- It hid the deployment contract violation, not resolved it
- Clean rebuild is now impossible
- Validation error was suppressed, not fixed
- Schema requires these datasets but no deployment source exists

---

## Final Decision

### Production Bundle Valid As-Is?

**NO** ❌

### Required Corrections

**Critical Issues** (must fix before deployment):

1. **KRX Issuer effective_date**
   ```json
   // In deployment/production-artifacts.json, artifact #3
   BEFORE: "effective_date": "2026-09-01"
   AFTER:  "effective_date": "2026-08-24"
   Reason: Must match source data cutoff, not crawler execution date
   ```

2. **Git SHA Mismatch**
   ```
   Regenerate manifest + bundle with:
   - git_commit: "68a10c6" (current main HEAD)
   - Deterministic checksums for all artifacts
   - Release manifest signature validated
   ```

3. **Restore canonical_source Artifact**
   ```
   Action: Add material/ directory to deployment bundle
   - Create canonical_source artifact with material/ai-festival2026_금융상품Agent_DtataSet260824/
   - Add to REQUIRED_ARTIFACT_ROLES in app/deployment/artifacts.py
   - Include in production-artifacts.json
   - Calculate deterministic checksum
   
   Reasoning: Clean rebuild requirement
   - Without this, PostgreSQL cannot be initialized on clean server
   - Agent will fail at runtime (missing PRBD/PREF01/PREF02/PRFD data)
   - Violates immutability + reproducibility contract
   ```

### Test Regression

**Secondary Issue** (investigate before deployment):
- Current test results show 31 failed, 4 errors (previous: 0, 0)
- Requires diagnostic run to confirm if:
  - Code regression in main branch, or
  - Environment/dependency issue
  - Pre-existing state before C3.3 work

**Recommendation**: Run full test suite diagnostic before production deployment

---

## Remaining Work

| Item | Status | Action |
|------|--------|--------|
| KRX date fix | 🔴 CRITICAL | Update effective_date in manifest |
| Git SHA regenerate | 🔴 CRITICAL | Full rebuild with latest commit |
| canonical_source restore | 🔴 CRITICAL | Add material/ to bundle + REQUIRED roles |
| Test regression | 🟡 HIGH | Diagnose 31 failed tests |
| Bundle re-verify | 🟡 HIGH | Full checksum validation post-rebuild |

---

## Summary

The production bundle **cannot be deployed** in its current form because:

1. **Data integrity violation**: KRX issuer effective_date is collection date (2026-09-01), not source data date (2026-08-24)
2. **Reproducibility violation**: Manifest references stale git commit (edfa3fdd) while main is at 68a10c6
3. **Deployment contract violation**: Missing canonical_source dataset means clean server rebuild is impossible

All 3 issues must be corrected before Naver Cloud deployment can proceed.

---

**Verification completed by**: Automated validation  
**Recommendation**: Do not proceed to Naver Cloud transfer. Return to development for fixes.

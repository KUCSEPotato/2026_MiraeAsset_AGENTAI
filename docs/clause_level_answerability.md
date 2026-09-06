# Clause-level answerability

작업 브랜치: `feature/clause-level-answerability`.
기준 커밋: `f2218d87175502e5b488a06b001ef72bc6b091d6`
(`fix/production-integration-stabilization`).
금융 연산 capability, 데이터, alias를 추가하지 않았다.

## Answerability와 No-Omission

[ValidationResult](../app/domain/models.py)는 기존 `answerable`을 유지하면서
`FULLY_ANSWERABLE`, `PARTIALLY_ANSWERABLE`, `UNANSWERABLE`을 표현한다.
앞의 두 상태에서 bool은 true이며, 명시한 상태와 bool이 모순되면 schema가 거부한다.
각 output의 entity × field별 상태는 `SATISFIED`, `MISSING`, `UNSUPPORTED`,
`AMBIGUOUS`다. Entity, selector, comparison의 미처리 사유도 같은 clause 목록에 남긴다.
서비스 trace와 evidence serialization에 상태, clause 목록, `comparison_completed`를 전달한다.
계획 단계에서 실행을 거부한 경우도 요청 output을 `query_not_executable`로 기록한다.

No-Omission은 요청을 모두 성공시켜야 답할 수 있다는 의미가 아니다.
검증된 output은 답하고, 확인할 수 없는 output은 이유와 함께 명시한다.
계획에 기록한 disclosure는 원래 grounded query에서 다시 계산해 대조하므로 삭제하거나
다른 내용으로 바꾼 plan은 거부한다. 파싱 자체가 실패해 clause를 구성할 수 없는 입력은
기존 semantic safety 오류를 유지한다.

## Hard constraint와 output requirement

[prepare_outputs](../app/planning/output_requirements.py)는 원래 GroundedQuery를
변경하지 않고 실행 가능한 output과 disclosure를 도출한다. Filter, Boolean tree,
sort/TopK, universe, relation, 관련 entity ID는 기존 검증 경계를 통과해야 한다.
deferred output ID가 hard constraint ID와 겹쳐도 거부한다.

- AUM/6M return/risk projection에서 risk만 없으면 두 사실과 risk 부재를 함께 출력한다.
- 3M return Top5 + AUM에서 AUM만 없으면 검증된 순위와 AUM 부재를 출력한다.
- 순위 metric이나 모집단 조건이 검증되지 않으면 다른 output 값이 있어도 전체를 거부한다.
- 운용보수 0.5% 이하 filter를 없앤 전체 ETF 조회는 실행하지 않는다.
- 필드의 상충하는 evidence는 해당 cell을 `AMBIGUOUS`로 만들며 그 값을 출력하지 않는다.
  실행 실패, 필수 조건/identity/snapshot 검증 실패는 전체 answerability를 차단한다.

## Comparison과 peer selector

필터·관계·순위·집계가 없는 명시적 상품 비교 목록에서 일부 상품만 resolve되면,
resolve된 상품의 검증된 필드만 조회한다. 미해결 상품과 비교 미완료를 명시한다.
resolve된 anchor가 하나도 없으면 unrestricted 조회로 전환하지 않는다.

`다른 … ETF`는 원문과 source span을 가진 `PEER_SELECTOR`로 보존하고 상품 이름으로
resolver에 보내지 않는다. 현재 peer 구성 근거가 없으므로 `peer_selector_unverified`로
표시한다. 지수/노출 조건이나 peer 목록을 추정하지 않으며 selector를 anchor의 filter로
바꾸지 않는다. Peer가 모집단 조건인 질의는 실행을 거부한다.

비교 완료에는 둘 이상의 확인된 상품, 모든 요청 cell, 기존 source-scoped comparison
contract와 evidence/provenance 검증이 필요하다. Risk-grade 비교 같은 미지원 비교는
실행하지 않으며, 허용된 raw projection만 제공하고 비교 미완료를 명시할 수 있다.

PARTIAL은 [deterministic renderer](../app/evidence/answer.py)가 검증된 cell의
evidence index만 사용해 생성한다. HCX로 재서술하지 않는다. 따라서 누락값 보완, 임의 peer,
우열 추론을 하지 않는다. Risk-grade 값은 기존 value-only 정책을 유지한다.

## Organization normalization 감사

[기존 normalization](../app/entity/normalization.py)의 마지막
`(자산운용|운용사|증권)$` 제거가 서로 다른 조직을 동일한 root 이름으로 만들 수 있었다.
이 제거를 없애고 증권/자산운용/은행/보험을 identity 구분 요소로 유지한다.
법인 표기 `(주)` 등의 정리와 `증권사 → 증권`, `자산운용사 → 자산운용` 정리는 유지한다.

Static 및 canonical V2 lookup은 suffix가 명시된 입력에 대해 canonical official name의
suffix가 일치하는지 검사한다. 과거 alias에 다른 조직의 qualified 이름이 있어도 exact
match로 채택하지 않는다. Fuzzy 비교도 suffix가 다르면 거부한다. Canonical 이름에
suffix 근거가 없으면 미해결이 될 수 있으며 root 이름으로 잘못 확정하지 않는다.
특정 회사 분기, 신규 alias, canonical 데이터 변경은 없다.

## 유지한 경계

- Risk-grade projection만 허용하며 filter/sort/comparison 및 Graph 우회는 차단한다.
- Expense-ratio scale 미검증, historical AUM 계산 불가 정책을 유지한다.
- 불완전한 비교는 사실 조회와 구분하고 비교 완료·우열을 주장하지 않는다.
- Unknown material clause는 계속 fail closed 한다. Strict HCX candidate schema를 완화하지 않는다.
- 새로운 peer selection, relation, 금융 연산, 수집/rebuild capability는 활성화하지 않는다.

## 검증

신규 [clause regression](../tests/test_clause_answerability.py)은 실제 parser/resolver/
ontology/planner 및 evidence validator를 사용하고 store 실행은 격리 fixture로 대체한다.
기존 evidence/planner 테스트 중 output 하나의 실패로 모든 사실을 금지하던 기대값을
명시적 PARTIAL/disclosure 기대값으로 갱신했다. 기존 risk-grade audit 테스트는 유지했다.

- 최종 focused: **69 passed, 0 skipped, 0 failed, 1 warning**, 1.62초.
  Clause answerability, composition evidence, composition planner 세 파일.
- 전체 tracked Python 42개 파일: **640 passed, 108 skipped, 0 failed, 1 warning**, 66.95초.
- Frontend: **3 passed, 0 skipped, 0 failed** (`node --test tests/frontend/*.test.cjs`).
- `git diff --check` 및 staged diff 검사 통과.

108 skip은 격리 PostgreSQL URL/환경 미설정에 따른 기존 조건이다.
warning은 기존 Starlette httpx TestClient deprecation이다.
전체 tracked regression 명령:

```sh
/private/tmp/structured-evidence-venv/bin/python - <<'PY'
import subprocess, sys
from pathlib import Path
paths = subprocess.check_output(['git', 'ls-files', '-z', 'tests']).decode().split('\0')
tests = sorted(p for p in paths if Path(p).name.startswith('test_') and p.endswith('.py'))
sys.exit(subprocess.call([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-ra', '--tb=short', *tests]))
PY
```

Production PostgreSQL/Neo4j integration 성공을 주장하지 않는다.
Production deploy/rebuild, main merge, workflow gate 변경은 수행하지 않았다.
기존 untracked `Report/` 사용자 작업은 수정하거나 커밋하지 않았다.

# Risk raw projection / clause partial 수정

기준: fetch 후 확인한 `origin/main`의
`0f1eb8d9324f9a2d237fd41b1c2fdd5ba9162573`.
작업 브랜치: `fix/clause-risk-projection-partial`.
로컬 main을 거치지 않고 origin/main에서 생성했으며 시작 시 tracked 변경은 없었다.
저장소 및 상위 경로에서 AGENTS.md는 발견되지 않았다.

## 확정한 원인

`RuleBasedQueryAnalyzer._extract_relations`의 bare relation alias 목록에
`위험등급`이 있었다. 이 때문에 `위험등급을 알려줘`는 다음 두 의미로 중복 파싱됐다.

- requested field: `위험등급 → product.risk_grade` (RESOLVED)
- relation: `위험등급 → hasRiskGrade` (RESOLVED, target 없음)

실제 호출 순서는 parser → resolver → ontology grounding →
`QueryPlanner.create_plan` → `prepare_outputs` → `SemanticCapabilityValidator.validate`다.
`prepare_outputs`는 raw projection을 실행 가능한 field로 유지했고 disclosure도 없었다.
그 뒤 capability validator가 중복 생성된 `hasRiskGrade` 관계에 대해
`risk_grade_ordering_and_comparability_unverified`로 거부했다.
서비스는 이 계획 실패를 모든 output의 `query_not_executable`로 변환했다.

따라서 prepare_outputs보다 먼저 all-or-nothing validator가 실행된 문제가 아니다.
허용된 projection에 금지된 Graph 관계를 추가한 parser의 semantic role 중복이 원인이다.
기존 positive tests는 `위험 정보` 표현을 사용해 이 중복 경로를 타지 않았다.
수정 전 추가한 회귀 6개 중 AUM+6M은 통과했고 위험등급 관련 5개는 실패했다.

## 수정과 처리 흐름

[analyzer](../app/query/analyzer.py)의 bare relation alias에서 `위험등급`을 제거했다.
기존 requested-field 추출은 그대로 사용한다. 특정 상품 분기는 없다.
`위험등급 1등급인 ETF`처럼 값을 가진 target pattern은 그대로 유지한다.
Risk filter, sort/TopK, comparison 및 Graph compiler의 차단 정책은 변경하지 않았다.

수정 후 흐름:

1. Parser가 AUM/6M/risk를 독립적인 requested field로 보존한다.
2. 기존 resolver가 anchor를 확인하고 ontology가 필드를 grounding한다.
3. prepare_outputs는 원본 GroundedQuery를 변경하지 않고 실행용 query를 파생한다.
   Risk raw projection은 기존 capability가 있으므로 데이터 조회 전에 unsupported로
   분류하거나 deferred 처리하지 않는다.
4. 기존 capability/plan validator를 통과한 RDB plan은 anchor ID와 세 field를 유지한다.
   근거 없이 output/disclosure를 지운 plan은 원본에서 재계산한 결과와 달라 거부된다.
5. 격리 executor가 AUM과 6M evidence만 반환하면 evidence validator가 risk cell을
   `MISSING`으로 판정하고 전체는 PARTIAL로 처리한다.
6. PARTIAL renderer는 검증된 cell의 evidence index만 사용한다. 설정된 answer generator를
   호출하지 않고 누락 risk와 비교 미완료를 명시하는 기존 결정론적 경로를 사용한다.

Raw 값이 있으면 risk 단독 조회는 FULL로 값을 제시한다. 값이 없으면 MISSING/UNANSWERABLE이다.
Anchor ID를 제거한 unrestricted 조회, Graph 보완, 값 추정은 하지 않는다.
원본 query 불변성, plan 변조 거부 및 이 구분을 회귀에서 확인했다.

## 필수 질의 결과

기존 canonical TIGER fixture와 실제 parser/resolver/ontology/planner/evidence validator를
사용했다. Store 실행만 격리했다. 아래 수치는 production 조회 결과가 아니라 제공된 실험
값을 재현하는 fixture 값이다: AUM `20158825743000`, 6M return `6.46`.

| Output 표현 | 상태 | AUM / 6M / risk |
|---|---|---|
| AUM과 최근 6개월 수익률 | FULLY_ANSWERABLE | SATISFIED / SATISFIED / 요청 없음 |
| 위험등급 (값 없음) | UNANSWERABLE | 요청 없음 / 요청 없음 / MISSING |
| AUM과 최근 6개월 수익률과 위험등급 | PARTIALLY_ANSWERABLE | SATISFIED / SATISFIED / MISSING |
| AUM, 최근 6개월 수익률, 위험등급 | PARTIALLY_ANSWERABLE | SATISFIED / SATISFIED / MISSING |

모두 parser LLM 호출 없이 anchor로 제한된 RDB 조회를 계획한다.
PARTIAL은 `answerable=true`, `comparison_completed=false`, `answer_generation_calls=0`이다.
Risk MISSING에는 evidence index가 없으며 두 SATISFIED cell의 index만 값을 가리킨다.
Risk 값이 있는 별도 positive는 `RiskGrade.2`만 제시하며 등급 체계·상대 위험을 해석하지 않는다.

## 경계 및 회귀

- Peer selector는 PARTIAL, `peer_selector_unverified`, 비교 미완료, answer generation 0회를 유지한다.
- Expense-ratio scale 미검증 hard filter와 risk selection은 store 실행 없이 거부한다.
- Unknown material clause가 붙은 risk 조회도 근거 값을 반환하지 않는다.
- Risk 비교 계약 및 v1/v2 Graph 우회 거부 테스트를 유지한다.
- Risk raw projection은 기존 표현과 새 재현 표현 모두 v1/v2 PostgreSQL SQL compilation을 확인한다.
- Missing/ambiguous entity, 무 anchor, 3M Top5+AUM, strict HCX, evidence 검증 경계를 기존 회귀에 포함한다.
- 실제 PARTIAL pipeline 결과가 API를 통과한 뒤 기존 5개 string field 계약을 유지하는지 확인한다.

Focused 명령:

```sh
/private/tmp/structured-evidence-venv/bin/python -m pytest -o addopts= -q -ra --tb=short \
  tests/test_clause_answerability.py tests/test_risk_grade_contract_audit.py \
  tests/test_production_integration_stabilization.py tests/test_semantic_composition_evidence.py \
  tests/test_semantic_composition_planner.py tests/test_m10_5_semantic_safety.py \
  tests/test_m10_9_hyperclova_answer.py tests/test_m10_9_operations.py
```

- Focused: **230 passed, 3 skipped, 0 failed, 1 warning**, 3.68초.
- 전체 tracked Python 42개 파일: **652 passed, 108 skipped, 0 failed, 1 warning**, 65.85초.
- Frontend: `node --test tests/frontend/*.test.cjs` — **3 passed, 0 skipped, 0 failed**.
- `git diff --check` 통과.

기존 640개 통과에서 신규 회귀 12개가 추가되어 총 652개가 통과했다.
108 skip은 기존 PostgreSQL URL/격리 DB 환경 미설정 조건이며 신규 skip/failure/error는 없다.
Focused의 3 skip도 같은 기존 환경 조건이다. Warning은 기존 Starlette/httpx TestClient
deprecation으로 분리한다. 전체 suite에는 실제 production PostgreSQL/Neo4j 성공이 포함되지 않는다.

전체 tracked regression은 요청한 interpreter와 다음 명령으로 실행했다.

```sh
/private/tmp/structured-evidence-venv/bin/python - <<'PY'
import subprocess
import sys
from pathlib import Path
paths = subprocess.check_output(['git', 'ls-files', '-z', 'tests']).decode().split('\0')
tests = sorted(path for path in paths if Path(path).name.startswith('test_') and path.endswith('.py'))
sys.exit(subprocess.call([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-ra', '--tb=short', *tests]))
PY
```

## 범위와 production 후속 검증

Application 변경은 analyzer뿐이다. 나머지는 기존 두 테스트 파일 확장과 이 문서다.
Ontology/transformer/canonical schema·mapping, 데이터·alias, PostgreSQL,
Graph/Semantic mapping/index/artifact, frontend, deploy/workflow/release 파일은 변경하지 않았다.
Report 내용 열람·수정·삭제·stash·커밋, commit/push/PR, 서버 접속, packaging, 배포/rebuild는 수행하지 않았다.

Production DB/Graph integration은 검증하지 않았다. 로컬 store fixture는 production의
READY snapshot, 실제 risk 값 존재 여부, canonical resolver, source evidence와 artifact
호환성을 보증하지 않는다. 새 main SHA image의 별도 one-off에서 위 네 조회와 다음 경계를 재확인한다.

- `국내 ETF 중 최근 3개월 수익률 상위 5개를 찾고 각 상품의 AUM도 알려줘`
- `TIGER 미국S&P500과 다른 미국 S&P500 ETF의 AUM과 수익률 비교`
- `미래에셋 ETF 중 운용보수가 0.5% 이하인 상품`
- `위험등급 1등급인 ETF를 알려줘`
- `국내 ETF 중 위험등급이 낮은 상위 3개를 알려줘`

Risk 데이터가 실제 존재하면 해당 cell은 SATISFIED일 수 있다. 데이터가 없을 때
MISSING/PARTIAL을 확인하되, production 값을 변경해서 이 상태를 만들지는 않는다.

로컬 회귀와 diff 검사를 통과해 변경 파일을 검토 후 커밋할 수 있는 상태다.
요청에 따라 stage/commit/push는 하지 않았으며 HEAD는 기준 SHA 그대로다.

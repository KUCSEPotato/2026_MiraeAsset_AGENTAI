# Production one-off 결과에 대한 stabilization

기준 main: `d56aa81ddf25941281d2ba4b93bcbf8aca531cf0`.
작업 브랜치: `fix/production-integration-stabilization`.
새 기능·데이터·alias·실행 capability를 추가하지 않았다.

## 위험등급 답변

기존 evidence validator는 사실과 실행 근거를 검사하지만 생성된 답변의 추가 해석은
검사하지 않았다. [answer.py](../app/evidence/answer.py)에 field 기반 value-only
답변 계약과 결정론적 검사를 추가했다. `product.risk_grade`가 포함된 답변은 검증된
field/value로 만든 결정론적 출력만 허용하고, HCX 문장이 이 계약을 벗어나면 해당 출력으로
대체한다. 특정 상품명이나 금지 문구 목록에 의존하지 않는다. 함께 요청한 다른 필드도 보존한다.

[HCX prompt/payload](../app/evidence/llm_answer.py)에 raw/display 값만 허용하는 계약과
등급 순서·상대 위험·등급 체계·다른 등급과의 비교 추론 금지를 전달한다.
결정론적 renderer도 risk-grade에 붙은 과거 comparison disclosure와 임의 단위를 사용하지 않는다.
등급 원문 자체에 붙은 공식 display label은 원문 값으로 제시할 수 있다.
기존 risk-grade projection 허용, filter/sort/comparison 및 Graph 우회 차단 정책은 유지한다.

## 파서 조합과 capability 경계

- `<운용사> [국내/해외] ETF 중 <기간 수익률 정렬> <TopK>`에서 운용사 entity와
  `managedBy` target을 함께 보존한다. 기존 국내/해외·provider 범위·상장시장 조건은
  회사명으로 파싱하지 않는다. canonical identity 판정은 기존 resolver가 담당한다.
- `<회사>를 보유한 ETF + 6M RETURN + DESC + TopK + 편입 비중`의 requested-field
  constraint에 `projection_scope=path`, `relation=holds`, `property=weight`를 보존한다.
  없는 entity는 parser 이후 `ENTITY_NOT_FOUND`로 끝난다.
- `최근 6개월 동안 AUM이 가장 많이 증가한 ETF`는 AUM + explicit 6M CHANGE + sort로
  표현하며 `historical_series_unavailable`로 거부한다. 현재 snapshot으로 계산하지 않는다.
- `<운용사> ETF 중 운용보수가 0.5% 이하인 상품`은 typed scalar 원문 `0.5%`, LTE,
  운용사 제약, ETF를 보존하고 `expense_ratio_scale_unverified`로 거부한다.
- [coordinator](../app/query/semantic_parser.py)는 residual이 전혀 없고 이유가 명시적으로
  허용된 세 종류(unit 미검증, historical series 부재, weight projection 미지원)뿐인
  rule 결과를 capability validator로 보낸다. 알 수 없는 material clause는 계속 fail closed 한다.

파싱 성공과 실행 허가는 구분한다. 지역이 없는 일반 ETF 모집단의 수익률 비교는 기존
source-scope 계약을 그대로 적용한다. 운용사 이름만으로 DomesticETF를 추론하지 않는다.
명시적으로 국내 ETF라고 한 운용사 조합의 기존 RDB ranking 계획은 회귀 테스트로 확인했다.
편입 비중 projection은 기존 감사의 미지원 계약을 유지하며, entity가 있어도 별도 수치
projection 실행을 활성화하지 않는다. 기존 path evidence의 weight 보존은 변경하지 않았다.

## HCX 진단 로그

기존 `ValidationError` handler는 sanitized `loc/type/msg`와 top-level keys를
LogRecord에 넣었지만 [운영 JSON formatter](../app/operations.py)의 출력 목록에 없어
실제 JSON에서는 사라졌다. formatter가 이 필드를 제한된 중첩 구조로 출력하도록 수정했다.
Pydantic input/context도 진단 수집에서 제외했다.

MockTransport로 invalid HCX 응답을 재현한 후 **실제 JsonLogFormatter 출력**에
`validation_errors[].loc/type/msg`, `parsed_top_level_keys`가 있는지 검사했다.
raw response, Authorization, API key가 출력되지 않는 것도 검증했다.
외부 HCX/production 로그에 접속해 확인한 결과는 아니며 strict candidate schema는 그대로다.

## 기존 Graph 검증용 후보

우선 후보는 **미래에셋증권**, ticker **006800**이다.

- 기존 [KODEX holdings fixture](../tests/external_data/fixtures/kodex_holdings_20260824.json)의
  `pdf.list[1]`에 `secNm=미래에셋증권`, `itmNo=006800`, `ratio=24.49`가 있다.
  원본 product source ID는 `2ETF15`, 기준일은 `20260824`다.
- 제공 `material/1.금융상품/PRBD01N001_20260824_datarows.xlsx`의 `data` sheet
  Excel 360행에 `pd_pbcm=미래에셋증권(주)`, `pd_no=KR6006802G20`이 있다.
  기존 rebuild는 `pd_pbcm`을 issuer organization으로 매핑한다.
- 추가 후보 **삼성전자(005930)**, **SK하이닉스(000660)**는 기존
  [holdings/issuer integration fixture](../tests/test_m10_9_c2_6_postgresql.py)에 있다.
  합성 fixture ID를 production ID로 사용해서는 안 된다.

로컬에 production canonical DB/Graph snapshot이 없으므로 위 후보의 production 존재와
정확한 resolver 결과를 확정하지 않는다. production one-off 환경에서 아래 읽기 전용
질의로 실제 두 관계가 연결된 후보의 canonical ID/name을 먼저 선택할 수 있다.
`$snapshot`은 운영 READY snapshot과 같은 값으로 bind한다.

```cypher
MATCH (p:M108DNode)-[h:HOLDS]->(s:M108DNode)
      -[i:SECURITY_ISSUED_BY]->(o:M108DNode)
WHERE p.dataset_snapshot = $snapshot AND s.dataset_snapshot = $snapshot
  AND o.dataset_snapshot = $snapshot AND h.dataset_snapshot = $snapshot
  AND i.dataset_snapshot = $snapshot AND p.product_type = 'ETF'
  AND o.entity_kind = 'ORGANIZATION'
RETURN o.entity_id AS organization_id, o.display_name AS company,
       s.entity_id AS security_id, s.ticker AS ticker,
       count(DISTINCT p) AS holding_etfs
ORDER BY holding_etfs DESC, organization_id
LIMIT 20
```

각 `company`를 기존 `CanonicalV2EntityLookup.lookup_with_diagnostics(company,
"organization")`에 넘겨 단일 canonical ID가 Graph `organization_id`와 일치하는
후보만 사용한다. 회사 이름 질의는 `holds → securityIssuedBy` 2-hop이고, ticker를
사용하는 security 질의와 구분한다. `query_eligible=false`인 organization도 resolver의
조직 조회 대상이며, 이름만으로 resolve되었다고 가정하지 않는다.
그다음 해당 회사와 **명시된 국내 ETF** 범위로 Graph→RDB 수익률 검증을 진행한다.
후보 제한/완전성 또는 source-scope 검증 실패를 회피하도록 계약을 완화하지 않는다.

Production deploy 및 DB/Graph/Semantic rebuild는 수행하지 않았다.
기존 `Report/` 파일은 수정하거나 커밋하지 않았다.

## 최종 회귀 결과

- Focused: **287 passed, 3 skipped, 0 failed**, 6.74초.
  stabilization 신규 37개, 기존 HCX·structured operations·risk audit·IR·semantic safety를 포함한다.
- 전체 tracked Python 41개 파일: **612 passed, 108 skipped, 0 failed, 1 warning**, 58.91초.
- Frontend: `node --test tests/frontend/*.test.cjs` — **3 passed, 0 skipped, 0 failed**.
- `git diff --check`, staged diff 검사 통과.
- 108 skip은 격리 PostgreSQL URL/환경 미설정이다. production PostgreSQL/Neo4j 검증
  성공으로 계산하지 않는다. warning은 기존 FastAPI/Starlette httpx TestClient deprecation이다.

전체 tracked regression 실행 명령:

```sh
/private/tmp/structured-evidence-venv/bin/python - <<'PY'
import subprocess, sys
from pathlib import Path
files = subprocess.check_output(['git', 'ls-files', '-z', 'tests']).decode().split('\0')
tests = sorted(p for p in files if Path(p).name.startswith('test_') and p.endswith('.py'))
sys.exit(subprocess.call([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-ra', '--tb=short', *tests]))
PY
```

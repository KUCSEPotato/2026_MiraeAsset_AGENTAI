# Risk-grade ordering / comparability 계약 감사

검토일: 2026-09-06. 브랜치: `feature/semantic-query-composition`.
검토 전 HEAD: `c1bedad0fcadee98068101d92dc29cdd5bc5e977`.
fetch 후 `origin/main`과 merge-base는 모두
`fd9660a35adf8b85e539528a7118611f50ad00dd`이다. 새로운 scope 확장은 하지 않았다.

## 결론

저장소에 제공된 원본 스키마와 ontology에는 PREF01 / PRBD / PRFD 위험등급의
평가 방법·기준일·척도가 동일하며 source 간 비교가 허용된다는 공식 근거가 부족하다.
기존 코드는 ontology의 숫자와 위험 명칭에 수동 순서를 부여하고 cross-dataset
comparability를 활성화했다. 별도의 공식 방법론·동등성 계약에 연결되어 있지 않아
보고서의 “검증된 risk-grade ordering” 표현을 철회한다.

사용자 지시에 따라 `product.risk_grade` projection과 단일 상품 위험등급 조회는
유지한다. 모든 risk-grade filter(EQ/NE/IN 포함), ASC/DESC sort, 동일 source 및
source 간 comparison은 비활성화한다. 명칭이 같다는 이유로 동등한 위험을 주장하지 않는다.
이 결론은 제공 자료의 검토 범위이며 외부에 공식 자료가 전혀 없다는 주장은 아니다.

## 근거 위치와 한계

기존 실행 계약은 [metric_capabilities.py](../app/data/metric_capabilities.py)의
`RISK_GRADE_ORDER`, `MetricCapabilityRegistry.comparison_contract`에 있었다.
`RiskGrade.6 → RiskGrade.1` 순서를 `ordered_values`에 수동 선언하고
`TEAM_ONTOLOGY_RISK_GRADE_V1` 척도, `sort_capability=True`,
`cross_dataset_comparability=True`를 부여했다.
실행 순서는 코드에 명시되어 있었지만 근거 문서와 연결된 계약은 아니었다.

[common.ttl](../ontology/common.ttl)의 `fin:RISK_GRADE_1`부터
`fin:RISK_GRADE_6`(444–496행)은 숫자, 등급명, “매우높은위험”부터
“매우낮은위험”까지의 alias를 선언한다. 공식 순서 관계나 source 간 평가방법의
동등성을 선언하지 않는다.
[candidate ontology](../ontology/candidates/new_optical_ontology.ttl)의 547–553행도
세 source에서 관찰한 등급이라는 설명과 동일 alias만 포함한다.

제공 workbook의 `schema` sheet를 직접 읽었다. 아래 행은 Excel의 1-based 행 번호다.
원본은 `material/1.금융상품/`에 있으며
`material/ai-festival2026_금융상품Agent_DtataSet260824/`의 대응 스키마도 같은 설명이다.

| Source workbook | 행 | 필드 | 제공 설명 |
|---|---|---|---|
| PREF01N001_20260824_schema.xlsx | 81 / 82 | `pd_risk_cd` / `pd_risk_nm` | 상품등급코드 / 상품등급명 |
| PRBD01N001_20260824_schema.xlsx | 50 / 51 | `pd_risk_gcd` / `pd_risk_nm` | 상품위험등급 원문 코드 / 상품위험등급명 |
| PRFD01N001_20260824_schema.xlsx | 70 / 71 | `zrin_fd_ivst_risk_gcd` / `zrin_fd_ivst_risk_grd_nm` | 제로인펀드투자위험등급코드 / 제로인펀드투자위험등급명 |

PREF01의 `pd_pen_risk_nm`(78행, 연금거래위험구분)은 별도 필드다.
공통 숫자나 명칭은 source 간 같은 방법론의 증거로 사용하지 않았다.
[source analysis](../ontology/docs/source_analysis.md)는 PRFD 위험등급 코드의
결측 및 코드표가 없는 위험 코드의 `UNMAPPED_CODE` 보존을 기록한다.
[rebuild mapping](../app/data/v2_rebuild.py)의 `_classifications`는 source 명칭/코드를
공통 concept에 매핑할 뿐 비교 허가를 제공하지 않는다. 이 수집·rebuild 코드는 변경하지 않았다.

## 제한 조치

- `RISK_GRADE_ORDER`는 비활성 감사 기록으로 남기되 unit/scale/순서 목록 및 비교 허가를 제거했다.
  registry는 `risk_grade_ordering_and_comparability_unverified`를 반환한다.
- [runtime mapping](../app/ontology/runtime_mapping.py),
  [field storage](../app/data/field_storage.py), [routing metadata](../app/planning/metadata.py)는
  risk-grade를 projection만 허용한다. 등급 alias와 기존 사실 저장은 유지한다.
- [capability validator](../app/planning/capabilities.py)는 risk filter/sort/comparison을
  계획 전에 차단하고 `hasRiskGrade` 후보 선택도 차단한다.
- [v2 RDB](../app/retrieval/rdb_v2.py), [legacy RDB](../app/retrieval/rdb.py)는
  직접 입력한 위험등급 연산을 거부한다. 과거 비교 계약을 JSON으로 주입해도 허용하지 않는다.
- [Graph compiler](../app/graph/compiler.py)는 `HAS_RISK_GRADE` 경로를 거부한다.
  Graph filter로 우회하지 못하며 위험등급 조회는 RDB projection으로 유지한다.

## Positive / negative tests

[감사 테스트](../tests/test_risk_grade_contract_audit.py) 46개가 모두 통과했다.

| 검증 | 사례 수 | 의미 |
|---|---:|---|
| 단일 상품 조회 → SQL compilation | 2 | 기존 parser/resolver/planner와 v1/v2 compiler에서 projection 유지, ranking 없음 |
| 등급 1–6 evidence | 6 | 임의 ordinal unit을 요구하지 않고 값이 있는 위험등급 조회를 허용 |
| filter 거부 | 18 | 9개 연산 × v1/v2, DB I/O 이전 차단 |
| sort 및 과거 허가 위조 거부 | 4 | ASC/DESC × v1/v2, JSON으로 주입한 수동 순서도 차단 |
| comparison 거부 | 10 | ETF/채권/펀드 단일 범위, 혼합 범위, 미지정 범위 × v1/v2 |
| Graph 우회 거부 | 4 | legacy/canonical-v2 × incoming/outgoing |
| 자연어 위험조건 거부 | 2 | 등급 조건과 낮은 위험 비교를 조용히 삭제하지 않음 |

기존 `test_m10_5_semantic_safety.py`의 위험 정보/위험/위험도/리스크 조회,
누락 위험등급의 unavailable 처리, 수익률+위험등급 공동 projection도 통과했다.
기존 위험등급 정렬 및 Graph 필터 성공 기대를 제거하고 거부 테스트로 대체했다.
PostgreSQL 전용 기존 위험등급 filter 테스트의 기대도 거부로 바꿨으나 로컬에서는 skip이다.

## 최종 로컬 회귀 결과

```sh
/private/tmp/structured-evidence-venv/bin/python -m pytest -o addopts='' -q -ra --tb=short
node --test tests/frontend/*.test.cjs
```

- Python: **575 passed, 108 skipped, 0 failed, 1 warning**, 61.13초.
- Frontend: **3 passed, 0 skipped, 0 failed**.
- warning: 기존 FastAPI/Starlette httpx TestClient deprecation.
- 108 skip: 격리 PostgreSQL 환경/URL 미설정. 성공으로 계산하지 않는다.
- Graph 검증은 fixture/compiler 수준이다. production PostgreSQL 및 Neo4j integration
  성공을 주장하지 않으며 production 서버의 별도 one-off environment 검증 대상으로 남긴다.

Production deploy, main merge, DB/Graph/Semantic rebuild, workflow gate 변경은
수행하지 않았다. 기존 untracked `Report/` 사용자 파일도 수정하거나 커밋하지 않았다.

# Policy-assisted parser 수정 및 QA 결과

검증일: 2026-09-06. 작업 브랜치: `fix/policy-assisted-parser`.

사용자가 제공한 제안에 따라 생략된 매개변수를 허용된 정책으로 보완하고, 해석 실패와 실행 근거 부족을 구분했다. 새로운 금융 데이터·비교 계약·Graph capability는 추가하지 않았다. 운영 배포, 이미지 빌드, 마이그레이션은 하지 않았다.

## 원인과 변경

화면의 세 질문은 기존 최신 main QA에서도 모두 상품 조회 전에 해석 후보 검증에서 실패했다. DB 장애나 상품 검색 결과가 없는 상황이 아니었다.

- 규칙은 미국 상장 조건과 수익률 정렬을 추출했지만, `것` 같은 비핵심 표현 때문에 LLM으로 넘어갔다. `추천`은 정렬 기준이 있어도 미지원 추천 의도로 남았고, `기준일과 함께`는 잔여 문구가 됐다.
- `수익률 좋은 ETF`의 일부가 상품명으로 추출되는 문제도 있었다. 일반 수익률 조건과 주관적 선정 표현을 상품명과 구분했다. 상품명을 특별 처리하지 않았다.
- LLM이 반환한 등록된 canonical field와 한국어 원문 표현이 문자 그대로 일치하지 않으면 거부됐다. 이제 등록된 동의어끼리만 원문 표현으로 정규화한다. 알 수 없는 필드·관계는 계속 거부한다.
- 오프셋 오류는 원문에 정확히 한 번 있는 동일 문자열에 한해 보정한다. 문구가 없거나 여러 번 나오는 경우 위치를 추측하지 않는다.
- 후보 JSON/schema 오류 또는 후보 검증 거부 시 최대 1회 복구를 허용한다. 원문, 거부된 후보, 정제된 오류 코드, 기존 schema·정책을 제공한다. HTTP 오류·타임아웃은 재시도하지 않는다.
- 기존 규칙의 상품 범위, 명시적 기간·정렬·개수·필터를 보존한다. 규칙이 식별한 주관적 선정 조건은 LLM이 일반 검색어로 표현해도 필수 미지원 조건으로 유지한다.

실행 경로는 `Rule → 필요 시 LLM semantic candidate → deterministic normalization/defaults → ontology → capability validation → planning/execution → evidence validation`이다. LLM은 SQL, Cypher, 실행 계획, canonical entity ID를 만들지 않는다.

## 허용 기본값

| policy_id | inferred_value | 적용 조건 |
|---|---|---|
| RETURN.default_period | 1Y | 수익률 기간 생략. 기존 MetricCapabilityRegistry 정책 재사용 |
| TOPK.default_k | 5 | 정렬 검색에 개수 생략. 전체 조회·집계·선택자·비교에 임의 적용하지 않음 |
| RETURN.positive_direction | DESC | 수익률이 좋다는 표현에 한함 |
| RETURN.negative_direction | ASC | 수익률이 나쁘다는 표현에 한함 |

애플리케이션이 정책을 적용한다. LLM의 정책 제안도 allowlist 값과 적용 가능성을 다시 검증한다. `ParseProvenance`, `think_trace.default_policies`에 `policy_id`, `inferred_value`, `source=DEFAULT_POLICY`, 관련 constraint와 disclosure를 기록한다. 명시적 기간과 개수가 기본값보다 우선한다. 기본값 설명은 성공·부분 답변·실행 미지원 응답에 포함한다.

일반 ETF 요청을 국내 ETF로 좁히거나 iShares 일부 범위로 바꾸지 않는다. ‘안전하고’라는 필수 조건을 제거해서 수익률 순위만 답하지 않는다. 위험등급 우열, 검증되지 않은 보수 척도, 과거 AUM, 임의 비교 대상, 미래 수익률, 종합 점수도 만들지 않는다.

## 부분 답변

기존 출력 항목별 검증을 유지한다. AUM과 수익률은 확인되지만 위험 정보가 없으면 확인된 두 항목과 위험 정보 부재를 함께 표시한다. 실제 QA에는 위험등급 원문 값이 있어 세 항목 모두 성공했으며, 위험등급의 크기·우열은 해석하지 않았다. 위험 정보가 없는 fixture에서는 부분 답변을 검증했다.

`기준일`은 온톨로지에 존재하지만 현재 V2의 상품 출력 필드로 실행할 수 없다. 이 출력 요청은 미지원으로 남기고 검증된 순자산 순위는 제공한다. 데이터셋 snapshot을 상품별 관측일로 대신 제시하지 않는다. 부분 답변의 상품명은 동일 entity의 충돌 없는 `product.name` 근거로 표시한다.

## 실제 QA: 제안의 6개 질문

동일한 실제 데이터와 HCX 설정으로 질문마다 순차 1회 요청했다. 아래 시간은 HTTP 왕복을 포함하는 단일 관측치이며 부하 시험·p95가 아니다.

| 질문 | 결과 | 시간 | 확인 내용 |
|---|---|---:|---|
| 수익률 좋은 ETF 알려줘 | capability unsupported | 0.079초 | 전체 ETF 범위의 동일 기준 수익률 비교 근거 부족. 국내로 좁히지 않음 |
| 국내 ETF 중 수익률 좋은 상품 알려줘 | success | 13.587초 | 기존 1년 수익률 기준 상위 5개, 기본값 disclosure |
| TIGER 미국S&P500의 AUM과 수익률, 위험정보 알려줘 | success | 6.647초 | 3개 출력 모두 실제 근거 존재. 위험 원문 값만 표시 |
| 국내 ETF 중 3개월 수익률 상위 5개와 AUM 알려줘 | success | 14.216초 | 명시적 3개월·5개 보존, AUM 포함 |
| 안전하고 수익률 좋은 ETF 알려줘 | capability unsupported | 0.026초 | ‘안전’ 선정 조건 유지. 조회 미실행 |
| 미래에셋 ETF 중 운용보수가 낮고 수익률 좋은 상품 알려줘 | capability unsupported | 0.042초 | 보수 비교 척도 미검증. 조회 미실행 |

6개 모두 `parser_failure`가 아니며 semantic parser LLM 호출은 0회였다. 성공 3건은 답변 생성 LLM을 각 1회 호출했다. 새 복구 경로 자체의 성공·실패·호출 상한은 실제 HTTP client를 사용하는 mock transport 회귀 검사로 검증했다. 이 6건의 실제 결과를 LLM 복구 성공률로 해석하면 안 된다.

## 화면의 3개 질문

| 질문 요약 | 수정 전 최신 main QA | 수정 후 QA | 수정 후 시간 |
|---|---|---|---:|
| 미국 상장 ETF 수익률 높은 것 | parser_failure, 19.784초 | capability unsupported: 수익률 범위 근거 부족 | 0.025초 |
| 순자산 큰 순서로 ETF 추천 | parser_failure, 11.248초 | capability unsupported: 통화·출처 비교 기준 미검증 | 0.024초 |
| 국내 ETF 순자산 큰 5개와 기준일 | parser_failure, 14.692초 | partial: 순자산 상위 5개 제공, 기준일 미지원 표시 | 최종 0.496초 |

세 질문 모두 해석 실패가 사라졌다. 첫 두 질문의 정렬 기능이 새로 지원됐다는 뜻은 아니다. 순자산 부분 답변은 최종 QA에서 상품명과 5개 근거 셀까지 재확인했다.

## 원래 4개 질문: 남은 한계

| 질문 요약 | 결과 | 시간 |
|---|---|---:|
| 캠브리콘이편입된중국반도체ETF를알려줘 | parser_failure 유지 | 25.428초 |
| 미국 시장에 상장된 ETF를 찾아줘 | success | 8.468초 |
| S&P 500을 추종하는 국내 ETF 알려줘 | parser_failure 유지 | 27.119초 |
| 가입 가능한 공모펀드의 1년 수익률 상위 클래스 5개 | capability unsupported 유지 | 0.027초 |

캠브리콘·S&P500 질문은 최초 후보와 1회 복구 후보 모두 `unknown_filter_field`, `candidate_value_not_grounded_in_span`, `candidate_omits_rule_material`로 거부됐다. 두 질문의 조회가 성공했다고 보고하지 않는다. 복구 실패 시 LLM 호출 2회가 필요해 이전 단일 호출보다 지연이 늘 수 있다.

규칙 관측상 캠브리콘 질문의 `반도체` 잔여 의미, ‘지수 + 추종하는 + 국내 ETF’ 관계 구문의 누락이 남아 있다. 후속 개선에서는 명시적 관계·주제 후보의 표현을 보완해야 한다. 공모펀드 클래스 비교와 전체 미국 ETF 수익률 비교는 별도의 capability/데이터 계약 검증이 필요하다.

성공한 국내 순위 질문도 총 13~14초 중 13초가량이 답변 생성에 쓰였다. 조회 실행은 약 0.36~0.53초였다. 추가 지연 개선은 구조화된 사실 답변의 결정적 렌더링 적용을 별도 검토할 수 있다.

## 검증

- 최종 focused: `tests/test_policy_assisted_parser.py`, `tests/test_clause_answerability.py` — **75 passed**.
- 최종 full tracked regression: **711 passed, 108 skipped, 1 warning**, 61.16초.
- 108개 생략은 별도 PostgreSQL 통합 테스트 DB가 설정되지 않은 항목이다. 이를 통과로 계산하지 않았다.
- 신규 정책 테스트 37개: 기본값·명시적 값 우선, 범위 보존, 위험/보수 선정 차단, 미지원 출력의 부분 답변, 등록된 별칭, 오프셋, 복구 1회 상한, HTTP/timeout 무재시도, 근거 상품명 충돌, 미지원 안내 등을 검증한다.
- 기존 날짜 조건 테스트는 유일한 원문 오프셋 보정 허용 및 복구 요청 횟수에 맞춰 기대값을 조정했다. 임의 날짜 생성, 미등록 필드, snapshot 오해석은 계속 거부한다.
- `git diff --check` 통과.

```python
import subprocess, sys
paths = subprocess.check_output(['git', 'ls-files', 'tests'], text=True).splitlines()
tests = [p for p in paths if p.rsplit('/', 1)[-1].startswith('test_') and p.endswith('.py')]
sys.exit(subprocess.call([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-ra', '--tb=short', *tests]))
```

인터프리터: `/private/tmp/structured-evidence-venv/bin/python`.
새 테스트 파일은 저장소의 `tests/*` ignore 규칙 때문에 intent-to-add로 검토 대상에 포함했다. 커밋·푸시는 하지 않았다.

## 최종 실행 상태와 근거 파일

- 수동 채팅: http://127.0.0.1:18080/chat — 이 PC의 SSH 터널이 유지되는 동안 사용 가능.
- API: http://127.0.0.1:18080/docs, 준비 상태: http://127.0.0.1:18080/health (`READY`).
- QA API: `mirae-qa-a0abe888-api`. 이름은 최초 QA 때와 같지만 실행 앱은 이번 작업 트리다.
- 기준 Git SHA: `87cf1a5033bc5d33af5e7458b93988ad1a3bb605`. 병합 main `a0abe888…`와 앱 내용이 동일했던 기준점이다. health의 SHA는 이 기준점을 나타낸다.
- 최종 수정 앱 165파일 SHA-256: `97e7211648f5f5ab69a44f06526549dcd3a5f5e49d27cdde33dc4a59921d9391`.
- 최종 QA 앱 경로: `/opt/mirae-agent/qa/policy-update/source-97e7211648f5/app` (읽기 전용 마운트).
- 전체 13질문 검사 앱 SHA-256: `2fb4ce8a8c1b3df7a9f2d8572fd41b22e0c7ef27f5880ea45b1054f145d652db`. 이후 부분 답변 상품명 표시만 수정하고 전체 회귀 및 해당 실제 질문을 재검증했다.
- QA 코드 교체 전후 운영 API/프런트엔드의 컨테이너 ID·시작 시각 동일. QA는 기존 이미지 의존성과 artifact를 재사용하며 DB 읽기 전용 세션 설정을 유지한다.
- 로컬 전체 13질문 원응답·trace: `/private/tmp/mirae-policy-live-results.json`.
- 로컬 최종 준비 상태·부분 답변: `/private/tmp/mirae-policy-final-qa.json`.
- 로컬 최종 회귀 출력: `/private/tmp/mirae-policy-regression-final.txt`.
- 서버 최종 QA 상태: `/opt/mirae-agent/qa/policy-update/state.json`.

운영 `http://223.130.154.53:8000/`에는 이 수정이 적용되지 않았다.

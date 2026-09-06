# 최신 코드 수동 QA 실행 기록

검사 시작: 2026-09-06 13:07:25 KST

> 아래는 최초 main QA 실행 기록이다. 이후 같은 QA 주소에 policy-assisted parser 작업 트리를 반영했다. 현재 버전과 최종 결과는 [policy_assisted_parser_fix.md](policy_assisted_parser_fix.md)를 참고한다.

## 접속과 실행 버전

- 채팅: http://127.0.0.1:18080/chat
- API 문서: http://127.0.0.1:18080/docs
- 준비 상태: http://127.0.0.1:18080/health
- 실제 실행 코드: `a0abe8883e778f25ffd6bceacdf2e6fd16f4c4e8` (main, PR #14 merge).
- 서버 코드: `/opt/mirae-agent/qa/a0abe888/source`.
- API 컨테이너: `mirae-qa-a0abe888-api`.
- 채팅 컨테이너: `mirae-qa-a0abe888-frontend`.
- 서버의 `127.0.0.1:18080`만 게시하고 SSH 터널로 이 PC에 연결했다.
- 운영 API·프런트엔드의 컨테이너 ID 및 시작 시각은 QA 생성 전후 동일하다.

기존 실행 이미지의 의존성을 재사용하고 최신 `app`·`ontology` 등 실행 파일을 읽기 전용으로 마운트했다. 최신 소스와 운영 소스의 `pyproject.toml`, `uv.lock`, `Dockerfile` 내용이 동일함을 확인했다. 따라서 컨테이너의 기본 이미지 태그는 `da39cbed…`이지만 실제 앱 소스는 `a0abe888…`이다. 앱·온톨로지 파일 188개의 내용 해시를 로컬 최신 소스와 컨테이너 내부에서 대조했다.

```text
source_tree_sha256=2363c79500cb7c00905172972a51c661be140be395c55e472d2d9d8898fcdd25
```

## 실제 데이터 연결

운영의 PostgreSQL·Neo4j·semantic artifact와 HCX-007 설정을 연결했다. 데이터 기준일은 2026-08-24, artifact는 `submission-candidate-20260906-da39cbe-v1`이다. 애플리케이션의 production artifact 및 전체 저장소 호환 검사 결과가 READY다. 테스트 DB 세션의 `default_transaction_read_only`와 `transaction_read_only`가 모두 `on`인 것을 확인했다. 마이그레이션·데이터 적재·인덱스 재생성은 실행하지 않았다.

자격증명은 서버 내 QA 전용 `runtime.env`(0600)에만 저장했다. 이 문서와 로컬 응답 기록에는 자격증명을 포함하지 않는다. QA API 메모리 한도는 768 MiB, 연결 풀은 기본 2개·추가 2개다. 각 질문을 원문 그대로 순차 1회 호출했으며 자동 재시도는 없다. 시간은 네트워크를 포함한 이번 1회 관측치이며 부하 성능 통계가 아니다.

## 네 질문 비교

| 번호 | 질문 | 배포 전 서버 | 최신 QA | 최신 QA 시간 |
|---|---|---|---|---:|
| 1 | 캠브리콘이편입된중국반도체ETF를알려줘 | 질의 해석 실패 | 질의 해석 실패 | 13.724초 |
| 2 | 미국 시장에 상장된 ETF를 찾아줘. | 조회 및 답변 성공 | 조회 및 답변 성공 | 10.440초 |
| 3 | S&P 500을 추종하는 국내 ETF 알려줘. | 질의 해석 실패 | 질의 해석 실패 | 14.355초 |
| 4 | 가입 가능한 공모펀드 중 최근 1년 수익률이 높은 클래스 5개를 찾아줘. | 실행 미지원 | 실행 미지원 | 0.024초 |

2번은 미국 상장 조건으로 5,972개가 일치했고 기본 반환 한도에 따라 10개를 답했다. 1·3번은 LLM 해석 후보 검증에서 거부되어 상품 조회에 도달하지 않았다. 4번은 규칙 해석을 통과했으나 `unsupported_comparison:return_1Y_product_scope_not_verified`로 비교 실행이 차단됐다. HTTP 200 자체가 질문에 대한 답변 성공을 뜻하지 않는다.

### 1. 캠브리콘이편입된중국반도체ETF를알려줘

응답 시간: 13.724초. 상태: `parser_failure`. 요청 ID: `6bf4b97e-f28d-4e17-ab6e-73bedcb824f7`.

실제 서버 답변:

> 질의 해석 서비스를 완료하지 못해 안전하게 검색을 진행할 수 없습니다. 잠시 후 다시 시도해 주세요.

서버 후보 검증 거부 코드: `candidate_value_not_grounded_in_span`, `unknown_relation_subject_type`, `unknown_relation_target_type`.

### 2. 미국 시장에 상장된 ETF를 찾아줘.

응답 시간: 10.440초. 상태: `success`. 요청 ID: `befd584b-f738-48cc-8218-bb77e972d440`.

실제 서버 답변:

> 미국 시장에 상장된 ETF 목록은 다음과 같습니다:
>
> 1. Alternative Access First Priority CLO Bond ETF
> 2. Amplius Aggressive Asset Allocation ETF
> 3. Columbia AAA CLO ETF
> 4. PGIM AAA CLO Aggregate Duration ETF
> 5. Pacer Barings CLO Market Flex ETF
> 6. Goldman Sachs Physical Gold ETF
> 7. AdvisorShares Dorsey Wright ADR ETF
> 8. Alpha Architect US Equity 2 ETF
> 9. Leverage Shares 2X Long AAL Daily ETF
> 10. Leverage Shares 2X Long AAOI Daily ETF
>
> 이 ETF들은 모두 `product.listing_country`가 **US**로 확인되어 미국 시장에 상장된 것으로 검증되었습니다.

### 3. S&P 500을 추종하는 국내 ETF 알려줘.

응답 시간: 14.355초. 상태: `parser_failure`. 요청 ID: `a33b48d0-37c2-402b-9c92-13057d61fe84`.

실제 서버 답변:

> 질의 해석 서비스를 완료하지 못해 안전하게 검색을 진행할 수 없습니다. 잠시 후 다시 시도해 주세요.

서버 후보 검증 거부 코드: `unknown_requested_field`, `candidate_value_not_grounded_in_span`, `unknown_filter_field`, `unknown_relation_subject_type`, `unknown_relation_target_type`, `candidate_omits_rule_material`.

### 4. 가입 가능한 공모펀드 중 최근 1년 수익률이 높은 클래스 5개를 찾아줘.

응답 시간: 0.024초. 상태: `unsupported`. 요청 ID: `cef10596-719d-47ca-9f98-56e5edd4b7d7`.

실제 서버 답변:

> 현재 제공된 조건을 모두 정확하게 해석하여 조회하기 어렵습니다. 조건을 조금 더 구체적으로 지정해 주세요.

진단 사유: `unsupported_comparison:return_1Y_product_scope_not_verified`. 사용자에게 표시된 일반적인 조건 구체화 안내와 실제 비교 기능 미지원 사유를 구분해야 한다.

## 순자산 정렬 수정 확인

질문: “국내 ETF 중 순자산이 큰 순서로 5개 보여줘.”

이전 서버에서는 28.34초 후 LLM 해석 후보 거부로 실패했다. 최신 QA에서는 규칙으로 해석해 실제 조회 및 답변에 성공했고 9.60초가 걸렸다. RDB 일치 1,234개 중 순자산 값이 있는 1,176개를 대상으로 상위 5개를 반환했다. 서로 1회 실행 결과이므로 평균 응답 속도 개선 수치로 해석하지 않는다.

실제 서버 답변:

> 국내 ETF 중 순자산이 큰 순서로 상위 5개는 다음과 같습니다:
>
> 1. **삼성 KODEX200 증권상장지수투자신탁[주식]**
>    순자산: 25,834,234,540,500 원
>
> 2. **미래에셋 TIGER 미국S&P500증권상장지수투자신탁(주식)**
>    순자산: 20,158,825,743,000 원
>
> 3. **미래에셋 TIGER 미국나스닥100증권상장지수투자신탁(주식)**
>    순자산: 11,242,858,878,500 원
>
> 4. **미래에셋 TIGER 200 증권상장지수투자신탁(주식)**
>    순자산: 10,557,724,780,500 원
>
> 5. **삼성 KODEX 미국S&P500 증권상장지수투자신탁[주식]**
>    순자산: 9,953,805,655,000 원
>
> ※ 순자산은 원화 기준이며, 가장 최근 데이터(2026년 8월 24일)를 기준으로 합니다.

## SSH 연결 유지와 종료

현재 브라우저 주소는 이 PC의 SSH 터널이 열려 있을 때 사용할 수 있다. 터널이 종료되면 아래 명령을 별도 터미널에서 실행하고 유지한다. 비밀번호는 SSH 프롬프트에 직접 입력한다.

```sh
ssh -o ExitOnForwardFailure=yes -N -L 127.0.0.1:18080:127.0.0.1:18080 root@223.130.154.53
```

테스트를 완전히 끝낼 때만 서버에서 아래 두 QA 컨테이너를 제거한다. 운영 컨테이너와 데이터 볼륨은 대상에 포함하지 않는다. 아래 종료 명령은 이번 작업에서 실행하지 않았다.

```sh
docker rm -f mirae-qa-a0abe888-frontend mirae-qa-a0abe888-api
```

원본 API 응답·trace·검색 근거: `/private/tmp/mirae-latest-qa-a0abe888.json`. 이전 서버 원본 기록: `/private/tmp/mirae-manual-qa-20260906-035338.json`.

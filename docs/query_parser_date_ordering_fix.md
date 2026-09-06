# 만기·순자산 질의 해석 수정과 수동 배포 준비

로컬 기준 HEAD: `fdca812a23787b5db4c54ad7fd860821f29a10e3`.
작업 브랜치: `fix/query-parser-date-ordering` (기존 작업 브랜치 보존).
시작 시 tracked 변경은 없었다. 기존 untracked Report 내용은 열람하거나 변경하지 않았다.
저장소/상위 경로의 AGENTS.md는 앞선 검사에서 발견되지 않았고, 관련 소스 트리에도 없었다.

운영 SHA/image digest는 이번 요청에 제공되지 않아 로컬과 운영의 byte-level 동일성을
확정하지 않는다. 로컬 coordinator의 rule 우선/한 번 fallback, `exc.reasons` 누락,
HCX content의 JSON/schema 검사, 원문 위치·값·rule material 검사는 제공된 운영 코드 설명과 일치한다.
서버에는 접속하지 않았다.

## 확인한 원인과 수정

실제 RuleBasedQueryAnalyzer를 여섯 질문에 실행했다.

| 질문 | 수정 전 규칙 결과 | 수정 후 결과와 검증 범위 |
|---|---|---|
| 채권 3개 보여줘. | COMPLETE, RDB plan | 동일, limit 3 |
| ETF 5개 보여줘. | COMPLETE, RDB plan | 동일, limit 5 |
| 국내 ETF 5개 보여줘. | COMPLETE, RDB plan | 동일, DomesticETF/limit 5 |
| 만기가 2027년인 채권 5개 보여줘. | 만기가/2027/년인 미해석 → fallback 필요 | Rule COMPLETE, maturity BETWEEN 2027-01-01/2027-12-31, limit 5 → execution UNSUPPORTED |
| 채권을 만기일이 빠른 순서로 5개 보여줘. | 만기일이/빠른/순서로 미해석 → fallback 필요 | Rule COMPLETE, maturity ASC/limit 5 → execution UNSUPPORTED |
| 국내 ETF 중 순자산이 큰 순서로 5개 보여줘. | AUM DESC는 인식, 순서로가 미해석 → fallback 필요 | Rule COMPLETE, DomesticETF + product.aum DESC + Top5 → RDB plan |

만기일은 기존 runtime mapping의 `product.maturity` / `maturityOrFirstCallDate`에
`만기`, `만기일` 별칭으로 정의되어 있다. 현재 mapping은 prospective/project 범위이며
filter/sort 계약이 활성화되어 있지 않다. Ontology grounding은 이런 연산 요청을
실행 가능한 resolved field로 승인하지 않는다. 그래서 parser를 통과해도 기존 capability
validator가 `unresolved_structured_field` 등으로 거부한다. 조건을 제거한 채권 검색은 하지 않는다.
이 단계는 API trace에서 `unsupported`/`UNSUPPORTED_CONSTRAINT`이며 parser_failure와 다르다.

수정 파일:

- [analyzer.py](../app/query/analyzer.py): 연도 만기 필터와 만기 ASC/DESC 표현을 인식하고,
  정렬 문구 안에서만 `순서로`/`순으로`/`오름차순으로`/`내림차순으로`를 소비한다.
  `순서로`를 모든 문장에서 무시하는 토큰으로 추가하지 않았다.
- [normalization.py](../app/query/normalization.py): 원문 year와 FILTER constraint/span이
  일치할 때만 EQ `2027년`을 양 끝 날짜를 포함하는 BETWEEN으로 정규화한다.
  원문·source span·constraint ID는 유지하고 새 query/filter/payload를 파생한다.
  Rule과 검증된 LLM candidate에 같은 함수를 적용한다. Snapshot 의미를 만들지 않는다.
- [semantic_parser.py](../app/query/semantic_parser.py): fallback 필요 상태와
  후보 거부 `exc.reasons`를 코드 목록으로 로깅한다. 예외 변환과 fail-closed는 유지한다.
- [llm_parser.py](../app/query/llm_parser.py): HTTP 200 로그에 호출 목적/provider request ID를
  기록한다. Timeout을 `semantic_parse_timeout`으로 구분하고 JSON/schema 오류에 단계 값을
  붙였다. Prompt v2는 만기 연도를 raw year 필터로 제안하도록 명시한다.
  API 수준 structured output 강제, 모델 교체, timeout 증가, strict schema 완화는 없다.
- [operations.py](../app/operations.py): 실제 JSON formatter에 parser 경로·진단 단계를
  포함한다. 후보 거부는 길이/개수를 제한한 code 목록만 출력한다.
- [main.py](../app/main.py), [api/answer.py](../app/api/answer.py): 서버 생성 request ID를
  ContextVar로 요청 수명 동안 유지한다. 응답 `X-Request-ID`와 최종 API 로그의 request_id는
  같고 하위 로그에는 같은 correlation_id가 붙는다. 동시 요청 간 분리와 reset을 검증했다.
  API의 기존 5개 string body field는 그대로다.
- [llm_answer.py](../app/evidence/llm_answer.py): 답변 생성 HTTP 200에도 purpose를 기록해
  semantic_parse 호출과 구분한다. HCX 각 호출의 request_id는 별도 UUID다.
- [기존 stabilization 테스트](../tests/test_production_integration_stabilization.py):
  실제 규칙·온톨로지·계획 경계, 모의 HCX를 통한 실제 client/validator,
  동시 API 요청의 로그와 검색 0건 회귀를 추가했다.
- [진단 CLI](../scripts/diagnose_query_parser.py): DB 없이 같은 경로를 실행한다.

원문 위치 자동 보정과 candidate 보존 검사는 변경하지 않았다. 이번 로컬 재현으로 확인된
것은 rule 누락/잔여 토큰이다. 운영 LLM이 실제 어떤 후보를 생성했는지, 어느 후보가
invalid_source_span 등으로 거부됐는지는 아직 확인하지 못했다. 당시의 ReadTimeout 원인과
질문에 연결되지 않은 HTTP 200의 용도도 확정하지 않는다.

## 진단 구분과 로그

| 상황 | 관찰 위치 |
|---|---|
| Rule 완료 | parser_path=RULE, constraint_count, unparsed_count |
| Fallback 필요 | parser_path=LLM_FALLBACK, rule latency, unparsed_count |
| HCX timeout | failure_stage=timeout, error_class=ReadTimeout 등, semantic_parse_timeout |
| HTTP 실패 | http_status, hcx_error_code, request_purpose, provider request_id |
| JSON/envelope 오류 | failure_stage=response_json, error_class |
| Strict schema 오류 | failure_stage=response_schema, sanitized validation_errors[].loc/type/msg, top-level keys |
| 의미 검증 거부 | failure_stage=candidate_validation, candidate_rejection_reasons |
| 해석 후 실행 미지원 | 최종 answer_status=unsupported, UNSUPPORTED_CONSTRAINT, stores=[] |
| 정상 실행의 검색 0건 | 최종 answer_status=unanswerable, ZERO_MATCH, RDB total_matches=0 |

일반 NO_EVIDENCE와 실제 total_matches=0은 동일하다고 가정하지 않는다.
완전한 count receipt가 있는 격리 retriever와 실제 QueryExecutor/evidence validator를 사용해
ZERO_MATCH를 확인했다. 이 경우 answer generator 호출을 금지하는 테스트도 통과했다.

Correlation ID는 API middleware가 생성한다. API/HCX/parser application 로그를 이 값으로
묶을 수 있고, 최종 API 로그에 selected_stores/candidate_counts/evidence_count가 있다.
동시 요청 테스트는 rule 검색 0건, LLM 후보 거부, 만기 실행 미지원 세 경로를 구분한다.
원문 전체, 모델 응답 전체, Authorization, API key는 새 로그에 기록하지 않는다.

## 로컬 재현과 실제 호출의 한계

사용 interpreter: `/private/tmp/structured-evidence-venv/bin/python`.
프로세스의 CLOVASTUDIO_API_KEY, DATABASE_URL, POSTGRES_TEST_DATABASE_URL은 미설정이었다.
저장소의 .env/.env.local/.env.production 파일도 없었다. 설정 유무만 확인했으며 비밀 값은
출력하지 않았다. DB 연결과 외부 HCX 요청은 실행하지 않았다.

저장소 루트에서:

```sh
/private/tmp/structured-evidence-venv/bin/python scripts/diagnose_query_parser.py
/private/tmp/structured-evidence-venv/bin/python -m pytest -o addopts= -q --tb=short \
  tests/test_production_integration_stabilization.py
```

CLI는 기본 여섯 질문의 규칙→coordinator→resolver→ontology→planner 결과를 출력한다.
`store_execution=false`이며 조회 성공/실제 상품 존재를 주장하지 않는다.
추가 질문은 `--question '…'`로 지정한다. 실행당 최대 여섯 질문이며 자동 재시도는 없다.

실제 HCX 검증에 필요한 설정 이름은 **CLOVASTUDIO_API_KEY**다.
기존 선택 설정은 HYPERCLOVA_BASE_URL, HYPERCLOVA_MODEL(HCX-007),
HYPERCLOVA_TIMEOUT_SECONDS, HYPERCLOVA_MAX_COMPLETION_TOKENS다.
환경에 안전하게 주입한 후 아래 명령을 사용자가 별도로 실행할 수 있다.

```sh
# --force-llm은 진단 CLI에서만 완전한 rule 결과도 LLM에 보내는 명시적 옵션이다.
# 규칙의 조건은 모두 보존하며 candidate validator를 그대로 통과해야 한다.
/private/tmp/structured-evidence-venv/bin/python scripts/diagnose_query_parser.py \
  --live --force-llm --question '만기가 2027년인 채권 5개 보여줘.'
```

키 미설정 상태의 --live는 키 이름만 알리고 전송 없이 종료하는 것을 확인했다.
테스트의 MockTransport HTTP 200/ReadTimeout은 실제 모델 서비스 결과가 아니다.
모의 정상 후보는 raw year → deterministic dates가 되고, 틀린 문자 위치·원문에 없는
날짜 문자열·unknown field·snapshot으로 재분류해 filter를 누락한 후보는 계속 거부된다.

## 검증 결과

- Focused 8개 파일: **272 passed, 3 skipped, 0 failed, 1 warning**, 4.01초.
- Alembic 선행 실행 후 로그 테스트 격리 확인: **60 passed, 9 skipped, 0 failed**, 1.68초.
  기존 Alembic fileConfig가 logger를 비활성화하는 테스트 전역 상태를 fixture에서 복원·격리했다.
- 전체 tracked Python 42개 파일: **674 passed, 108 skipped, 0 failed, 1 warning**, 60.67초.
- Frontend: **3 passed, 0 skipped, 0 failed**.
- `git diff --check` 통과.

전체 tracked 명령은 `git ls-files -z tests`에서 `test_*.py`를 선택하여 동일 interpreter로
`-m pytest -o addopts= -q -ra --tb=short`를 실행한다. 신규 Python 테스트는 기존 tracked 파일에
추가했으므로 stage 없이도 전체 suite에 포함된다. 환경 의존 skip은 integration 성공이 아니다.
108 skip은 기존 DB URL/격리 PostgreSQL 환경 조건이고 warning은 기존 Starlette/httpx
TestClient deprecation이다. 이번 회귀는 22개 추가됐으며 최종 신규 failure/error/skip은 없다.

```sh
/private/tmp/structured-evidence-venv/bin/python - <<'PY'
import subprocess, sys
from pathlib import Path
paths = subprocess.check_output(['git', 'ls-files', '-z', 'tests']).decode().split('\0')
tests = sorted(p for p in paths if Path(p).name.startswith('test_') and p.endswith('.py'))
sys.exit(subprocess.call([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-ra', '--tb=short', *tests]))
PY
node --test tests/frontend/*.test.cjs
git diff --check
```

## 수동 배포 절차 — 이번 작업에서는 실행하지 않음

근거: [scripts/deploy_naver.sh](../scripts/deploy_naver.sh),
[deploy/README.md](../deploy/README.md), [Dockerfile](../Dockerfile),
[docker-compose.prod.yml](../docker-compose.prod.yml).
코드·테스트·로컬 진단 script·문서만 변경했고 데이터/스키마/온톨로지/매핑/인덱스/artifact는
변경하지 않았다. Migration, 수집, DB/Graph/Semantic rebuild는 필요하지 않다.
실행 코드는 API image에 들어가므로 기능상 API 교체 대상이다. 다만 저장소의 표준 deploy
script는 API와 frontend **두 image 모두 같은 40자 code SHA 태그**를 요구하고 둘 다 교체한다.
Frontend 코드가 같아도 기존의 다른 SHA 태그를 그대로 인자로 주면 gate가 거부한다.

1. 사용자가 변경을 검토·승인하고 실제 커밋을 생성한 뒤 clean checkout을 준비한다.
   아직 새 커밋/이미지는 없다. 아래 `<NEW_CODE_SHA>`, `<API_IMAGE>`, `<FRONTEND_IMAGE>`,
   `<SERVER_PLATFORM>`, `<APPROVED_ARTIFACT_RELEASE_ID>`는 반드시 실제 값으로 바꿔야 한다.
2. 대상 서버 아키텍처에 맞춰 root Dockerfile의 API image와 frontend/ image를 같은 새 SHA
   태그로 빌드해 서버가 pull할 registry에 게시한다. 환경파일은 build context에 추가하지 않는다.
   예시(실행하지 않음):

   ```sh
   docker buildx build --platform '<SERVER_PLATFORM>' -t '<API_IMAGE>:<NEW_CODE_SHA>' --push .
   docker buildx build --platform '<SERVER_PLATFORM>' -t '<FRONTEND_IMAGE>:<NEW_CODE_SHA>' --push frontend
   ```

3. 정확히 그 code tree를 `/opt/mirae-agent/releases/code-<NEW_CODE_SHA>/app`에 준비한다.
   자격증명·Report·material을 복사하지 않는다. 최소 실행에 필요한 compose 파일과
   deploy_naver.sh/deployment_diagnostics.py도 동일 SHA에서 가져온다.
   기존 `/opt/mirae-agent/.env`는 보존한다. 현재 `current` 경로와 그 deployment-state/
   deployment.env를 이전 정상 release로 기록해둔다(비밀 값을 공유 로그에 출력하지 않는다).
4. 기존 승인 artifact release를 재사용한다. Script는 release 디렉터리의 release.json 외에
   `/opt/mirae-agent/incoming/<ARTIFACT_RELEASE_ID>.tar` 및 `.tar.sha256`도 요구한다.
   이 파일들이 없으면 보관된 승인 archive를 준비해야 하며, 새 데이터 rebuild로 대신하지 않는다.
5. 운영자가 서버에서 아래 명령을 실행한다. 이 명령은 preflight만 하는 명령이 아니라
   **컨테이너 교체와 current promotion까지 수행**한다.

   ```sh
   bash '/opt/mirae-agent/releases/code-<NEW_CODE_SHA>/app/scripts/deploy_naver.sh' \
     '<NEW_CODE_SHA>' '<API_IMAGE>:<NEW_CODE_SHA>' \
     '<APPROVED_ARTIFACT_RELEASE_ID>' '<FRONTEND_IMAGE>:<NEW_CODE_SHA>'
   ```

   Script는 archive checksum, code/image identity, artifact-only preflight, 실제 연결을 쓰는
   one-off API lifespan, frontend config를 확인한 뒤 교체한다. 이후 image/mount/env와
   readiness/UI/API smoke gate를 통과해야 current를 갱신한다. 이 기존 gate를 우회하지 않는다.
6. `/live`, `/health`, `/frontend-health`와 위 여섯 질문을 URL encode해 확인한다.
   만기 두 질의는 현재 **unsupported**가 정상 경계이며, parser_failure가 아니어야 한다.
   나머지 네 질의는 rule/RDB, 조건·정렬·limit 및 실제 evidence를 확인한다. 0건이면
   ZERO_MATCH로 처리해야 한다. 추가로 기존 risk raw partial, peer 비교 partial,
   운용보수 0.5% hard filter 거부를 확인한다. 응답 X-Request-ID로 correlation 로그를 묶는다.

배포 도중 gate가 실패하면 기존 script의 ERR trap이 이전 deployment-state를 읽어 API와
frontend를 이전 image로 재생성하고 readiness 확인 후 current를 복구한다.
배포 완료 후 업무 질의에서 회귀가 발견되면, 기록한 이전 정상 code/image/artifact/frontend
네 값을 사용해 이전 code tree의 같은 deploy script를 다시 실행할 수 있다. 이전 승인
artifact archive와 code tree가 남아 있어야 하며 실행 전 네 값이 이전 정상 release인지 확인한다.
`rollback`은 script 내부 함수이며 `--rollback` CLI 옵션은 없다.
DB 볼륨 삭제, 재적재, migration/rebuild는 rollback에 포함하지 않는다.

남은 검증은 실제 HCX 후보 출력과 운영 데이터의 snapshot/provenance·순위 결과·배포
artifact 호환성이다. 이번 작업은 로컬 parser/fixture 검증과 수동 배포 준비이며 운영 DB를
조회한 전체 서비스 검증은 아니다. 커밋·push·이미지 빌드·서버 접속·배포는 수행하지 않았다.

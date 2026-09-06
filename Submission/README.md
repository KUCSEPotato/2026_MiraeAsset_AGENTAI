# 2026 미래에셋증권 AI Festival

## Financial Product Agent Submission

### Team / Project

- Team: ORY
- Project: F:INORY — 온톨로지 기반 금융상품 질의응답 에이전트
- Members: 한은규, 이정민, 김태연

팀·프로젝트 정보는 최종 기술 제안서 표지에서 확인했습니다.

### Submission Contents

1. Source Code
   - `source-code-a1d76c9.tar.gz`: 최종 커밋의 정확한 Git source snapshot
   - `SOURCE_CODE_INFO.md`: 구성, 재현 환경, 실행 방법 및 archive 정보
2. Technical Proposal
   - `ORY_MiraeAsset_AgenticAI.pdf`: 최종 기술 제안서 사본
3. Evaluation API
   - `API_SPEC.md`: 평가 API 요청·응답 계약
   - `ENDPOINT_INFO.md`: Google Form 입력용 endpoint 요약
   - `API_SMOKE_TEST.md`: 제출 준비 중 수행한 단일 read-only smoke 결과
4. Final Review
   - `FINAL_CHECKLIST.md`: 제출자가 마지막으로 확인할 체크리스트

### Final Code Version

Commit:

`a1d76c9366d78cc0ea6de33a57a2171e8950de5a`

준비 시점에 로컬 `HEAD`와 `origin/main`이 위 SHA로 일치했습니다. Source archive는
working tree가 아니라 이 커밋에서 직접 생성했습니다.

### Evaluation API

- Endpoint: `http://223.130.154.53:8000/answer`
- Method: `GET`
- Authentication: None

### Technical Proposal

`ORY_MiraeAsset_AgenticAI.pdf`

### Verification Scope

제출 준비 중 production endpoint에 read-only GET 요청을 1회 보내 API 연결과 응답
schema를 확인했습니다. 응답은 배포된 Git SHA를 제공하지 않으므로, endpoint가 위
commit으로 배포되었다는 사실까지 독립적으로 검증한 것은 아닙니다.

기술 제안서와 현재 구현 사이의 치명적인 모순은 발견하지 못했습니다. 제안서의
Rule-first parsing, 제한적 HyperCLOVA X fallback, ontology grounding, deterministic
planning/compiler, RDB·Graph·Semantic retrieval, evidence validation, fail-closed 정책과
비교 계약 제한은 구현 구조와 일치합니다. 지원 가능한 일부 clause만 실행하는 경우에도
제외한 clause와 이유를 trace와 답변에 보존하는 partial-answer 정책을 사용합니다.


# Evaluation API Smoke Test

## Scope

Production을 변경하지 않는 read-only `GET /answer` 요청을 제출 준비 중 1회 수행했습니다.
응답 body 및 전체 `think_trace`는 Submission package에 저장하지 않았습니다.

- Endpoint: `http://223.130.154.53:8000/answer`
- Question ID: `submission-smoke-001`
- Question: `TIGER 미국S&P500 ETF의 NAV를 알려줘`
- Authentication: None

## Result

| Check | Result |
| --- | --- |
| External connection | PASS |
| HTTP status | `200` |
| Content-Type | `application/json` |
| JSON parse | PASS |
| Exact five response fields | PASS |
| All five values are strings | PASS |
| `question_id` echo | PASS |
| `question` echo | PASS |
| Safe trace status | `success` |
| Observed latency | 5,321.5 ms |

이 요청은 API schema와 연결 상태만 확인합니다. 응답에는 deployed Git SHA attestation이
없으므로 production image가 특정 commit과 동일하다는 사실까지 증명하지는 않습니다.


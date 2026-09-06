# Evaluation API Specification

## Endpoint

`GET http://223.130.154.53:8000/answer`

## Authentication

None. 인증 header를 사용하지 않습니다.

## Query Parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `question_id` | string | Yes | 평가 문항 ID |
| `question` | string | Yes | 평가 질의 원문 |

정의되지 않은 추가 query parameter는 FastAPI 기본 동작에 따라 무시됩니다.

## Request Example

```bash
curl --get \
  "http://223.130.154.53:8000/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=TIGER 미국S&P500 ETF의 NAV를 알려줘"
```

## Response

정상 형식의 평가 요청은 `HTTP 200 OK`와 JSON body를 반환합니다.

```json
{
  "question_id": "Q-001",
  "question": "평가 질의 원문",
  "retrieved_context": "...",
  "think_trace": "...",
  "answer": "..."
}
```

응답은 정확히 다음 5개 field를 가지며 모든 값은 JSON string입니다.

- `question_id`
- `question`
- `retrieved_context`
- `think_trace`
- `answer`

`retrieved_context`와 `think_trace`의 내부 내용이 구조화된 JSON인 경우에도 API에서는
직렬화된 string으로 반환됩니다.

## Field Semantics

### `question_id`

요청의 `question_id`를 그대로 반환합니다.

### `question`

요청의 `question`을 그대로 반환합니다.

### `retrieved_context`

검증된 retrieval/evidence context의 serialized string입니다.

### `think_trace`

Parsing, planning, retrieval, validation execution 정보를 담은 serialized string입니다.
숨겨진 model chain-of-thought를 제공하는 field가 아닙니다.

### `answer`

최종 user-facing answer입니다.

## HTTP Behavior

정상 형식의 평가 request에 대해서는 다음 내부 처리 결과도 동일한 5-field HTTP 200
envelope를 유지합니다.

- success
- partial
- unsupported
- entity not found
- parser failure
- insufficient evidence
- safe internal failure

HTTP transport status와 semantic execution status는 분리됩니다. Semantic 상태와 안전한
실패 사유는 serialized `think_trace`에 보존됩니다.

필수 parameter가 누락되거나 값이 비어 있거나 공백뿐인 malformed request는 현재 FastAPI
required/min-length contract에 따라 `HTTP 422`를 반환할 수 있습니다.

## Content-Type

`application/json`


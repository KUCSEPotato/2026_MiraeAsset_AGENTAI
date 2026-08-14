# Financial Semantic Agent

금융 상품 질의응답 Agent를 위한 평가용 Backend API baseline입니다. 현재 milestone은 API 계약과 교체 가능한 Agent 경계만 제공하며, 실제 데이터 검색과 HyperCLOVA X 생성은 아직 연결하지 않습니다. 근거가 없는 상태에서 금융 정보를 생성하지 않도록 모든 질문에 명시적인 답변 불가 결과를 반환합니다.

## Architecture

```text
GET /answer
  -> FastAPI validation
  -> AnswerService (dependency injection)
  -> AgentResult
  -> AnswerResponse validation
```

`DeterministicBaselineService`는 다음 milestone의 retrieval/validation/LLM pipeline으로 교체할 수 있습니다. API route에는 Agent 구현 세부사항이 없습니다.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Environment Variables

설정 예시는 `.env.example`에 있습니다. 현재 baseline 실행에는 API key가 필요하지 않습니다.

```text
CLOVA_API_KEY=
CLOVA_BASE_URL=
CLOVA_MODEL=
DATA_SNAPSHOT_DATE=2026-07-11
APP_TIMEOUT_SECONDS=280
LOG_LEVEL=INFO
```

실제 secret을 담은 `.env`는 Git에 포함하지 않습니다.

## Local Setup

```bash
uv sync --all-groups
```

## Running the Server

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Evaluation API

인증 header 없이 다음 query parameter를 받습니다.

```text
GET /answer?question_id=<string>&question=<string>
```

빈 값이나 공백뿐인 값은 `422`를 반환합니다. 정상 요청은 정확히 다섯 개의 string field를 반환합니다.

## Evaluation API End-point

```text
http://<PUBLIC_IP>/answer
```

## Example Request

```bash
curl -G "http://localhost:8000/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=국내 ETF 중 운용보수가 낮은 상품을 알려줘"
```

## Example Response

```json
{
  "question_id": "Q-001",
  "question": "국내 ETF 중 운용보수가 낮은 상품을 알려줘",
  "retrieved_context": "",
  "think_trace": "{\"steps\": [\"request_validation\", \"baseline_agent\"], \"status\": \"unanswerable\", \"reason\": \"retrieval_not_connected\"}",
  "answer": "현재 평가 API baseline에는 실제 검색 Agent가 연결되어 있지 않아 근거 기반 답변을 생성할 수 없습니다."
}
```

## Health Check

```bash
curl "http://localhost:8000/health"
```

예상 응답은 `{"status":"ok"}`입니다.

## Tests

```bash
uv run pytest
```

실행 중인 서버를 평가 서버 방식으로 검사하려면:

```bash
uv run python scripts/smoke_test.py \
  --question-id Q-001 \
  --question "평가 질의"
```

## Docker

Docker packaging은 production milestone에서 추가합니다.

## Production Deployment

Production 배포에서는 public port 80/443의 reverse proxy 뒤에서 Uvicorn을 실행합니다. SSH는 관리자 IP로 제한하고 RDB, Graph DB, Vector DB port는 public internet에 직접 공개하지 않습니다.

# 2026_MiraeAsset_AGENTAI

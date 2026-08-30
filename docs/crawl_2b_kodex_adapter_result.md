# Crawl-2B KODEX Adapter Result

## Contract Implemented

삼성자산운용 KODEX 공식 상품 페이지가 사용하는 공개 JSON 계약만 구현했다.

```text
GET https://www.samsungfund.com/api/v1/kodex/product-pdf/{fId}.do
    ?gijunYMD=YYYY.MM.DD
```

응답 최상위 `pdf`와 `gijunYMD`, count, Excel URL, list, receive time 및 holdings row의 확인된 필드를 strict schema로 검증한다. 알 수 없는 필드, 누락된 필드, count 불일치는 schema drift로 처리한다. 다른 운용사에는 이 계약이나 의미를 재사용하지 않는다.

## Files

- `app/external_data/holdings/models.py`
- `app/external_data/holdings/contract.py`
- `app/external_data/holdings/normalize.py`
- `app/external_data/holdings/providers/kodex.py`
- `app/external_data/holdings/__init__.py`
- `app/external_data/holdings/providers/__init__.py`
- `app/external_data/manifest.py`
- `tests/external_data/fixtures/kodex_holdings_20260824.json`
- `tests/external_data/test_kodex_holdings.py`

## Raw → Normalized Flow

```text
KODEX public JSON
  → Crawl-1 robots/rate-limit/retry/cache HTTP client
  → immutable SHA-256 raw JSON artifact
  → strict KODEX response validation
  → response pdf.gijunYMD cutoff validation
  → SourceRecord
  → external-holdings-v1 JSONL
  → snapshot manifest checksums/counts
```

fetch 실패는 artifact를 만들지 않고 failure로 기록한다. schema drift와 post-cutoff 응답은 raw artifact와 실패 provenance를 보존하지만 evaluation-ready holdings JSONL을 만들지 않는다.

## Identity Fields

Product input:

- `product_name_raw`
- `product_ticker`
- `product_isin`
- KODEX `fId`를 `product_source_id`로 보존

Constituent response:

- `secNm` → `constituent_name_raw`
- `itmNo` → `constituent_source_id`
- 정확히 6자리 숫자인 국내 종목코드만 `constituent_ticker`에도 보존
- source에 없는 constituent ISIN은 null
- `KRD...`/원화예금은 security로 오인하지 않고 `NON_SECURITY`
- 코드가 없으면 name-only이며 canonical identity를 만들지 않음

## Holdings Fields

- `ratio` → raw weight + normalized proportion
- `applyQ` → raw/normalized creation-unit quantity
- `evalA` → raw/normalized KRW evaluation amount
- source가 명시적 rank를 주지 않으므로 `rank=null`
- `curp`, `risep`, `basrpRt`, `pdfType`은 strict parse하지만 의미가 holdings-v1에 승인되지 않아 emit하지 않음

## Numeric Semantics

KODEX 공식 설명에 따라 `ratio`는 국내외 현금성 자산과 예금을 제외해 계산한 비중의 percent points다. 예를 들어 raw `24.49`는 normalized proportion `0.2449`, unit `PERCENT_OF_NON_CASH_ASSETS`, scale `PERCENT_POINTS`로 저장한다.

`applyQ`는 PDF creation-unit 구성수량, `evalA`는 원 단위 평가금액으로 처리한다. Decimal로 파싱하며 음수, 비유한 값, 100을 초과한 percent points는 거부한다. 공란은 0으로 바꾸지 않는다.

## Temporal Semantics

portfolio effective date는 응답 `pdf.gijunYMD`만 사용한다. `rcvTime`과 HTTP `retrieved_at`은 effective date가 아니다. `published_at`은 source가 제공하지 않아 null이다.

2026-08-30 live probe에서 `fId=2ETF15`, 요청일 2026-08-24에 대해 다음을 확인했다.

```text
effective_date: 2026-08-24
holdings rows: 15
raw artifacts: 1
normalized rows: 15
```

## Cutoff Enforcement

고정 평가 cutoff는 `2026-08-24`다.

- 요청일이 cutoff 이후면 fetch 전에 거부
- 응답 `gijunYMD`가 cutoff 이후면 raw와 failed SourceRecord를 남기고 normalized holdings 제외
- `retrieved_at`으로 날짜를 보완하거나 과거로 변경하지 않음
- KODEX workspace manifest에 `data_cutoff_date=2026-08-24` 기록

## Provenance

각 holding row는 다음을 가진다.

- deterministic `holding_record_id`
- `source_record_id`
- provider와 exact request URL
- retrieval/effective timestamp
- snapshot ID와 authoritative trust tier

holding ID는 provider, product source ID, constituent source key, effective date, SourceRecord ID의 SHA-256으로 결정한다. 동일 raw artifact의 재실행은 같은 SourceRecord/Holding ID와 같은 normalized bytes를 생성한다.

## Tests

전용 offline test:

```text
.venv/bin/python -m pytest -p no:capture -p no:debugging \
  tests/external_data/test_kodex_holdings.py \
  tests/external_data/test_foundation.py -q
```

결과: `14 passed`.

검증 항목:

- 최소화한 실제 KODEX schema와 복수 구성종목
- deterministic ID와 동일 snapshot rerun byte idempotency
- percent-point → proportion 변환
- identifier 결측/name-only quality
- 2026-08-24 effective date 승인
- post-cutoff response 제외
- schema drift fail-visible
- raw artifact → SourceRecord → holdings provenance
- constituent source code → KODEX product source ID reverse-index capability probe
- manifest source/holdings checksum 및 row count

환경상 수집 불가능한 기존 통합 테스트 5개를 제외한 project suite:

```text
.venv/bin/python -m pytest -p no:capture -p no:debugging \
  --ignore=tests/test_m10_5_semantic_safety.py \
  --ignore=tests/test_m10_8_a_postgresql_schema.py \
  --ignore=tests/test_m10_8_b_rebuild.py \
  --ignore=tests/test_m7_real_rdb_integration.py \
  --ignore=tests/test_m8_ontology_integration.py
```

결과: `84 passed, 33 skipped, 1 warning`.

## Known Limitations

- KODEX product catalog에서 내부 ETF universe로의 `fId` mapping은 아직 구현하지 않음
- KODEX 이외 운용사에는 적용하지 않음
- 6자리 constituent code와 canonical security/company의 resolution은 하지 않음
- 원화예금 외 현금·채권·파생 source code 분류는 provider evidence가 더 필요함
- KODEX weight는 현금 제외 기준이므로 다른 provider의 net-asset weight와 직접 비교 불가
- KODEX 내부 공개 API는 UI contract이며 별도 SLA가 없음
- CLI orchestration과 canonical ingestion은 이번 범위 밖

## Ready for Canonical Integration

No.

source-level KODEX output은 준비됐지만 canonical product/security resolution, evidence 승인, portfolio semantics mapping은 main Agent integration milestone에서 별도로 수행해야 한다. 이 adapter는 canonical DB, ontology, Neo4j, QueryPlan에 쓰지 않는다.

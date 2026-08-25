# Financial Semantic Agent

금융 상품 질의응답 Agent를 위한 평가용 Backend API입니다. Milestone 10에서는
repository-local Ontology v0로 grounded된 structured query를 canonical RDB에서
실행하고, 실제 해외 ETF 전략 원문을 BM25/vector index로 검색하며, 관계 질의를
Neo4j knowledge graph에서 실행합니다.
`DATABASE_URL`이 설정된 runtime의 RDB, entity lookup, field-quality metadata와
BM25/Vector retriever는 실제 backend를 사용합니다. `NEO4J_URI`와 credential이 함께
설정되면 Graph도 real retriever를 사용합니다.

## Architecture

```text
GET /answer
  -> FastAPI validation
  -> AnswerService (dependency injection)
  -> QueryAnalyzer
  -> EntityResolver
  -> OntologyService
  -> QueryPlanner
     -> FastRoutingChecker
        -> DeterministicRuleRouter
        -> DeterministicSupervisorPlanner
     -> StructuredQueryPlanValidator
  -> QueryExecutor
     -> RetrieverRegistry
        -> RDB / Graph / Vector / BM25 Retriever
     -> InternalTransformExecutor
  -> ExecutionResult
  -> EvidenceBuilder
  -> QualityAwareEvidenceValidator
     -> answerable: AnswerGenerator
     -> unanswerable: ReasonAwareSafeResponseGenerator
  -> AgentResult
  -> AnswerResponse validation
```

`PipelineAnswerService`는 모든 component를 constructor injection으로 받습니다. 기본 provider는 M3 semantic component와 M2 downstream fake를 조립하고, 격리 테스트 provider는 전체 fake pipeline을 조립합니다. 이후 각 component를 실제 구현으로 교체해도 orchestrator와 API route는 수정할 필요가 없습니다.

## Semantic Front-End

```text
Question
  -> RuleBasedQueryAnalyzer
  -> RegistryEntityResolver
  -> RDFOntologyService
  -> GroundedQuery
```

- Query Understanding은 intent, raw product type, filter, sort, requested field와 semantic-search 필요 여부를 구조화합니다.
- Entity Resolution은 RDB가 설정되면 `RDBEntityLookup`, 격리 test에서는
  `StaticEntityLookup`을 사용하며 모르는 entity ID를 만들지 않습니다.
- Semantic Grounding은 raw 표현을 보존하면서 `Region.US`, `AssetType.Bond`, `product.aum` 같은 canonical representation을 별도로 추가합니다.
- M2 fake semantic components는 격리 테스트와 failure simulation을 위해 유지합니다.

Production semantic grounding은 startup에서 repository의 mandatory TTL 5개를
검증하고 한 번 index합니다. `StaticSemanticRegistry`와
`RegistryOntologyService`는 격리/failure test용 baseline으로 유지합니다. 전체 금융
entity dictionary와 LLM query parser는 아직 구현하지 않았습니다.

Ontology의 목적, 파일별 vocabulary, competency questions, canonical mapping과
validation 방법은 [ontology/README.md](ontology/README.md)에 정리되어 있습니다.

## Planning

```text
GroundedQuery
  -> Fast Routing Check
     -> simple: Rule Router
     -> complex: Supervisor Planner
  -> Structured QueryPlan
  -> Plan Validation
  -> Executor
```

Rule Router는 canonical filter와 sort를 RDB-oriented plan으로 변환하지만 SQL을 생성하지 않습니다. Semantic free text, relation traversal, unresolved/ambiguous input은 Supervisor path로 보냅니다. 두 경로 모두 동일한 `QueryPlan` schema를 사용합니다.

M4 Supervisor Planner는 deterministic one-shot planner입니다. LLM supervisor, ReAct loop, tool execution은 연결되어 있지 않습니다. Plan validator는 empty plan, duplicate step ID, 잘못된 source/operation, dependency 오류와 cycle, 입력에 없는 canonical field·concept·entity ID를 Executor 전에 차단합니다.

## Execution Layer

```text
Validated QueryPlan
  -> DAG QueryExecutor
     -> ready independent steps in parallel
     -> RetrieverRegistry
        -> RealRDBRetriever / FakeRDBRetriever
        -> RealGraphRetriever / FakeGraphRetriever
        -> RealVectorRetriever / FakeVectorRetriever
        -> RealBM25Retriever / FakeBM25Retriever
     -> InternalTransformExecutor
  -> RetrievalRecord
  -> Evidence
```

`depends_on`은 required dependency로 처리합니다. Expected retrieval failure와 timeout은 step status로 기록되고, 해당 결과에 의존하는 downstream step은 skipped 처리됩니다. 독립적인 성공 결과는 보존되며 programmer error와 request cancellation은 숨기지 않습니다.

`filter_candidates`와 `rank_candidates`는 `source=internal`인 Executor 내부
transform입니다. canonical `entity_id` 교집합과 stable ordering만 제공하며 실제
금융 ranking을 수행하지 않습니다. 결합 결과에는 dependency별 source ID, retrieval
score type과 semantic matched text가 `fusion_provenance`로 보존됩니다.

`DATABASE_URL`이 설정되면 RDB, Vector, BM25 source는 real retriever를 사용하고,
Neo4j 설정이 완전하면 Graph source도 real retriever를 사용합니다.
Fake retriever는 execution isolation test와 DB 미설정 개발 환경을 위해 유지합니다.
생성되지 않았거나 snapshot/model/version이 맞지 않는 semantic index는 빈 결과로
숨기지 않고 기존 Executor의 retrieval failure isolation으로 전달합니다.

## Semantic Retrieval

```text
canonical_products + etf_attributes.strategy
  -> ForeignETFStrategyDocumentBuilder
  -> SemanticDocument (raw + normalized text, canonical entity, provenance)
  -> persisted search artifact
     ├── BM25 Okapi lexical search
     └── cosine vector search
  -> RealBM25Retriever / RealVectorRetriever
  -> existing Evidence pipeline
```

### Document Construction

M9 corpus는 `source_foreign_etfs.payload["cu_strtegy"]`에서 적재된
`etf_attributes.strategy`를 사용합니다. 검색 field는 dataset column과 분리된
`product.strategy_description`이며 문서 ID는
`<canonical_product_id>:strategy`입니다. 원문을 요약하거나 다시 작성하지 않고
NFKC, case folding, whitespace normalization 결과를 별도로 저장합니다.

### BM25 and Vector Embeddings

BM25는 현재 약 5.6K 문서에 적합한 portable BM25 Okapi 구현입니다. 전문용어와
영문 phrase를 lexical token으로 검색하며 score는 raw BM25 provenance일 뿐 금융
속성이 아닙니다.

Vector baseline은 `EmbeddingProvider` 뒤의
`multilingual-semantic-hash-v1`을 사용합니다. 외부 credential과 model download 없이
재현 가능한 한국어/영어 금융전략 concept feature와 hashed lexical feature를
384차원으로 encoding합니다. 실제 persisted vector/cosine search를 수행하지만
neural foundation embedding은 아니며, 향후 multilingual neural provider로 교체할
수 있는 초기 offline baseline입니다. Vector similarity 역시 금융 사실이 아니라
ranking metadata로만 저장합니다.

검색 artifact backend는 별도 SQLite file의 metadata-indexed documents와 packed float
vectors이며, 현재 규모에서는 deterministic exact cosine scan을 사용합니다. 이는
canonical production database가 PostgreSQL이라는 원칙을 바꾸지 않습니다. pgvector는
extension 운영 dependency를 피하기 위해 M9 default로 선택하지 않았습니다.

### Index Build

API request에서 corpus embedding을 만들지 않습니다. canonical DB ingestion 후 다음
offline command를 실행합니다.

```bash
DATABASE_URL=postgresql+psycopg://... \
DATA_SNAPSHOT_DATE=2026-07-11 \
uv run python -m app.search.index
```

현재 지원 source는 `--source etf_gl`입니다. build는 임시 artifact를 완성한 뒤
atomic replace하며 stable document ID로 idempotent합니다. 기존 index의 snapshot,
version, model, dimension과 text checksum이 같으면 embedding을 재사용합니다. 결과는
source/usable/skipped/duplicate count와 embedding metadata를 출력합니다.

작은 actual-corpus retrieval evaluation은 다음 command로 재현합니다.

```bash
uv run python -m app.search.evaluate
```

### Index Version / Snapshot

검색 artifact는 `embedding_model`, `embedding_dimension`, `index_version`,
`dataset_snapshot`, `indexed_at`, document statistics를 저장합니다. Runtime은 이를
현재 canonical DB 설정과 비교하고 mismatch 시 fail-fast retrieval result를 냅니다.
BM25와 Vector는 동일 문서와 동일 version/snapshot 경계를 공유합니다.

### Metadata Filtering and Candidate Restriction

`source_dataset`, `source_field`, `product_type`, `region`, `asset_type`,
`dataset_snapshot`은 scoring 전에 index candidate query에 적용됩니다. Region/asset
등 강한 structured 조건이 있으면 planner는 다음 dependency를 만듭니다.

```text
RDB structured candidates (bounded by SEMANTIC_CANDIDATE_LIMIT)
  -> Vector/BM25 search(candidate_ids_from=RDB)
  -> existing INTERNAL entity_id intersection
```

product type만 있는 narrative query는 semantic index metadata로 직접 제한해 RDB
round trip을 생략합니다. Requested field/sort 때문에 RDB가 필요하지만 실제 후보를
줄이는 filter가 없으면 RDB와 Vector를 parallel branch로 실행한 뒤 INTERNAL
transform에서 결합합니다. 따라서 dependency가 꼭 필요한 질의만 sequential합니다.

### Known Retrieval Limitations

- 현재 corpus는 해외 ETF strategy field 하나이며 product name, benchmark, 국내 ETF,
  공모펀드 text index는 아직 없습니다.
- unresolved product-name BM25 step은 field-aware하게 분리되어 있어 strategy text를
  이름 index처럼 오용하지 않으며, name corpus가 추가되기 전에는 정상 empty입니다.
- learned hybrid fusion, cross-encoder reranking, calibrated similarity threshold,
  pgvector/ANN backend는 아직 없습니다.
- multilingual feature lexicon 밖의 paraphrase 품질은 neural embedding보다 제한적입니다.

## Minimal Knowledge Graph

```text
PostgreSQL canonical snapshot
  -> CanonicalGraphExtractor
  -> GraphMappingRegistry (Ontology v0 domain/range validation)
  -> offline Neo4j ingestion
  -> ready graph metadata (version + snapshot + built_at + counts)
  -> allow-listed GraphQueryCompiler
  -> RealGraphRetriever
  -> existing RetrievalRecord / Evidence pipeline
```

PostgreSQL은 numeric/structured product fact의 authoritative store입니다. Neo4j는
동일 canonical product ID를 재사용하는 relation-centric projection이며 숫자 값을
복제해 새로운 source of truth로 만들지 않습니다. TTL ontology는 class/property와
domain/range를 정의하는 schema vocabulary이고 product instance store가 아닙니다.

### Nodes and Relations

- Product: `ETF`, `ETN`, `Bond`, `Fund`, `FundClass`
- Supporting entity: `AssetManager`, `Issuer`, `Index`, `Benchmark`, `Region`,
  `AssetType`, `RiskGrade`, `Currency`
- Edge: `MANAGED_BY`, `ISSUED_BY`, `TRACKS`, `REFERENCES_BENCHMARK`,
  `HAS_CLASS`, `INVESTS_IN_REGION`, `HAS_ASSET_TYPE`, `HAS_RISK_GRADE`,
  `DENOMINATED_IN`

공개펀드는 family `Fund`와 실제 class product `FundClass`를 `HAS_CLASS`로 연결합니다.
명시적 source ID가 없는 manager/index/issuer/benchmark는 source scope 안에서만 exact
normalized label identity를 사용하며 fuzzy merge하지 않습니다. NULL, blank, 명시적
unavailable sentinel에는 edge를 만들지 않습니다. 채권 `PD_RISK_GCD`는 현재 source
의미상 신용등급으로 입증되지 않았으므로 `HAS_CREDIT_RATING`을 만들지 않고
`HAS_RISK_GRADE`로만 적재합니다.

모든 node와 edge는 `dataset_snapshot`, graph version 및 source provenance를
보존합니다. Runtime은 metadata status가 `ready`이고 version/snapshot이 정확히
일치할 때만 query하며 missing/stale graph는 fail closed합니다.

### Offline Graph Build

Neo4j Community를 준비한 뒤 API request와 분리된 command를 실행합니다.

```bash
DATABASE_URL=postgresql+psycopg://... \
DATA_SNAPSHOT_DATE=2026-07-11 \
NEO4J_URI=neo4j://localhost:7687 \
NEO4J_USER=neo4j \
NEO4J_PASSWORD=... \
uv run python -m app.graph.ingest
```

Build는 ontology mapping을 먼저 검증하고, bounded batch로 M10 label 범위만
재구축하며, constraints와 stable node/edge ID를 사용해 idempotent합니다. 완료 전
metadata는 `building`, count 검증 성공 후에만 `ready`가 됩니다.

`QueryStep`은 canonical relation, direction, source node ID, dependency candidate ID만
전달합니다. Cypher relationship type은 `GraphMappingRegistry` allow-list에서만 나오고
value는 parameter로 bind됩니다. Raw user text를 Cypher에 연결하지 않습니다. 현재
baseline은 direct/reverse relation과 최대 depth 2의 explicit path를 지원하며,
unbounded traversal은 허용하지 않습니다. Structured filter와 관계가 결합되면 RDB가
bounded candidates를 만들고 Graph가 그 candidate set 안에서만 traversal한 뒤 기존
INTERNAL transform이 canonical entity ID로 결합합니다.

현재 graph relation parser는 운용사, 발행사, 기초/추종지수, 벤치마크, 펀드 클래스,
표시통화, 위험등급의 deterministic 표현만 지원합니다. 자연어 multi-hop planning,
fuzzy supporting-entity resolution, learned graph ranking, graph analytics는 M11+ 범위입니다.

## Evidence Layer

```text
ExecutionResult
  -> GenericEvidenceBuilder
  -> EvidenceBundle
  -> QualityAwareEvidenceValidator
     -> execution integrity
     -> required fields
     -> missing / sentinel values
     -> entity consistency
     -> conflicting values
     -> snapshot consistency
     -> coverage policy
  -> Answerability
```

Production 경로는 `execute_with_result()`를 사용해 `failed`, `timed_out`, `skipped`, success-empty를 Evidence Validator까지 전달합니다. 기존 `execute() -> list[RetrievalRecord]`는 이전 abstraction과 격리 테스트 호환을 위해 유지합니다.

Validator는 모든 finding을 deterministic 순서로 수집하고, blocking finding이 하나라도 있으면 unanswerable로 결정합니다. Safe Response는 canonical reason code를 사용하므로 timeout, missing field, conflict, snapshot mismatch, insufficient coverage를 추측 없이 구분합니다. Invalid sentinel의 raw value는 provenance를 위해 보존하되 `retrieved_context`에 quality finding을 함께 표시합니다.

M6의 `StaticFieldQualityProvider`는 validation architecture 테스트용 fixture로 유지합니다. Real RDB runtime은 ingestion이 생성한 profile을 `DatabaseFieldQualityProvider`로 조회합니다. 임의 coverage threshold와 임의 staleness 기간은 사용하지 않습니다.

## Data Ingestion

Excel은 API request에서 읽지 않습니다. 원본 `material/1.금융상품` 파일은 수정하지 않으며 다음 offline command가 schema validation, cleaning, canonical mapping, quarantine, quality profiling을 한 transaction 흐름으로 실행합니다.

원본 Excel은 저장소에 커밋하지 않습니다(`material/`은 `.gitignore`에 포함). 각 팀원은 로컬의 `material/1.금융상품/`에 아래 이름으로 파일을 배치합니다. `YYYYMMDD`는 데이터 스냅샷 날짜입니다.

```text
PRBD01N001_YYYYMMDD_datarows.xlsx
PRBD01N001_YYYYMMDD_schema.xlsx
PREF01N001_YYYYMMDD_datarows.xlsx
PREF01N001_YYYYMMDD_schema.xlsx
PREF02N001_YYYYMMDD_datarows.xlsx
PREF02N001_YYYYMMDD_schema.xlsx
PRFD01N001_YYYYMMDD_datarows.xlsx
PRFD01N001_YYYYMMDD_schema.xlsx
```

```bash
uv run python -m app.data.ingest
```

다른 material root 또는 database를 사용하려면 다음처럼 지정합니다.

```bash
uv run python -m app.data.ingest \
  --material-root material \
  --database-url sqlite:///data/financial_agent.db
```

동일 dataset snapshot은 replace-snapshot transaction으로 다시 적재됩니다. 따라서 command를 반복해도 logical row가 중복되지 않습니다. 치명적인 schema 오류는 dataset transaction을 rollback하며, 개별 invalid row는 source file/row/reason/raw payload와 함께 `quarantine_records`에 보존합니다.

온톨로지의 280개 칼럼 매핑을 실행 명세로 사용하는 evidence-first 옵션,
dataset 선택, dry-run, SHACL gate, Graph projection 및 검색 문서 준비 방법은
[`docs/ontology_ingestion.md`](docs/ontology_ingestion.md)를 참고하세요.

## Database Setup

Production PostgreSQL 예시:

```text
DATABASE_URL=postgresql+psycopg://financial_agent:change-me@localhost:5432/financial_agent
```

격리된 local test에서는 `DATABASE_URL=sqlite:///data/financial_agent.db`를 사용할
수 있습니다.

Production database는 PostgreSQL이며 Psycopg 3 driver를 사용합니다. 같은
SQLAlchemy 2.x schema와 query compiler를 SQLite test에도 사용하며 application
query code는 PostgreSQL 전용 SQL에 의존하지 않습니다. Credential은 source
code나 log에 기록하지 않습니다. `DATABASE_URL`이 없는 test/dev runtime은 기존
fake RDB를 사용하므로 외부 DB가 unit test의 필수 조건이 아닙니다.

PostgreSQL integration test는 운영 DB가 아닌 격리된 database URL만 받습니다.

```bash
POSTGRES_TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/testdb \
  uv run pytest -m postgresql tests/test_postgresql_integration.py
```

## Canonical Schema

```text
source_domestic_bonds / source_domestic_etfs
source_foreign_etfs / source_public_funds
                  ↓
          canonical_products
           ├── bond_attributes
           ├── etf_attributes
           ├── funds / fund_classes
           └── product_identifiers
                  ↓
       field_quality_profiles
```

Canonical product ID는 source namespace와 source key로 결정됩니다. 예: `bond_kr:<PD_NO>`, `etf_kr:<pd_itm_no>`, `etf_gl:<pd_itm_no>`, `fund_pub:<itm_no>:<class_code>`. 이름 유사도로 상품을 병합하지 않습니다.

`product_identifiers`는 실제 source에 존재하는 source ID, ISIN, ticker, KSD, MA, FSS, Lipper identifier를 canonical product에 연결합니다. 공모펀드는 `funds`와 `fund_classes`로 분리하며 class identity는 실제 파일에서 unique한 `itm_no + prfd_attr_cd`를 사용합니다.

## Quality Profiling

Ingestion은 dataset/product-type/canonical-field별로 다음을 계산합니다.

```text
total_count
valid_count
missing_count
coverage_fraction
unique_count
constant_value / is_constant
```

`DatabaseFieldQualityProvider`가 이 profile을 M6 Validator에 공급합니다. Coverage
threshold를 임의로 사용하지 않으며 complete/partial/unknown과 constant 여부를
기반으로 ranking safety를 결정합니다. 금액 ranking은 currency를 보존하고 단일
통화 범위에서만 answerable하므로 환율 환산 없이 서로 다른 통화를 비교하지
않습니다. `StaticFieldQualityProvider`는 validator isolation test를 위해 유지합니다.

## Real RDB Retrieval

```text
GroundedQuery
  -> structured QueryStep
  -> allow-listed RDBQueryCompiler
  -> SQLAlchemy expression
  -> RealRDBRetriever
  -> compact RetrievalRecord + source provenance
```

Canonical field, operator, sort, projection, product type만 allow-list를 통과합니다.
`QueryStep`은 SQLAlchemy expression으로만 compile되며 raw user text를 SQL 문자열에
연결하지 않습니다. `RDB_DEFAULT_LIMIT`으로 product result 수를 제한합니다.
`RDBEntityLookup`은 actual product name, short name, ticker, ISIN과 source
identifier의 normalized exact match만 지원합니다.

## M10.5 Semantic Safety Boundary

Deterministic parsing은 일부 표현만 인식한 상태로 retrieval을 실행하지 않습니다.

```text
Question
  -> stable material constraints (C1, C2, ...)
  -> ontology grounding
  -> QueryStep coverage mapping
  -> no-invention + no-omission validation
  -> execute or explicit safe block
```

각 material constraint는 source span, raw text, semantic type, status와
constraint ID를 보존합니다. QueryStep은 `covers_constraint_ids`로 자신이 실제
구현하는 조건을 선언합니다. 다음 상태가 있으면 retriever 실행 전에
fail-closed safe response로 전환합니다.

- unparsed material text
- unsupported or unresolved structured constraint/relation
- uncovered constraint or unsupported intent
- execution을 보장할 수 없는 hybrid ranking

Flat filter와 별도로 `Predicate`/`And`/`Or`/`Not` boolean contract를 제공하며,
단순 region OR는 canonical `IN`으로 최적화합니다. `NE`는 SQL NULL을 포함하지
않는 conservative policy를 사용합니다. Typed percentage/금액은 raw value,
normalized value, unit/currency를 함께 보존하지만 source dataset의 단위 계약이
검증되지 않은 경우 실행하지 않습니다.

Graph relation은 subject type, direction, target type/value 및 chain identity를
보존합니다. Target value는 Cypher parameter로만 전달되며 raw user text가 label,
relation type 또는 다른 Cypher identifier가 되지 않습니다.

현재 COUNT, historical snapshot selection, comparison, recommendation 및
conjunctive semantic predicates는 명시적으로 unsupported입니다. 향후 rule/LLM
semantic parser는 동일한 `ParsedQuery`/constraint schema를 출력해야 하며 별도
LLM 전용 plan schema를 사용하지 않습니다.

## M10.6 HyperCLOVA X Semantic Parser Fallback

Semantic parsing은 rule-first 방식입니다.

```text
Question
  -> RuleBasedQueryAnalyzer
  -> complete: ParsedQuery (network call 없음)
  -> incomplete/descriptive fallback: HCX-007 structured candidate
  -> deterministic span/schema/allow-list/coverage validation
  -> existing ParsedQuery
  -> existing RDF ontology grounding
```

HyperCLOVA X는 raw semantic candidate만 제안합니다. QueryPlan, SQL, Cypher,
retriever 선택, 추천, evidence 판단 또는 최종 답변은 생성하지 않습니다. 응답은
Chat Completions v3 Structured Outputs JSON schema와 strict Pydantic model을 모두
통과해야 하며, source span과 원문 불일치, 알려지지 않은 field/relation/operator,
숫자 단위 불일치 또는 material condition 누락이 있으면 폐기됩니다. Constraint ID와
numeric normalization은 application이 deterministic하게 생성합니다.

LLM candidate가 승인된 뒤에도 ontology URI와 canonical concept는 기존
`RDFOntologyService`만 결정합니다. Aggregate, recommendation, temporal/grouped
boolean 등 execution 미지원 의미는 정확히 parsing되더라도 M10.5 boundary에서
차단됩니다. Timeout, 5xx, invalid JSON/schema 및 validation failure 시 partial rule
parse를 실행하지 않고 기존 safe response를 반환합니다. Retry/repair loop와 parser
cache는 이번 milestone에 포함하지 않습니다.

Parser prompt에는 전체 TTL 대신 ontology/runtime에서 export한 product type, region,
asset type, field, relation alias의 compact vocabulary만 포함됩니다. `think_trace`에는
prompt, raw model response 또는 reasoning을 넣지 않고 parser path, validation status,
model 및 stage latency summary만 기록합니다.

기본 test suite는 API key 없이 offline fake parser로 실행됩니다. 실제 integration은
credential이 있는 환경에서만 실행됩니다.

```bash
uv run pytest tests/test_m10_6_semantic_parser.py
uv run pytest -m hyperclova tests/test_hyperclova_semantic_integration.py
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Environment Variables

설정 예시는 `.env.example`에 있습니다. 현재 baseline 실행에는 API key가 필요하지 않습니다.

```text
CLOVASTUDIO_API_KEY=
HYPERCLOVA_BASE_URL=https://clovastudio.stream.ntruss.com
HYPERCLOVA_MODEL=HCX-007
HYPERCLOVA_TIMEOUT_SECONDS=30
HYPERCLOVA_MAX_COMPLETION_TOKENS=2048
DATA_SNAPSHOT_DATE=2026-07-11
APP_TIMEOUT_SECONDS=280
RETRIEVAL_STEP_TIMEOUT_SECONDS=10
DATABASE_URL=postgresql+psycopg://financial_agent:change-me@localhost:5432/financial_agent
RDB_DEFAULT_LIMIT=10
RDB_MAX_LIMIT=10000
SEMANTIC_INDEX_PATH=data/semantic_search.db
SEMANTIC_INDEX_VERSION=m9-strategy-v1
EMBEDDING_MODEL=multilingual-semantic-hash-v1
EMBEDDING_DIMENSION=384
BM25_TOP_K=10
VECTOR_TOP_K=10
SEMANTIC_CANDIDATE_LIMIT=10000
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j
GRAPH_VERSION=m10-minimal-graph-v1
GRAPH_INGEST_BATCH_SIZE=1000
GRAPH_QUERY_LIMIT=100
GRAPH_MAX_DEPTH=2
LOG_LEVEL=INFO
```

빠른 격리 test에서는 `sqlite:///...` URL을 사용할 수 있습니다.

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
  "retrieved_context": "[Evidence 1]\nsource_type=rdb\nsource_id=fake-pipeline-record-001\nentity_id=fake-pipeline-record-001\nfield=pipeline_status\nvalue=deterministic_test_record\ntext=M2 pipeline test evidence only; this is not financial product data.\ndataset_snapshot=2026-07-11\nmetadata={\"dataset_snapshot\":\"2026-07-11\",\"fake\":true}",
  "think_trace": "{\"steps\": [\"query_understanding\", \"entity_resolution\", \"ontology_grounding\", \"planning\", \"execution\", \"evidence_building\", \"validation\", \"answer_generation\"], \"status\": \"success\", \"planner\": \"rule\", \"evidence_count\": 1, \"validation_reasons\": [\"usable_fake_evidence_available\"]}",
  "answer": "M2 pipeline test answer generated from validated fake evidence."
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

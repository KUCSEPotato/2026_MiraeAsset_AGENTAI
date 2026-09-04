# Financial Semantic Agent

금융 상품 질의응답 Agent를 위한 competition evaluation API입니다. Production은
2026-08-24 (`260824`) source generation의 `canonical_v2` PostgreSQL snapshot과
Team Ontology `merged-optical-1.4`를 사용합니다. PostgreSQL에서 파생한 Neo4j graph와
6,026-document BM25/vector artifact는 동일 version bundle로 startup compatibility
검사를 통과해야 합니다.
`DATABASE_URL`은 필수이며 PostgreSQL URL만 허용됩니다. `NEO4J_URI`와 credential이
함께 설정되면 Graph도 real retriever를 사용합니다.

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
- Semantic Grounding은 raw 표현을 보존하면서 ontology URI, canonical name,
  runtime key와 mapping version을 별도로 유지합니다.
- M2 fake semantic components는 격리 테스트와 failure simulation을 위해 유지합니다.

Team mode는 제출용 module
`ontology/common.ttl`, `bond_kr.ttl`, `etf_kr.ttl`, `etf_gl.ttl`,
`fund_pub.ttl`을 모두 load한 뒤 version/URI를 검증하고 한 번 index합니다. 하나라도
누락되거나 parse에 실패하면 초기화를 중단합니다. Split 이전 merged artifact
`ontology/candidates/new_optical_ontology.ttl`은 graph-level semantic equivalence
검증 기준선으로 유지됩니다. Ontology meaning과 PostgreSQL/Neo4j physical mapping은 versioned
`TeamOntologyRuntimeMapping`에서 분리됩니다. `legacy` mode는 명시적 v1 rollback과
migration regression에만 남습니다. Runtime은 authoritative data에 실제로 매핑되는
controlled individual만 executable constraint로 사용합니다.

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

Production composition은 항상 PostgreSQL RDB, PostgreSQL-derived Vector/BM25 source를
사용하고, Neo4j 설정이 완전하면 Graph source도 real retriever를 사용합니다.
Fake retriever는 constructor-injected isolation test에서만 유지합니다.
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

BM25는 현재 6,026개 문서에 적합한 portable BM25 Okapi 구현입니다. 전문용어와
영문 phrase를 lexical token으로 검색하며 score는 raw BM25 provenance일 뿐 금융
속성이 아닙니다.

Vector baseline은 `EmbeddingProvider` 뒤의
`multilingual-semantic-hash-v1`을 사용합니다. 외부 credential과 model download 없이
재현 가능한 한국어/영어 금융전략 concept feature와 hashed lexical feature를
384차원으로 encoding합니다. 실제 persisted vector/cosine search를 수행하지만
neural foundation embedding은 아니며, 향후 multilingual neural provider로 교체할
수 있는 초기 offline baseline입니다. Vector similarity 역시 금융 사실이 아니라
ranking metadata로만 저장합니다.

검색 artifact는 PostgreSQL canonical snapshot에서 생성한 versioned JSON projection이며,
metadata-indexed documents와 packed float vectors를 포함합니다. 현재 규모에서는
deterministic exact cosine scan을 사용합니다. 이 artifact는 canonical factual store가
아니며 재생성 가능합니다. pgvector는 extension 운영 dependency를 피하기 위해 M9
default로 선택하지 않았습니다.

### Index Build

API request에서 corpus embedding을 만들지 않습니다. canonical DB ingestion 후 다음
offline command를 실행합니다.

```bash
DATABASE_URL=postgresql+psycopg://... \
DATA_SNAPSHOT_DATE=2026-08-24 \
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
  -> GraphMappingRegistry (Team Ontology domain/range validation)
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

- Product/entity grain: `ETF`, `ETN`, `Bond`, `Fund`, `FundShareClass`, `SaleLot`
- Supporting entity: `AssetManagementCompany`, `Organization`, `Index`,
  `ExposureRegion`, `AssetClass`, `RiskGrade`, `CreditRating`, `Currency`
- Core edge: `MANAGED_BY`, `ISSUED_BY`, `TRACKS_INDEX`,
  `HAS_UNDERLYING_INDEX`, `HAS_BENCHMARK`, `HAS_SHARE_CLASS`,
  `HAS_EXPOSURE_REGION`, `HAS_ASSET_CLASS`, `HAS_RISK_GRADE`,
  `DENOMINATED_IN`, `TRADED_IN_CURRENCY`

공개펀드는 family `Fund`와 semantic entity `FundShareClass`를
`HAS_SHARE_CLASS`로 연결합니다. M10.7에서는 current PostgreSQL compatibility grain을
storage adapter로 유지하며 physical redesign은 M10.8로 연기합니다.
명시적 source ID가 없는 manager/index/issuer/benchmark는 source scope 안에서만 exact
normalized label identity를 사용하며 fuzzy merge하지 않습니다. NULL, blank, 명시적
unavailable sentinel에는 edge를 만들지 않습니다. 채권 `PD_RISK_GCD`는 현재 source
새 source의 `crd_grd`는 credit-rating source로 검증되어 `HAS_CREDIT_RATING`으로
적재하며, 별도 risk-grade source는 `HAS_RISK_GRADE`로 유지합니다.

모든 node와 edge는 `dataset_snapshot`, graph version 및 source provenance를
보존합니다. Runtime은 metadata status가 `ready`이고 version/snapshot이 정확히
일치할 때만 query하며 missing/stale graph는 fail closed합니다.

### Offline Graph Build

Neo4j Community를 준비한 뒤 API request와 분리된 command를 실행합니다.

```bash
DATABASE_URL=postgresql+psycopg://... \
DATA_SNAPSHOT_DATE=2026-08-24 \
NEO4J_URI=neo4j://localhost:7687 \
NEO4J_USER=neo4j \
NEO4J_PASSWORD=... \
ONTOLOGY_VERSION=team-v1 \
GRAPH_VERSION=m10.7-team-v1-20260829 \
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

현재 graph relation parser는 relation/field registry와 composable grammar를 통해
운용사, 발행사, 기초/추종지수, 벤치마크, 펀드 클래스, 표시통화, 위험등급을
구조화합니다. 상품명이나 acceptance 질문 문자열별 handler는 두지 않습니다.
자연어 multi-hop planning,
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
  --database-url postgresql+psycopg://financial_agent:change-me@localhost:5432/financial_agent
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

PostgreSQL은 production, local development, ingestion verification 및 integration
test의 유일한 relational backend이며 Psycopg 3 driver를 사용합니다. `DATABASE_URL`은
필수이고 PostgreSQL URL이 아니면 startup이 명시적으로 실패합니다. DB가 필요 없는
parser/grounding/planner unit test만 repository fake를 사용합니다. Credential은 source
code나 health metadata, log에 기록하지 않습니다.

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

## M10.8-B Canonical v2 Clean Rebuild

`canonical_v2`는 authoritative 2026-08-24 (`260824`) 4개 workbook에서 직접
재구축됩니다. v1 canonical row는 entity materialization 입력으로 사용하지 않으며,
identity crosswalk와 regression 비교에만 읽습니다.

```bash
uv run alembic upgrade head
uv run python -m app.data.v2_rebuild --material-root material
```

snapshot은 source/schema checksum, Team Ontology version, semantic mapping version,
transformer version 및 DB schema version이 일치하고 reconciliation이 통과한 뒤에만
`READY`가 됩니다. 동일 입력의 두 번째 실행은 `SKIPPED_UNCHANGED`입니다. 현재
production repository, Neo4j, Vector/BM25와 `/answer`는 계속 v1을 사용하며 v2 runtime
cutover는 M10.8-C 이후 범위입니다.

## M10.8-C Canonical v2 RDB Repository

기존 `QueryPlan`을 변경하지 않고 allow-listed SQLAlchemy compiler와 typed adapter를
통해 `canonical_v2`를 조회합니다. M10.8-E 이후 production 기본 profile은 `v2`이며
`v1`은 restart-only rollback profile입니다.

```text
RUNTIME_DATA_VERSION=v2
CANONICAL_V2_GENERATION=260824
CANONICAL_V2_ONTOLOGY_VERSION=merged-optical-1.4
```

v2 mode는 지정 generation/date의 4개 source snapshot이 모두 `READY / PASSED`이고
Graph/Semantic manifests가 같은 bundle일 때만 실행됩니다. v1 table fallback은
없습니다. Public Fund는 product type이 아니라
`Fund WHERE EXISTS FundShareClass WITH OfferingType.PUBLIC`으로 조회하며 Fund,
FundShareClass, Bond, SaleLot grain을 분리합니다. AUM/expense-ratio의 filter/sort는
비교 단위 계약이 승인될 때까지 닫혀 있습니다.

## Requirements

- Ubuntu 24.04 LTS (production assumption)
- Docker Engine and Docker Compose v2
- Python 3.12 and [uv](https://docs.astral.sh/uv/) for non-container development
- PostgreSQL and Neo4j reachable only over the Compose internal network

## Environment Variables

설정 예시는 `.env.example`에 있습니다. Production v2는
`RUNTIME_DATA_VERSION=v2`와 coherent RDB/Graph/Semantic manifests가 필요합니다.
Rule-complete parsing은 LLM parser를 호출하지 않지만 live answer generation을 켜려면
`CLOVASTUDIO_API_KEY`와 `HYPERCLOVA_ANSWER_ENABLED=true`가 필요합니다.

```text
CLOVASTUDIO_API_KEY=
HYPERCLOVA_BASE_URL=https://clovastudio.stream.ntruss.com
HYPERCLOVA_MODEL=HCX-007
HYPERCLOVA_TIMEOUT_SECONDS=30
HYPERCLOVA_MAX_COMPLETION_TOKENS=2048
HYPERCLOVA_ANSWER_ENABLED=true
HYPERCLOVA_ANSWER_MODEL=HCX-007
HYPERCLOVA_ANSWER_TIMEOUT_SECONDS=45
HYPERCLOVA_ANSWER_MAX_COMPLETION_TOKENS=1024
DATA_SNAPSHOT_DATE=2026-08-24
APP_TIMEOUT_SECONDS=240
RETRIEVAL_STEP_TIMEOUT_SECONDS=10
POSTGRES_DB=financial_agent
POSTGRES_USER=financial_agent
POSTGRES_PASSWORD=change-me
DATABASE_URL=postgresql+psycopg://financial_agent:change-me@postgres:5432/financial_agent
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=5
DATABASE_POOL_TIMEOUT_SECONDS=30
DATABASE_POOL_RECYCLE_SECONDS=1800
RDB_DEFAULT_LIMIT=10
RDB_MAX_LIMIT=10000
RUNTIME_DATA_VERSION=v2
CANONICAL_V2_GENERATION=260824
CANONICAL_V2_ONTOLOGY_VERSION=merged-optical-1.4
CANONICAL_V2_TRANSFORMER_VERSION=fund-unresolved-parent-evidence-1
SEMANTIC_ARTIFACT_ROOT=/srv/financial-semantic-agent/artifacts/260824
SEMANTIC_INDEX_PATH=/var/lib/financial-semantic-agent/v1/semantic_search.json
SEMANTIC_INDEX_VERSION=m10.7-strategy-20260829
EMBEDDING_MODEL=multilingual-semantic-hash-v1
EMBEDDING_DIMENSION=384
BM25_TOP_K=10
VECTOR_TOP_K=10
SEMANTIC_CANDIDATE_LIMIT=10000
NEO4J_URI=neo4j://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j
NEO4J_CONNECTION_TIMEOUT_SECONDS=5
NEO4J_CONNECTION_ACQUISITION_TIMEOUT_SECONDS=5
NEO4J_MAX_TRANSACTION_RETRY_SECONDS=5
GRAPH_VERSION=m10.7-team-v1-20260829
ONTOLOGY_VERSION=team-v1
GRAPH_INGEST_BATCH_SIZE=1000
GRAPH_QUERY_LIMIT=100
GRAPH_MAX_DEPTH=2
CANONICAL_V2_GRAPH_PROJECTION_VERSION=m10.9-c2.5-step3-canonical-v2-graph-2
CANONICAL_V2_SEMANTIC_INDEX_PATH=/var/lib/financial-semantic-agent/canonical_v2/semantic_search.json
CANONICAL_V2_SEMANTIC_INDEX_VERSION=m10.9-c2-canonical-v2-semantic-1
RUNTIME_ENVIRONMENT=production
PUBLIC_BASE_URL=https://PUBLIC_HOST
LOG_LEVEL=INFO
API_BIND_ADDRESS=0.0.0.0
API_PORT=8000
```

SQL execution, schema, constraints, NULL behavior, transaction 및 RDBRetriever test는
반드시 disposable PostgreSQL에서 실행합니다.

실제 secret을 담은 `.env`는 Git에 포함하지 않습니다.

## Local Setup

```bash
uv sync --all-groups
```

## Running the Server

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Production에서는 [deployment runbook](deploy/README.md)의 Docker Compose stack을
사용합니다. `agent-api`만 port를 publish하며 PostgreSQL과 Neo4j는 Compose network
안에서만 접근합니다. Process liveness는 `/live`, coherent runtime readiness는
`/health`에서 구분됩니다.

## Evaluation API

인증 header 없이 다음 query parameter를 받습니다.

```text
GET /answer?question_id=<string>&question=<string>
```

빈 값이나 공백뿐인 값은 `422`를 반환합니다. 정상 요청은 정확히 다섯 개의 string field를 반환합니다.

## Evaluation API End-point

```text
${PUBLIC_BASE_URL}/answer
```

최종 Naver Cloud stable URL은 배포 후 `PUBLIC_BASE_URL` placeholder를 실제 HTTPS
endpoint로 교체해야 합니다. Evaluator에는 인증 header가 필요하지 않습니다.

## Example Request

```bash
curl -G "http://localhost:8000/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=미국 증시에 상장된 주식형 ETF 중 순자산이 큰 상품 3개"
```

## Example Response

```json
{
  "question_id": "Q-001",
  "question": "국내 ETF 중 운용보수가 낮은 상품을 알려줘",
  "retrieved_context": "{...validated canonical evidence...}",
  "think_trace": "{\"steps\":[\"query_understanding\",\"entity_resolution\",\"ontology_grounding\",\"planning\",\"execution\",\"evidence_building\",\"validation\",\"answer_generation\"],\"status\":\"success\",\"planner\":\"rule\"}",
  "answer": "검증된 데이터 기준 결과입니다. ..."
}
```

## Health Check

```bash
curl "http://localhost:8000/health"
```

Ready server는 HTTP 200과 `status=ok`, `process_status=alive`,
`readiness_status=READY`, active bundle metadata를 반환합니다. Store/manifest가
호환되지 않는 v2 process는 startup에서 fail closed하며 v1으로 자동 전환하지 않습니다.

## Tests

```bash
uv run pytest
```

## canonical_v2 Runtime Bundle and Cutover (M10.8-E)

`canonical_v2` PostgreSQL is the semantic source of truth.  Neo4j and the
Vector/BM25 artifact are deterministic, separately versioned projections; they
never read Excel rows or reconstruct missing relations.

```text
canonical_v2 PostgreSQL
 ├── app.graph.ingest_v2      → isolated Neo4j M108DNode namespace
 └── app.search.index_v2      → data/canonical_v2 semantic artifact
```

`RUNTIME_DATA_VERSION` selects exactly one coherent bundle.  The intended
deployment profile is `v2`; `v1` remains the explicit rollback profile.  A
v2 process requires all three compatible stores and fails closed when the RDB
snapshot, graph manifest, or semantic manifest is absent, stale, failed, or
version-mismatched.  It never falls back to v1 during a v2 request:

```text
RUNTIME_DATA_VERSION=v2
CANONICAL_V2_SEMANTIC_INDEX_PATH=data/canonical_v2/semantic_search.json
```

For compatibility with older deployment files, `RDB_REPOSITORY_VERSION=v2`
and `CANONICAL_V2_MULTI_STORE_ENABLED=true` may be supplied together, but a
partial legacy selection is rejected.  Do not point v2 mode at a v1 Neo4j
graph or semantic index.

The startup `/health` response identifies the selected bundle and reports
generation, snapshot, ontology, canonical schema, transformer, per-store
readiness, and overall compatibility.  It contains no credentials.

Build the v2 stores after applying Alembic migrations and rebuilding the
authoritative snapshot:

```bash
uv run python -m app.graph.ingest_v2
uv run python -m app.search.index_v2
RUNTIME_DATA_VERSION=v2 uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
curl --fail http://127.0.0.1:8000/health
```

To roll back without rebuilding data, restart with `RUNTIME_DATA_VERSION=v1`.
The v1 tables, graph/index artifacts, and implementation are retained.

실행 중인 서버를 평가 서버 방식으로 검사하려면:

```bash
uv run python scripts/smoke_test.py \
  --question-id Q-001 \
  --question "평가 질의"
```

## Production Deployment and Evaluation Rehearsal

Naver Cloud prerequisites, stable paths, Docker Compose build/start commands, persistent
volumes, firewall policy, startup readiness, and API-only rollback recreation are in
[deploy/README.md](deploy/README.md). Persistent PostgreSQL/Neo4j data and the semantic
artifact must not live in `/tmp`. Production secrets come only from a server-local `.env`;
that file is excluded from Git and the Docker build context.

After deployment, validate externally and preserve the JSON artifact:

```bash
uv run python scripts/smoke_test.py \
  --base-url "$PUBLIC_BASE_URL" \
  --question-id M109-SMOKE \
  --question "미국 주식형 ETF를 알려줘."

uv run python scripts/m10_9_rehearsal.py \
  --base-url "$PUBLIC_BASE_URL" \
  --repetitions 3 \
  --output artifacts/m10_9_rehearsal.json
```

The rehearsal is sequential, repeats identical `question_id`/question pairs, validates the
exact five-string response schema, records semantic fingerprints, and reports p50/p95/max.
Semantic unanswerability (`ENTITY_NOT_FOUND`, `ENTITY_AMBIGUOUS`,
`UNSUPPORTED_CONSTRAINT`, `ZERO_MATCH`, `INSUFFICIENT_EVIDENCE`) remains an HTTP 200 safe
response. Request timeout and answer-generation dependency failure use controlled 504/503 so
the evaluator may retry; malformed input remains 422.

Rollback is only `RUNTIME_DATA_VERSION=v1` plus process restart. Rebuild, re-ingestion, and
migration commands are forbidden during `v2 -> v1 -> v2` rollback rehearsal.

## Structured Metric Operations (M10.9-C1)

Ranking is authorized by a source-scoped comparison contract, not by a global
sortable-field flag. `ORDER BY` and bounded Top-N are compiled from structured
operations with SQLAlchemy. PREF01/KRW and PREF02/USD AUM are sortable only
inside their own source scope and are never compared across sources. Expense
ratio ranking remains fail-closed because neither source contract defines its
numeric scale. Bond credit ratings use an explicit ordinal vocabulary.
Organizer evaluation semantics ignore buyable quantity: a Bond is purchasable
unless an authoritative delisted/listing-ended fact exists by the 2026-08-24
cutoff. The current PRBD source has no such lifecycle field, so it is treated as
the cutoff Bond universe without inventing a SaleLot availability condition.

`ONE_YEAR_RETURN` is a distinct exact-period metric. PREF01 `du_er_1y` is
rankable only for the DomesticETF source scope. PREF02 has no 1Y return field,
and PRFD return is FundShareClass-grain, so cross-product ETF/Public Fund
ranking fails closed. Validated product-universe unions are planned as one
global candidate set; arbitrary grouped boolean predicates remain unsupported.

The reviewed `ISHARES_FOREIGN_ETF_ONE_YEAR_RETURN` scope adds official
2026-07-31 iShares NAV total returns for EWY, IYW, and SOXX. The issuer defines
the published metric as NAV change including distributions. It is sortable
only inside the corresponding three-product iShares READY scope; generic
`ForeignETF` remains partial. PREF01 does not document enough NAV/market-price
or distribution methodology to compare its `du_er_1y` with the iShares metric,
so domestic+iShares return ranking deliberately remains fail-closed.

## Trusted Holdings Canonical Integration (M10.9-C2)

The holdings boundary consumes normalized, manifest-backed provider snapshots;
it does not crawl or call provider APIs. Eligible rows resolve the source ETF
and constituent by deterministic identifiers and materialize the temporal path:

```text
FinancialProduct -> HOLDS -> EquitySecurity -> SECURITY_ISSUED_BY -> Organization
```

Every projected relation is backed by a canonical fact and source evidence.
Name-only identities, post-cutoff rows, collisions, and unproven issuers fail
closed. Neo4j supports the reviewed reverse traversal and the two-hop issuer
path without storing a separate `HELD_BY` fact.

`TRUSTED_HOLDINGS_RUNTIME_ENABLED=0` is the deployment default. Enabling it
requires one or more reviewed `KODEX_LONG_ONLY_COMPATIBLE`,
`TIGER_LONG_ONLY_COMPATIBLE`, and
`ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS` READY scopes, evidence-backed PostgreSQL `HOLDS`
facts, and an exactly reconciled compatible graph manifest. The provider scopes
can be queried separately or as one bounded union; candidates are deduplicated
before a single global ranking. Only questions explicitly constrained to a
selected READY scope may traverse holdings. `KODEX_FULL`, `TIGER_FULL`,
`ISHARES_US_FULL`, `DomesticETF`, and generic `ForeignETF` remain partial
coverage, while `PublicFund` remains unsupported; those generic universes
therefore fail closed. Public-fund
holdings, sector inference, and short/derivative/leveraged position semantics
remain out of scope.

## Authoritative Security Issuer Mapping (M10.9-C2.6)

The scoped company-name path is prebuilt from the KRX KIND exact-cutoff
listed-company result:

```text
KODEX_LONG_ONLY_COMPATIBLE or TIGER_LONG_ONLY_COMPATIBLE Product
  -> HOLDS -> EquitySecurity
  -> SECURITY_ISSUED_BY -> Organization
```

Every graph edge retains its canonical fact ID, evidence assertion IDs, source
fields, and source-record keys. Company names resolve to an exact canonical
Organization and use only the reviewed two-hop path; a six-digit ticker
resolves to a canonical Security and keeps the narrower one-hop path. Exact
name collisions, missing representative-ticker evidence, and unknown companies
fail closed. The issuer snapshot is built over the exact union of selected
provider Security identities. This does not expand holdings coverage:
`KODEX_FULL`, `TIGER_FULL`, `DomesticETF`, generic ETF, Foreign ETF, and Public
Fund remain incomplete or unsupported as declared by the holdings coverage
registry.

Production activation requires both:

```text
TRUSTED_HOLDINGS_RUNTIME_ENABLED=1
TRUSTED_ISSUER_RUNTIME_ENABLED=1
```

`/health` reports `issuer_source`, `canonical_issuer`, `graph_issuer`, and
`company_query` readiness independently before the v2 bundle becomes READY.

# 2026_MiraeAsset_AGENTAI

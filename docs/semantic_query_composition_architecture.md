# Semantic Query / Planning 일반화: 사전 분석 및 구현 계획

분석 기준: `c7f07da` (사용자 요청으로 `origin/main` pull 후 재검토), 작업 브랜치 `feature/semantic-query-composition`.
이 문서는 구현 코드 수정 전에 작성했다.

## 1. 현재 Semantic Query representation

`app/domain/models.py`의 `ParsedQuery`는 entity/filter/sort/requested-field/relation
목록, BooleanExpression(AND/OR/NOT), result limit, COUNT aggregation,
snapshot용 TemporalConstraint와 source-span 기반 SemanticConstraint를 이미 가진다.
`ResolvedQuery` → `GroundedQuery`는 entity ID, ontology identity, canonical field,
grounding status를 보존한다. 새 질문별 handler 대신 이 표현을 확장한다.
`app/query/semantic_models.py`의 LLM candidate는 extra=forbid이고,
`semantic_validation.py`가 source span, vocabulary, material clause 보존을 검사한다.

## 2. 현재 planner

`app/planning/coordinator.py`는 FastRoutingChecker → RuleRouter 또는
DeterministicSupervisorPlanner → StructuredQueryPlanValidator 순서다.
`QueryStep.depends_on`으로 DAG를 표현하며 SQL/Cypher는 LLM이 생성하지 않는다.
rule route는 structured input을 RDB에 전달하고 supervisor는 주로
RDB → Semantic → Graph → internal intersection 순서를 만든다.
constraint coverage, invented field/entity, dependency cycle 검사가 있다.

## 3. 현재 filter / sort / metric

FilterSpec은 EQ/NE/LT/LTE/GT/GTE/IN/BETWEEN을 표현한다. SortSpec 목록과
SortOperation, TopN, OrderedComparison도 이미 있다. 하지만 BooleanExpression은
structured input에서 빠지고 실제 RDB 필터는 AND로 연결된다. CONTAINS는 없다.
canonical-v2 field mapping과 MetricCapabilityRegistry의 source-scoped 계약을
함께 사용해야 한다. expense ratio의 scale 미검증, AUM 통화 차이, return의
dataset/basis/grain 제한은 유지한다. numeric field라는 이유만으로 비교를 허용하지 않는다.

최신 checkout은 PREF01_RETURN_CONTRACTS로 **1D/1M/3M/6M/1Y/YTD**를 지원하며,
generic return은 승인된 default 1Y 정책을 적용한다. metric_resolution dict에
period/source가 있으나 재사용 가능한 typed TemporalSpec은 없다. 이 계약과 default
정책을 그대로 재사용한다. historical AUM series는 없으므로 snapshot에서 증가율을 계산하지 않는다.

## 4. 현재 graph planning

RelationMention/GroundedRelation은 direction, chain_id, path_position을 가진다.
registry 및 compiler가 승인한 1–2 hop parameterized traversal을 지원한다.
managedBy, index 관계, holds, securityIssuedBy 등이 존재한다. subsidiary는
실제 runtime graph registry에 없으므로 실행 불가로 유지한다. HoldingsCoverageRegistry의
정확한 provider scope와 snapshot readiness 조건도 유지한다.
graph result에 final Top-N을 먼저 적용하면 후속 ranking의 후보가 누락될 수 있다.

## 5. multi-field 지원과 evidence

requested_fields와 RDB projection 자체는 이미 목록을 지원하므로 entity resolution을
필드마다 반복할 필요가 없다. 그러나 internal non-ranking intersection은 entity당
record 하나를 선택하여 여러 field를 잃을 수 있다. ranking merge 역시 graph fact를
source ID provenance로만 남길 수 있다. required-field 검증은 entity×field보다
global field 존재에 의존한다. 따라서 projection 확장과 함께 merge 및 evidence
정합성을 강화해야 No-Omission을 지킬 수 있다.

## 6. 일반화 병목

- comparison intent는 sort가 있을 때만 허용되고 side-by-side multi-field comparison spec이 없다.
- Boolean tree가 parser에서 executor로 전달되지 않는다.
- temporal period와 metric field binding이 분리되어 있지 않다.
- graph/semantic 후보 생성 후 RDB filtering/projection/ranking을 수행하는 계획이 제한된다.
- capability metadata가 routing/ontology/compiler/metric contract에 분산되어 있다.
- aggregation은 COUNT 표현만 있고 group-by 실행 계약은 없다.
- evidence merge가 여러 store의 동일 entity facts를 모두 보존하지 않는다.

## 7. 최소 Semantic IR extension

기존 ParsedQuery/GroundedQuery를 호환 경계로 유지한다. strict reusable TemporalSpec,
MetricSpec, ComparisonSpec, GroupBySpec 및 grounded logical operator representation을
추가한다. 새 logical representation은 grounded input에서 결정론적으로 만들며 기존
constraint ID를 보존한다. ResolveEntity/Filter/ProjectField/ResolveMetric/Sort/
TopK/Compare/TraverseRelation/TemporalResolve/Aggregate/GroupBy를 표현하되
표현 가능성과 실행 가능성을 분리한다. unknown 값과 unsupported historical/grouped
연산은 capability validation에서 차단한다.

## 8. backward compatibility 전략

기존 public pipeline, QueryPlan DAG, parameterized compiler와 evidence response 계약을
유지한다. 새 필드는 기본값을 둔다. 기존 approved comparison 계약을 확대하지 않는다.
후보의 canonical entity ID를 store 사이에서 전달하고 provenance/snapshot을 보존한다.
limit은 전체 조건 적용 후 실행하거나 upstream completeness 검사로 누락을 차단한다.
LLM proposal은 기존 schema/span/vocabulary grounding 경계를 계속 통과해야 한다.

## 구현 계획

| Milestone | 변경 | 검증 |
|---|---|---|
| M1 | IR/temporal/operator 모델, 기존 표현 adapter, parser 연결 | strict schema, period mapping, source preservation, 기존 parse regression |
| M2 | Boolean predicate serialization/compiler, allow-listed CONTAINS, projection 보존 | parameterized SQL, invalid operator/type, AND/OR, ranking regression |
| M3 | multi-entity multi-field comparison와 field별 계약, entity×field evidence | 허용 scope, 비용 scale/혼합 dataset 차단, missing evidence |
| M4 | 승인된 relation chain composition, graph evidence 보존 | 실제 registry path, unknown relation/hop 차단, fixture traversal |
| M5 | deterministic federated DAG, complete candidate 전달 후 RDB 연산 | Graph→RDB / Semantic→RDB, dependency failure, truncation/snapshot |
| M6 | pre-execution capability gate 및 Level 1–5 composition tests | unknown fields/operators, historical/group-by fail closed, 전체 regression |

각 milestone에서 focused tests, 관련 regression, `git diff --check`를 실행하고
가능한 단위별 commit을 남긴다. Level 5는 존재하는 2-hop 관계의 긍정 사례와
subsidiary 등 미지원 관계의 부정 사례를 구분한다. fixture에 없는 실제 상품명을
새로 하드코딩하지 않는다.

## 작업 경계와 테스트 환경

TIGER 미국S&P500 production issue, 외부 데이터 수집, DB/Graph/Semantic rebuild,
deployment workflow/gate/secret, production release identity 변경은 범위 밖이다.
기존 untracked `Report/` 파일은 사용자 작업으로 보존한다. production deployment는
완료 조건이 아니다. PostgreSQL/Neo4j integration은 격리 test 설정이 있을 때만 수행한다.

테스트는 기존 `/private/tmp/structured-evidence-venv/bin/python` 환경에서 실행한다.
로컬 `.venv`의 conda readline extension은 pytest startup에서 segfault를 일으켜
다음 fallback runner로도 검증했다. application 코드는 변경하지 않는다: `.venv/bin/python -c 'import sys; sys.modules["readline"] = None;
import pytest; raise SystemExit(pytest.main([...]))'`.

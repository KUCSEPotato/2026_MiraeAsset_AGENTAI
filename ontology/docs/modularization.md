# Team Ontology 제출용 모듈화 감사

## 기준선

- 파일: `ontology/candidates/new_optical_ontology.ttl`
- ontology URI: `https://miraeasset.com/ontology/financial-product`
- namespace: `https://miraeasset.com/ontology/financial-product#`
- version: `merged-optical-1.4`
- triples: 1,244
- classes: 62
- object properties: 54
- datatype properties: 35
- subclass triples: 58
- domain triples: 60
- range triples: 89
- SHACL NodeShapes: 12
- OWL restrictions: 0
- inverse properties: 1
- equivalent classes/properties: 0/0

## 기존 loader

`team-v1`과 `v7`이 모두 `candidates/new_optical_ontology.ttl` 한 파일을 읽었다. 일부 holdings ontology test도 이 경로를 직접 parse했다. 제출 경로의 기존 5개 TTL은 현재 runtime과 다른 구형 namespace와 class model을 사용해 운영 의미 기준선으로 사용할 수 없었다.

## 변경된 loader

`team-v1`은 다음 registry를 순서대로 전부 읽는다.

1. `common.ttl`
2. `bond_kr.ttl`
3. `etf_kr.ttl`
4. `etf_gl.ttl`
5. `fund_pub.ttl`

누락 파일 목록을 검사한 후 parse하며, 누락 또는 parse 오류는 `OntologyLoadError`로 종료한다. `v7` compatibility mode는 기존 merged baseline 파일을 유지한다. Runtime test는 특정 파일 경로 대신 loader graph를 사용한다.

## 모듈별 inventory

| module | triples | classes | object properties | datatype properties | SHACL shapes |
|---|---:|---:|---:|---:|---:|
| `common.ttl` | 827 | 36 | 25 | 29 | 8 |
| `bond_kr.ttl` | 229 | 8 | 8 | 4 | 1 |
| `etf_kr.ttl` | 81 | 6 | 5 | 1 | 2 |
| `etf_gl.ttl` | 23 | 2 | 4 | 1 | 0 |
| `fund_pub.ttl` | 84 | 10 | 12 | 0 | 1 |
| union | 1,244 | 62 | 54 | 35 | 12 |

### common.ttl

공통 semantic/provenance 계층, Identifier, Organization, Security와 EquitySecurity, 공통 분류·controlled vocabulary, metric catalog, answerability rule, 공통 SHACL을 포함한다. `holds`와 `securityIssuedBy`의 유일한 authoritative declaration도 여기에 있다.

### bond_kr.ttl

Bond와 SaleLot grain, BondType 32종, 신용등급·금리·이자지급·거래 분류, 발행일·만기/콜일·발행금액, 채권 관계와 SaleLot shape를 포함한다.

### etf_kr.ttl

현재 ontology가 locale-specific ETF subclass를 갖지 않으므로 기존 ETP/ETF/ETN 공통 계층과 ETF/ETN 배타성, 지수 노출·복제 방식, ETF/ETN shape를 배치한다. 국내 전용 class를 새로 만들지 않았다.

### etf_gl.ttl

현재 global ETP 경로에서 사용되는 거래소·시장 정의와 listing/trading-country/currency, 장문 전략 속성을 배치한다. global Security identity 자체는 다른 상품군에서도 공유되므로 common에 유지한다.

### fund_pub.ttl

Fund와 FundShareClass를 분리하고 `hasShareClass` parent 관계, 수탁사·benchmark·펀드 분류·판매채널 및 FundShareClass shape를 포함한다. `PublicFund` subclass나 holdings relation은 추가하지 않았다.

## 동등성 방법

`scripts/modularize_ontology.py`는 merged graph의 named subject를 module responsibility에 따라 정확히 한 곳에 배치한다. SHACL list/property shape 등 blank-node subgraph는 소유 subject와 같은 module로 재귀 이동한다. 하나의 blank node가 module 경계를 넘으면 생성을 실패시킨다.

검증은 다음 두 단계다.

1. memory상 partition union과 merged baseline의 RDF graph isomorphism
2. 실제로 저장한 각 TTL을 독립 parse한 union과 baseline의 RDF graph isomorphism

추가 test는 class/object-property/datatype-property URI set과 subclass, subproperty, domain, range, disjoint, inverse, equivalent 공리를 명시적으로 비교한다.

## SHACL

Shape도 새로운 정의 없이 기존 triple을 그대로 이동했다.

- common: FinancialProduct, Identifier, ISIN, collision, Security, HOLDS, security issuer, SourceRecord
- etf_kr: ETF, ETN
- bond_kr: SaleLot
- fund_pub: FundShareClass

대표 valid graph와 `FinancialProduct holds Organization` invalid graph를 split 전후에 각각 검증해 동일한 conform/non-conform 결과를 요구한다.

## 변경하지 않은 영역

- PostgreSQL/canonical_v2 schema 및 row count
- Neo4j node/edge schema
- QueryPlan 및 retrieval semantics
- holdings ingestion과 coverage gating
- Fund/FundShareClass metric promotion
- global Security identity 규칙

## Semantic change

None. 파일 위치와 loader input registry만 변경했다.

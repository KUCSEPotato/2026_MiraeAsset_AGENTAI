# Financial Product Ontology

운영 Team Ontology `merged-optical-1.4`를 대회 제출 형식에 맞춰 5개 Turtle module로 제공한다. 애플리케이션은 다섯 파일을 모두 하나의 RDF graph로 읽으며, 하나라도 없거나 parse에 실패하면 초기화를 중단한다.

Namespace와 모든 resource URI는 module 분리 전과 동일하다.

```text
Ontology URI: https://miraeasset.com/ontology/financial-product
Namespace:    https://miraeasset.com/ontology/financial-product#
Version:      merged-optical-1.4
```

## 제출 module

```text
common.ttl
├── bond_kr.ttl
├── etf_kr.ttl
├── etf_gl.ttl
└── fund_pub.ttl
```

- `common.ttl`: 공통 canonical class, Security identity, Organization, Identifier, provenance/evidence, `holds`, `securityIssuedBy`, metric/answerability vocabulary와 공통 SHACL
- `bond_kr.ttl`: Bond, SaleLot, 채권 분류·금리·신용등급·만기 속성과 SaleLot SHACL
- `etf_kr.ttl`: 기존 공통 ETP/ETF/ETN 계층, 지수 노출·복제 방식 및 ETF/ETN SHACL
- `etf_gl.ttl`: 기존 해외 ETP 사용 경로의 거래소·시장·국가·거래통화·전략 설명 속성
- `fund_pub.ttl`: Fund, FundShareClass, parent/class 관계, benchmark·수탁·펀드 분류 및 FundShareClass SHACL

현재 ontology에는 `DomesticETF`나 `ForeignETF` 같은 locale-specific class가 없다. 이 분리 작업은 새 class를 만들지 않고 기존 triple만 위 책임에 따라 물리적으로 배치한다. `Security`, `EquitySecurity`, `holds`, `securityIssuedBy`는 국내·해외 상품에 모두 쓰이므로 `common.ttl`에 한 번만 선언한다.

Module은 서로 `owl:imports`로 개별 실행되지 않는다. `OntologyLoader`의 고정 registry가 정확히 5개를 모두 load한다. 이 방식은 common → domain module이라는 단방향 책임을 유지하면서 부분 graph를 READY로 오인하지 않게 한다.

## 의미 동등성 기준선

`candidates/new_optical_ontology.ttl`은 split 직전 merged graph 기준선이다. runtime은 제출 module을 읽고, 자동 검증은 기준선과 module union을 RDF graph isomorphism으로 비교한다.

```bash
# 5개 module과 merged 기준선의 동등성 확인
uv run python scripts/modularize_ontology.py

# merged 기준선에서 module을 재생성한 뒤 확인
uv run python scripts/modularize_ontology.py --write
```

검증 대상은 class/property URI, subclass, subproperty, domain, range, disjoint, inverse/equivalent 공리, SHACL blank-node 구조와 ontology metadata를 포함한 전체 RDF graph다. TTL text 순서나 blank-node label은 비교 기준으로 사용하지 않는다.

## 핵심 보존 경로

```text
FinancialProduct ──holds──> Security
Security ──securityIssuedBy──> Organization
```

- `holds` domain: `FinancialProduct`
- `holds` range: `Security`
- `securityIssuedBy` domain: `Security`
- `securityIssuedBy` range: `Organization`
- product `issuedBy`와 security `securityIssuedBy`는 서로 다른 relation

## Legacy 자료

다음 파일은 이전 모듈형 ontology와 mapping/sample 검증을 위한 legacy 자료다. 현재 `team-v1` runtime module registry에는 포함되지 않는다.

- `core.ttl`
- `products.ttl`
- `entities.ttl`
- `observations.ttl`
- `mappings.ttl`
- `shapes.ttl`
- `mappings/column_mapping.csv`
- `examples/sample_instances.ttl`
- `queries/*.rq`

## 검증

```bash
# 제출 module 전용: 존재, 독립 parse, graph 동등성, SHACL, fail-closed loader
uv run pytest tests/test_ontology_modularization.py

# Team Ontology grounding과 provenance
uv run pytest tests/test_m10_7_team_ontology_activation.py
uv run pytest tests/test_m10_8_a_ontology_provenance.py

# holdings/security runtime 회귀
uv run pytest tests/test_m10_9_c2_holdings.py

# legacy 280-column mapping/sample/SPARQL 검증
uv run python scripts/validate_ontology.py
```

상세 audit와 module별 triple inventory는 [docs/modularization.md](docs/modularization.md)에 정리되어 있다.

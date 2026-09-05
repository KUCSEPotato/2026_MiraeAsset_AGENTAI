# Financial Product Ontology

2026 미래에셋증권 AI Festival 금융상품 Agent 과제를 위해 설계한 금융상품 Ontology입니다.
서로 다른 금융상품 데이터를 하나의 통합 테이블로 평탄화하지 않고, 공통 금융 개념과 상품군별 특수 개념을 분리한 뒤 의미적으로 연결하는 구조를 사용합니다.

## 1. Repository Structure

```text
repo/
├── src/
│   └── validate_ontology.py
├── ontology/
│   ├── common.ttl
│   ├── bond_kr.ttl
│   ├── etf_kr.ttl
│   ├── etf_gl.ttl
│   └── fund_pub.ttl
├── requirements.txt
└── README.md
```

## 2. Ontology Files

| File | 주요 내용 |
|---|---|
| `common.ttl` | 금융상품 공통 상위 클래스, 공통 Entity, Relation, Datatype Property, 식별자/위험등급/출처 관련 vocabulary |
| `bond_kr.ttl` | 국내채권, 채권 유형, 신용등급, 이자 지급/금리 유형, 판매 회차와 거래 채널 등 채권 특화 개념 |
| `etf_kr.ttl` | 국내 ETF/ETN, ETP 공통 구조, 기초지수, 복제방식, 운용방식, 레버리지 배수 등 국내 ETP 특화 개념 |
| `etf_gl.ttl` | 해외 ETP의 상장 국가, 시장, 거래소, 거래 통화, 투자전략 설명 등 해외 상장 상품 특화 개념 |
| `fund_pub.ttl` | 공모펀드와 펀드 클래스 구조, 펀드 분류, 설정 유형, 투자자 유형, 판매 채널, 수탁사 등 공모펀드 특화 개념 |

## 3. Source Dataset Scope

본 Ontology는 금융상품 Agent가 다루는 상품군별 원천 데이터의 의미 차이를 보존하는 것을 목표로 합니다.

| Source Scope | Ontology File | Modeling Grain | 설명 |
|---|---|---|---|
| 국내채권 | `bond_kr.ttl` | `Bond`, `SaleLot` | 채권 자체의 조건과 판매 회차별 조건을 분리합니다. 같은 채권이라도 회차, 거래 방식, 판매 채널이 달라질 수 있으므로 `SaleLot`을 별도 개념으로 둡니다. |
| 국내 ETF/ETN | `etf_kr.ttl` | `ETF`, `ETN`, `ExchangeTradedProduct` | 거래소 상장 상품이라는 공통 구조를 `ExchangeTradedProduct`로 묶고, ETF와 ETN의 법적/상품적 차이는 별도 class로 구분합니다. |
| 해외 ETF/ETN | `etf_gl.ttl` | `ExchangeTradedProduct` + 해외 상장 정보 | 해외 상장 상품은 거래소, 시장, 국가, 통화, 투자전략 설명이 중요하므로 국내 ETP 공통 구조 위에 해외 listing 정보를 확장합니다. |
| 공모펀드 | `fund_pub.ttl` | `Fund`, `FundShareClass` | 하나의 펀드 family 아래에 수수료, 판매 채널, 가입 상태가 다른 class가 존재할 수 있으므로 family와 share class를 분리합니다. |

이 구조는 원천 데이터를 단순히 컬럼 단위로 합치는 대신, 실제 금융상품이 가진 식별 단위와 질의응답에 필요한 의미 단위를 함께 표현합니다.

## 4. Core Model

전체 Ontology의 중심 상품 구조는 다음과 같습니다.

```text
FinancialProduct
├── DebtSecurity
│   ├── Bond
│   └── ETN
├── ExchangeTradedProduct
│   ├── ETF
│   └── ETN
└── FundProduct
    ├── ETF
    └── Fund
        └── FundShareClass
```

주요 공통 Entity에는 `Organization`, `AssetManagementCompany`, `SecuritiesCompany`, `Country`, `Currency`, `RiskGrade`, `AssetClass`, `Identifier`, `IdentifierScheme`, `Index`, `SourceDataset`, `SourceRecord` 등이 포함됩니다.

대표 Relation은 다음과 같습니다.

- `managedBy`: 금융상품과 운용사/운용 조직 연결
- `issuedBy`: 채권 또는 ETN과 발행 조직 연결
- `hasIdentifier`: 상품 또는 의미 엔터티와 식별자 연결
- `hasRiskGrade`: 금융상품과 위험등급 연결
- `denominatedIn`: 금융상품과 표시통화 연결
- `hasUnderlyingIndex`, `tracksIndex`: ETP와 기초지수 연결
- `hasSaleLot`: 채권과 판매 회차 연결
- `hasShareClass`: 공모펀드와 펀드 클래스 연결
- `hasBenchmark`: 펀드와 벤치마크 지수 연결

`ETF`는 거래소에서 매매되는 상품이면서 펀드 성격을 가지므로 `ExchangeTradedProduct`와 `FundProduct`의 하위 class로 모델링합니다.
`ETN`은 거래소에서 매매되는 상품이면서 발행사의 신용위험이 중요한 채무증권 성격을 가지므로 `ExchangeTradedProduct`와 `DebtSecurity`의 하위 class로 모델링합니다.
이 다중 상속 구조를 통해 "상장 상품", "펀드형 상품", "채무증권형 상품" 관점의 질의를 모두 지원할 수 있습니다.

## 5. Modeling Decisions

| 결정 | 이유 |
|---|---|
| 공통 개념은 `common.ttl`에 두고 상품군별 확장은 별도 파일에 둠 | 공통 class/property 재사용성을 높이고, 특정 상품군의 vocabulary가 다른 상품군에 불필요하게 섞이지 않도록 하기 위함 |
| 채권 판매 정보를 `SaleLot`으로 분리 | 채권 본체와 판매 회차의 조건이 다를 수 있어, 채권을 "상품"으로 보는 질의와 "판매 가능 조건"을 보는 질의를 구분하기 위함 |
| 공모펀드를 `Fund`와 `FundShareClass`로 분리 | 동일 fund family 안에서도 class별 보수, 판매 채널, 가입 상태가 다를 수 있기 때문 |
| ETF와 ETN을 모두 `ExchangeTradedProduct` 아래에 둠 | 국내/해외 상장 상품의 공통 속성인 기초지수, 상장 시장, 거래 통화 등을 일관되게 표현하기 위함 |
| `SourceDataset`, `SourceRecord`, `SourceFieldAssertion`을 둠 | 답변 생성 시 어떤 원천 데이터와 필드에 근거했는지 추적할 수 있게 하기 위함 |
| FIBO를 직접 import하지 않음 | 과제 범위에 필요한 금융상품 의미 계층만 경량으로 유지하고, 제출 파일의 독립 실행성을 높이기 위함 |

## 6. Competency Questions

이 Ontology는 다음과 같은 Agent 질의를 의미적으로 해석하고 필요한 상품군/관계/속성으로 연결하기 위해 설계되었습니다.

- 국내 ETF 중 수익률이 높은 상품을 찾을 수 있는가?
- 특정 ETF 또는 ETN이 어떤 기초지수를 추종하는지 설명할 수 있는가?
- 같은 `ExchangeTradedProduct`라도 ETF와 ETN을 구분할 수 있는가?
- 채권의 신용등급, 만기일, 이자 지급 방식, 발행사 정보를 함께 표현할 수 있는가?
- 채권 상품 자체와 판매 회차별 거래 채널 또는 거래 유형을 구분할 수 있는가?
- 공모펀드 family와 share class의 관계를 표현할 수 있는가?
- 공모펀드 class별 판매 채널, 수수료 유형, 가입 상태를 구분할 수 있는가?
- 해외 상장 ETP의 상장 국가, 시장, 거래소, 거래 통화를 표현할 수 있는가?
- 상품명, 식별자, 위험등급, 통화, 운용사 등 공통 정보를 상품군과 무관하게 조회할 수 있는가?
- Agent 답변에 사용된 원천 데이터셋과 source record를 추적할 수 있는가?

## 7. Design Principles

- 공통 금융 개념과 상품군별 특수 개념을 분리하여 중복을 줄입니다.
- 상품군별 원천 데이터의 서로 다른 grain을 보존한 상태에서 의미적으로 통합합니다.
- Class, ObjectProperty, DatatypeProperty의 역할을 구분하여 금융상품 간 관계와 속성을 명시적으로 표현합니다.
- `rdfs:label`, `skos:prefLabel`, `skos:altLabel`을 사용해 한국어/영어 명칭과 주요 별칭을 표현합니다.
- 상품 데이터의 출처와 검증 가능성을 유지하기 위해 `SourceDataset`, `SourceRecord`, `SourceFieldAssertion` 계층을 둡니다.

본 Ontology는 FIBO의 금융 도메인 모델링 방식을 참고하되, 과제 데이터와 질의응답 Agent에 필요한 범위에 맞추어 경량화하여 설계했습니다. FIBO를 직접 import하는 구조는 아닙니다.

## 8. Namespace

```turtle
@prefix fin: <https://miraeasset.com/ontology/financial-product#> .
```

모든 프로젝트 고유 클래스와 속성은 위 namespace를 사용합니다.

## 9. Installation

Python 환경에서 다음 명령어로 필요한 패키지를 설치합니다.

```bash
pip install -r requirements.txt
```

현재 제출본의 검증 스크립트는 RDF/Turtle 파싱을 위해 `rdflib`을 사용합니다.

## 10. Validation

저장소 루트에서 다음 명령어를 실행합니다.

```bash
python src/validate_ontology.py
```

검증 스크립트는 다음 항목을 확인합니다.

1. 5개 `.ttl` 파일 존재 여부
2. Turtle 문법 파싱 가능 여부
3. 주요 공통/상품군별 클래스와 속성 존재 여부
4. `fin:` namespace 내부의 정의되지 않은 참조 여부
5. `rdfs:subClassOf`, `rdfs:domain`, `rdfs:range`의 내부 Class 참조 정합성

정상적인 경우 마지막에 다음 메시지가 출력됩니다.

```text
[PASS] 5개 Turtle 파일의 파싱 및 기본 구조 검증이 완료되었습니다.
```

현재 제출본 기준 검증 결과 예시는 다음과 같습니다.

```text
[Ontology validation summary]
- common.ttl: 827 triples
- bond_kr.ttl: 231 triples
- etf_kr.ttl: 81 triples
- etf_gl.ttl: 23 triples
- fund_pub.ttl: 114 triples
- merged: 1276 triples
- classes: 63
- object properties: 55
- datatype properties: 36

[PASS] 5개 Turtle 파일의 파싱 및 기본 구조 검증이 완료되었습니다.
```

## 11. Usage Note

각 상품군 Ontology는 `common.ttl`의 공통 개념을 전제로 설계되어 있으므로, 전체 금융상품 Ontology를 사용할 때는 5개의 Turtle 파일을 함께 로드하는 것을 권장합니다.

이 저장소의 TTL 파일은 상품 개념, 관계, 속성 및 canonical vocabulary를 정의하는 Semantic Layer가 중심이며, 전체 금융상품 인스턴스 데이터 자체를 포함하는 Knowledge Graph dump는 아닙니다.

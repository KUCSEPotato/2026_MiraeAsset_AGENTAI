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

## 3. Core Model

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

## 4. Design Principles

- 공통 금융 개념과 상품군별 특수 개념을 분리하여 중복을 줄입니다.
- 상품군별 원천 데이터의 서로 다른 grain을 보존한 상태에서 의미적으로 통합합니다.
- Class, ObjectProperty, DatatypeProperty의 역할을 구분하여 금융상품 간 관계와 속성을 명시적으로 표현합니다.
- `rdfs:label`, `skos:prefLabel`, `skos:altLabel`을 사용해 한국어/영어 명칭과 주요 별칭을 표현합니다.
- 상품 데이터의 출처와 검증 가능성을 유지하기 위해 `SourceDataset`, `SourceRecord`, `SourceFieldAssertion` 계층을 둡니다.

본 Ontology는 FIBO의 금융 도메인 모델링 방식을 참고하되, 과제 데이터와 질의응답 Agent에 필요한 범위에 맞추어 경량화하여 설계했습니다. FIBO를 직접 import하는 구조는 아닙니다.

## 5. Namespace

```turtle
@prefix fin: <https://miraeasset.com/ontology/financial-product#> .
```

모든 프로젝트 고유 클래스와 속성은 위 namespace를 사용합니다.

## 6. Installation

Python 환경에서 다음 명령어로 필요한 패키지를 설치합니다.

```bash
pip install -r requirements.txt
```

현재 제출본의 검증 스크립트는 RDF/Turtle 파싱을 위해 `rdflib`을 사용합니다.

## 7. Validation

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

## 8. Usage Note

각 상품군 Ontology는 `common.ttl`의 공통 개념을 전제로 설계되어 있으므로, 전체 금융상품 Ontology를 사용할 때는 5개의 Turtle 파일을 함께 로드하는 것을 권장합니다.

이 저장소의 TTL 파일은 상품 개념, 관계, 속성 및 canonical vocabulary를 정의하는 Semantic Layer가 중심이며, 전체 금융상품 인스턴스 데이터 자체를 포함하는 Knowledge Graph dump는 아닙니다.

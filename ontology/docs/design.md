# 온톨로지 설계

## 목적과 경계

이 온톨로지는 관계와 의미를 정의하는 schema vocabulary다. 원본 Excel 전체를 RDF instance store로 복제하거나 OWL 추론으로 금융 추천 순위를 계산하지 않는다. 수치 필터와 정렬의 authoritative store는 기존 canonical RDB이며, RDF/Neo4j는 관계 탐색과 grounding을 담당한다.

## 상품 계층

```text
FinancialProduct
├─ DebtSecurity
│  ├─ Bond
│  └─ ETN
├─ FundProduct
│  ├─ ETF
│  ├─ FundPortfolio
│  └─ FundShareClass
└─ ExchangeTradedProduct
   ├─ ETF
   └─ ETN
```

ETF는 `FundProduct`와 `ExchangeTradedProduct`, ETN은 `DebtSecurity`와 `ExchangeTradedProduct`의 하위 클래스다. 실제 데이터의 `pd_grp_no`가 두 유형을 구분하므로 `ETF owl:disjointWith ETN`을 적용한다. 공모/사모는 `OfferingType`이며 상품 클래스가 아니다.

최신 채권 데이터는 한 상품에 여러 판매 행이 있으므로 `BondOfferLot`을 분리한다. 최신 펀드 데이터는 한 행이 판매 클래스 성격이지만 포트폴리오 동일성의 확정 키가 없으므로 `FundShareClass` 인스턴스는 만들되 이름 기반 `FundPortfolio` 병합은 하지 않는다.

## 식별자와 동일상품 후보

`internalProductID`는 source namespace와 검증된 source key로 생성한다. 외부 식별자는 `ProductIdentifier`에 type, value, namespace, source primary 여부, validation status를 각각 둔다. 형식이 ISIN과 같아도 체크디지트 검증을 수행하지 않았다면 `FORMAT_ONLY`다. 이름 일치나 불완전한 식별자 일치는 `owl:sameAs`가 아니라 `SameProductCandidate`로 표현한다.

## 원본과 provenance

`SourceDataset → SourceRecord → describesProduct → FinancialProduct` 경로로 원본 행과 상품을 분리한다. `SourceRecord`는 원본 복합키, Excel 행 번호, 배포 snapshot을 가진다. 정규화되지 않은 모든 값은 `SourceFieldAssertion`으로 원본 칼럼명·원본 값·품질 상태를 보존할 수 있다.

## 관측값

가격, NAV, AUM, 수익률, 수익률/금리, 변동성, 유동성, 보수, 분배, 신용등급, 자산구성은 `MetricObservation` 계층으로 표현한다. 관측값에는 metric type, decimal value, 단위/통화, 실제 기준일, source column, source record를 연결한다. 실제 기준일이 없으면 `asOfDate`를 만들지 않는다.

## SHACL 정책

상품은 내부 ID, 이름, 하나 이상의 식별자와 원본 레코드를 요구한다. 식별자는 빈 값, namespace 누락, 허용하지 않은 type/status를 거부한다. ETF/ETN 동시 타입을 거부하며, ISIN은 형식 패턴을 검증한다. 식별자 충돌 SPARQL constraint는 동일 namespace/type/value가 서로 다른 상품에 연결되는 경우를 탐지한다.

스키마 Nullable이 NO더라도 실데이터가 위반하는 필드는 SHACL 필수로 올리지 않는다. 이는 데이터 오류를 숨기는 것이 아니라 ingestion quality report에서 별도로 차단해야 할 source contract violation이다.

## Agent 연결

`app.ontology.OntologyLoader`가 5개 mandatory TTL을 한 그래프로 파싱하고 canonical RDB field allow-list를 검사한다. `RDFOntologyService`는 alias를 canonical concept/field/relation으로 grounding한다. 기존 `GraphMappingRegistry`는 object property의 domain/range 호환성을 시작 시 검증한다.

다음 단계는 전체 280개 매핑을 ingestion transform registry로 옮겨 `SourceRecord`, `ProductIdentifier`, `MetricObservation`을 canonical RDB/Neo4j에 실제 생성하고, 관측 기준일·통화가 일치하는 지표만 planner의 filter/sort allow-list에 단계적으로 추가하는 것이다.

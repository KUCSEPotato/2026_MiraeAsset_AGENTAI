# 금융상품 지식 모델링 보고서

## 1. Canonical Data Model

본 프로젝트의 금융상품 지식 모델은 `common.ttl`, `etf_kr.ttl`, `etf_gl.ttl`, `fund_pub.ttl`, `bond_kr.ttl` 5개 TTL을 최종 온톨로지 기준선으로 사용한다. 온톨로지 URI는 `https://miraeasset.com/ontology/financial-product`이며, 버전은 `merged-optical-1.4`이다.

Canonical Data Model의 핵심 원칙은 그래프와 RDB의 역할을 분리하는 것이다. 상품, 기관, 식별자, 지수, 분류 체계, 판매 단위, 편입 종목과 같은 안정적인 의미 관계는 온톨로지와 그래프에서 관리한다. 반면 가격, NAV, 수익률, AUM, 보수, 거래량, 듀레이션처럼 자주 변하거나 기준일이 중요한 값은 RDB의 canonical fact/metric 계층에서 관리한다.

이 구조는 금융상품 질의응답에서 필요한 두 가지 요구를 동시에 만족한다. 첫째, “어떤 상품이 어떤 운용사와 지수, 통화, 위험등급, 판매 클래스, 채권 판매 LOT와 연결되는가”는 그래프 관계로 탐색할 수 있다. 둘째, “특정 기준일의 수익률, 가격, 잔존만기, 평가단가가 얼마인가”는 RDB에서 단위, 기간, 관측일, provenance와 함께 검증한다.

최종 TTL 모듈 구성은 다음과 같다.

| 모듈 | 역할 |
|---|---|
| `common.ttl` | 공통 상위 클래스, 식별자, 기관, 증권, 분류 개념, 지표 family, provenance, answerability constraint, 공통 SHACL |
| `etf_kr.ttl` | ETF/ETN/ETP 계층, 지수 추종 관계, ETF/ETN 배타성 및 역할 제약 |
| `etf_gl.ttl` | 글로벌 ETP의 상장 국가, 시장, 거래소, 거래 통화, 투자전략 설명 |
| `fund_pub.ttl` | 공모펀드와 판매 클래스 분리, 벤치마크, 수탁사, 판매채널, 가입 상태 |
| `bond_kr.ttl` | 국내 채권, 채권 종류, 신용등급, 금리/이자 유형, 판매 LOT, 거래채널 |

## 2. 금융상품 Ontology 설계

온톨로지는 금융상품 도메인을 하나의 거대한 상품 테이블로 다루지 않고, 상품 유형별 grain을 명시적으로 분리한다. 최상위에는 `fin:SemanticEntity`가 있으며, 그 아래에 증거를 가질 수 있는 `fin:EvidenceBearingEntity`, 실제 상품을 표현하는 `fin:FinancialProduct`, 기관을 표현하는 `fin:Organization`, 분류/코드 체계를 표현하는 `fin:ClassificationConcept`가 배치된다.

상품 계층은 다음 구조를 갖는다.

```text
FinancialProduct
  ├─ DebtSecurity
  │   ├─ Bond
  │   └─ ETN
  └─ FundProduct
      ├─ ETF
      └─ Fund

ExchangeTradedProduct
  ├─ ETF
  └─ ETN
```

`fin:ETF`는 `fin:FundProduct`이면서 `fin:ExchangeTradedProduct`이고, `fin:ETN`은 `fin:DebtSecurity`이면서 `fin:ExchangeTradedProduct`이다. 두 클래스는 `owl:disjointWith` 및 SHACL 제약을 통해 서로 동시에 성립하지 않도록 설계되었다. 즉 ETF의 운용사 관계(`managedBy`)와 ETN/채권의 발행사 관계(`issuedBy`)를 섞지 않는다.

공모펀드는 `fin:Fund`와 `fin:FundShareClass`를 분리한다. 펀드 자체는 운용전략, 벤치마크, 수탁사, 설정 국가, 펀드 분류를 갖고, 판매 클래스는 판매 여부, 보수 유형, 가입 상태, 판매채널처럼 판매 단위에서 달라지는 속성을 갖는다.

국내 채권은 `fin:Bond`와 `fin:SaleLot`를 분리한다. 하나의 채권 상품은 발행일, 만기/콜일, 총발행금액, 채권 종류, 신용등급 등 상품 본체 속성을 갖고, 판매 LOT는 실제 판매 조건, 거래채널, 매매 유형 등 source row 단위의 판매 조건을 담는다. `hasSaleLot`의 부재는 매수 불가를 뜻하지 않도록 주석으로 명확히 제한되어 있다.

## 3. 주요 Entity

| Entity | 설명 |
|---|---|
| `fin:FinancialProduct` | 모든 금융상품의 공통 상위 클래스. 내부 상품 ID, 상품명, 식별자, source record를 필수 grounding 요소로 갖는다. |
| `fin:ETF` | 상장지수펀드. `FundProduct`와 `ExchangeTradedProduct`의 성격을 모두 갖고, 운용사와 지수 노출 관계를 표현한다. |
| `fin:ETN` | 상장지수증권. `DebtSecurity`와 `ExchangeTradedProduct`의 성격을 모두 갖고, ETF 운용/복제 속성과 분리된다. |
| `fin:Fund` | 공모펀드의 포트폴리오/상품 본체. 판매 클래스와 별도 entity로 관리한다. |
| `fin:FundShareClass` | 펀드 판매 클래스. 판매 가능 여부, 가입 상태, 판매채널, 보수 유형이 연결되는 grain이다. |
| `fin:Bond` | 국내 채권 상품 본체. 발행일, 만기일, 총발행금액, 채권 종류, 신용등급 등을 갖는다. |
| `fin:SaleLot` | 채권 판매 조건 LOT. 하나의 채권에 여러 LOT가 연결될 수 있다. |
| `fin:Security` / `fin:EquitySecurity` | ETF holdings 등의 편입 증권. 상품이 직접 기관을 보유하는 오류를 막기 위해 별도 증권 entity로 분리한다. |
| `fin:Organization` | 운용사, 발행사, 수탁사, 증권사 등 기관의 공통 상위 클래스. |
| `fin:Index` | ETF 추종 지수 또는 펀드 벤치마크. |
| `fin:Identifier` / `fin:IdentifierScheme` | ISIN, ticker, KSD ID, FSS ID, RIC 등 식별자와 그 체계. |
| `fin:SourceDataset` / `fin:SourceRecord` / `fin:SourceFieldAssertion` | 원천 데이터셋, 원천 행, 원천 필드 단위 증거를 표현하는 provenance entity. |

분류 entity는 `AssetClass`, `ExposureRegion`, `RiskGrade`, `MarketScope`, `OfferingType`, `BondType`, `CreditRating`, `InterestRateType`, `InterestPaymentType`, `TradingType`, `TradingChannel`, `ManagementStyle`, `ReplicationMethod`, `SubscriptionStatus` 등으로 구성된다. 이들은 문자열 값을 직접 비교하기보다 controlled vocabulary로 정규화하기 위한 의미 계층이다.

## 4. 주요 Relation

| Relation | Domain → Range | 의미 |
|---|---|---|
| `fin:managedBy` | FinancialProduct → Organization | ETF/Fund가 운용사와 연결되는 관계 |
| `fin:issuedBy` | FinancialProduct → Organization | ETN/Bond 발행사 역할 |
| `fin:securityIssuedBy` | Security → Organization | 편입 증권 자체의 발행사 관계 |
| `fin:holds` | FinancialProduct → Security | ETF/Fund 등이 특정 증권을 보유하는 관계 |
| `fin:hasUnderlyingIndex` | ExchangeTradedProduct → Index | ETP의 기초/추종 지수 |
| `fin:tracksIndex` | ExchangeTradedProduct → Index | 명시적 추종 근거가 있을 때만 assert되는 지수 추종 관계 |
| `fin:hasBenchmark` | Fund → Index | 공모펀드의 벤치마크 |
| `fin:hasShareClass` | Fund → FundShareClass | 펀드 본체와 판매 클래스 연결 |
| `fin:hasSaleLot` | Bond → SaleLot | 채권 본체와 판매 조건 LOT 연결 |
| `fin:denominatedIn` | FinancialProduct → Currency | 상품 표시 통화 |
| `fin:tradedInCurrency` | ExchangeTradedProduct → Currency | ETP 거래 통화 |
| `fin:listedInCountry` / `fin:listedOnExchange` | ExchangeTradedProduct → Country/Exchange | 글로벌 ETP 상장 국가와 거래소 |
| `fin:hasAssetClass` | FinancialProduct → AssetClass | 상품 자산군 |
| `fin:hasExposureRegion` | FinancialProduct → ExposureRegion | 투자 노출 지역 |
| `fin:hasRiskGrade` | FinancialProduct → RiskGrade | 위험등급 |
| `fin:hasOfferingType` | FundShareClass/Bond → OfferingType | 공모/사모 등 모집 구분 |
| `fin:hasBondType` | Bond → BondType | 채권 종류 |
| `fin:hasCreditRating` | DebtSecurity → CreditRating | 채권/채무증권 신용등급 |
| `fin:availableThroughSalesChannel` | FundShareClass → SalesChannel | 펀드 판매 채널 |
| `fin:availableThroughTradingChannel` | SaleLot → TradingChannel | 채권 판매 LOT의 거래 가능 채널 |

관계 모델링에서 중요한 점은 역할을 엄격히 분리한 것이다. 예를 들어 ETF 운용사는 `managedBy`, 채권 발행사는 `issuedBy`, 편입 증권 발행사는 `securityIssuedBy`로 구분된다. 또한 `holds`는 `FinancialProduct → Security`로만 허용되어, 상품이 기관을 직접 보유하는 잘못된 그래프 생성을 방지한다.

## 5. 금융 지표 및 속성 모델링

최종 온톨로지는 변동성이 높은 수치 관측값을 RDF node로 대량 materialize하지 않는다는 원칙을 명시한다. 가격, NAV, 성과, 수익률, 보수, 유동성, 규모, 분배, 변동성, 채권 평가 지표 등은 `fin:MetricFamily`로 semantic catalog를 제공하고, 실제 값은 RDB에 저장한다.

주요 지표 family는 다음과 같다.

| Metric Family | 대표 semantic key |
|---|---|
| `fin:PriceMetric` | `price.open`, `price.high`, `price.low`, `price.close`, `price.reference` |
| `fin:NAVMetric` | `nav.perShare`, `nav.reference`, `nav.yesterday` |
| `fin:PerformanceMetric` | `return.1D`, `return.1W`, `return.1M`, `return.3M`, `return.6M`, `return.1Y`, `return.3Y`, `return.5Y`, `return.YTD` |
| `fin:YieldMetric` | `buyYield`, `afterTaxYield`, `corporateYield` |
| `fin:SizeMetric` | `aum`, `netAssets`, `outstandingAmount` |
| `fin:CostMetric` | `fee`, `expenseRate` |
| `fin:LiquidityMetric` | `volume`, `tradingValue` |
| `fin:VolatilityMetric` | `volatility.1M`, `volatility.3M`, `volatility.6M`, `volatility.1Y` |
| `fin:DistributionMetric` | `distributionAmount`, `distributionYield`, `lastDistributionRate` |
| `fin:FixedIncomePortfolioMetric` | `duration`, `avgCoupon`, `avgMaturity`, `averageCreditQualityScore` |
| `fin:ValuationMetric` | `evaluatedPrice`, `bondClosePrice` |
| `fin:SaleMetric` | `tradePrice`, `buyableQuantity` |

모든 `MetricFamily`의 `primaryStore`는 `fin:RDBStore`로 지정된다. 따라서 사용자가 수익률, 가격, 규모, 위험 지표를 묻는 경우 그래프는 상품과 지표의 의미 grounding에 사용되고, 값 조회와 비교/정렬은 RDB의 기준일, 단위, 통화, 비율 scale 검증을 통과한 데이터만 사용한다.

상품의 비교적 안정적인 속성은 온톨로지 관계 또는 datatype property로 유지한다. 예를 들어 `Bond`의 `issueDate`, `maturityOrFirstCallDate`, `totalIssueAmount`, `SaleLot`의 `lotSequence`, ETP의 `leverageFactor`, 글로벌 ETP의 `investmentStrategyDescription`, 상품 공통의 `productName`, `shortName`, `englishName`, `internalProductID`가 여기에 해당한다.

## 6. Entity Resolution 및 Relation Construction

Entity Resolution은 질의 또는 원천 데이터의 문자열을 canonical entity로 연결하는 단계이다. 최종 온톨로지는 이를 위해 식별자 체계와 검증 상태를 명시한다. `Identifier`는 `identifierScheme`, `identifierValue`, `identifierNamespace`, `validationStatus`, `isPrimaryInSource`를 갖고, 상품과 기관, 증권 등 `SemanticEntity`에 `hasIdentifier`로 연결된다.

해결 규칙은 “정확히 하나의 canonical entity로만 확정”하는 방식이다. 핵심 질의 entity가 0개 후보로 매칭되면 `ENTITY_NOT_FOUND`, 여러 후보로 매칭되면 `ENTITY_AMBIGUOUS`로 처리한다. 이는 `OperationalConstraint`의 `S045`에 명시되어 있으며, 런타임 resolver도 후보가 하나일 때만 `RESOLVED`로 승격한다.

식별자 기반 resolution은 다음 순서를 따른다.

1. canonical ID, 검증된 식별자, preferred name, alias를 후보 생성에 사용한다.
2. ticker, ISIN, KSD ID, FSS ID, RIC 등은 scheme과 namespace를 함께 본다.
3. 외국 주식의 bare ticker처럼 거래소 정보가 빠진 식별자는 임의로 특정 상장시장에 결합하지 않는다.
4. 동일 scheme/namespace/value가 서로 다른 상품에 연결되는 경우 identifier collision으로 보고 확정 관계를 만들지 않는다.
5. 완전히 확정되지 않은 동일상품 가능성은 `owl:sameAs`가 아니라 `SameProductCandidate`로 남긴다.

Relation Construction은 Entity Resolution이 끝난 뒤, source mapping과 온톨로지 domain/range를 모두 통과한 관계만 그래프에 적재하는 방식이다. 예를 들어 `managedBy`는 ETF/Fund와 Organization, `hasUnderlyingIndex`는 ExchangeTradedProduct와 Index, `hasSaleLot`은 Bond와 SaleLot, `holds`는 FinancialProduct와 Security 사이에서만 생성된다. 관계 생성 시 source record와 field assertion을 보존하여 답변 단계에서 증거를 역추적할 수 있게 한다.

## 7. Data-Ontology Mapping

Data-Ontology Mapping은 원천 데이터의 `dataset + table + column`을 canonical entity, relation, metric, source assertion으로 변환하는 계약이다. 최종 구현에서는 `ontology/mappings/column_mapping.csv`와 런타임 mapping registry가 이 역할을 수행한다.

원천 컬럼은 크게 네 가지로 분류된다.

| 분류 | 처리 방식 |
|---|---|
| 상품/기관/지수 식별 컬럼 | `FinancialProduct`, `Organization`, `Index`, `Identifier` 등 canonical entity와 연결 |
| 안정적인 상품 속성 | `Bond.totalIssueAmount`, `Bond.issueDate`, `Fund.hasBenchmark`, `ETP.tradedInCurrency` 등으로 변환 |
| 기준일이 있는 관측값 | RDB metric observation/fact로 저장하고, 온톨로지의 `MetricFamily`와 semantic key로 grounding |
| 의미 확정이 어려운 코드/원문 | `SourceFieldAssertion`으로 raw value, normalized value, quality status를 보존 |

그래프 relation mapping은 allow-list 방식이다. Team ontology 기준 mapping에는 `MANAGED_BY`, `ISSUED_BY`, `TRACKS_INDEX`, `HAS_UNDERLYING_INDEX`, `HAS_SHARE_CLASS`, `HAS_BENCHMARK`, `DENOMINATED_IN`, `HAS_RISK_GRADE`, `HAS_ASSET_CLASS`, `HAS_EXPOSURE_REGION`, `HAS_MARKET_SCOPE`, `TRADED_IN_CURRENCY`, `HAS_OFFERING_TYPE`, `HAS_SALE_LOT`, `HAS_BOND_TYPE`, `HAS_INTEREST_RATE_TYPE`, `HAS_INTEREST_PAYMENT_TYPE`, `HAS_CREDIT_RATING`, `HAS_TRADING_TYPE`, `AVAILABLE_THROUGH_TRADING_CHANNEL` 등이 포함된다.

이 방식의 장점은 원천 컬럼명이 유사하다는 이유만으로 관계를 만들지 않는다는 점이다. mapping registry에 등록된 컬럼만 변환되고, 시작 시 ontology index가 object property의 domain/range 호환성을 검증한다. 따라서 데이터 적재 오류가 곧바로 그래프 의미 오류로 확산되는 것을 막을 수 있다.

## 8. 외부 데이터 및 Provenance 관리

최종 온톨로지는 모든 답변 가능한 사실이 source evidence에 grounded되어야 한다는 원칙을 갖는다. provenance 계층은 다음 구조를 중심으로 한다.

```text
SourceDataset
  └─ SourceRecord
       ├─ describesEntity / describesProduct
       ├─ hasFieldAssertion
       └─ supportsEntity
```

`SourceDataset`은 원천 데이터셋의 코드와 snapshot을 표현한다. `SourceRecord`는 원천 행 단위의 primary key, row number, snapshot date, raw payload hash를 보존한다. `SourceFieldAssertion`은 특정 source column의 raw value, normalized value, quality status를 저장하여, 정규화가 불완전하거나 코드 의미가 불명확한 값도 손실 없이 추적할 수 있게 한다.

외부 데이터는 canonical entity를 즉시 덮어쓰는 방식이 아니라 source-level evidence로 수집된다. holdings, issuer, 성과/AUM 등의 외부 수집 결과는 provider, snapshot, raw hash, parser 결과, source key를 보존한 뒤, 별도의 canonicalization 및 validation을 통과해야 그래프 relation 또는 RDB metric으로 승격된다.

답변 생성에서는 `S052` 제약에 따라 모든 factual claim이 최소 하나 이상의 evidence item에 연결되어야 한다. 서로 다른 source가 동일 entity/metric/observedAt에 대해 충돌하고 source priority 정책으로 해결할 수 없으면 `S050`에 의해 답변 생성을 중단한다.

## 9. Ontology Constraint 및 정합성 검증

정합성 검증은 SHACL shape와 운영 제약(`OperationalConstraint`)으로 나뉜다.

공통 SHACL은 다음을 검증한다.

| Shape | 검증 내용 |
|---|---|
| `FinancialProductShape` | 상품은 `internalProductID`, `productName`, 하나 이상의 `Identifier`, 하나 이상의 `SourceRecord`를 가져야 한다. |
| `IdentifierShape` | 식별자는 scheme, value, namespace, validationStatus를 정확히 가져야 한다. |
| `ISINIdentifierShape` | ISIN 식별자는 `^[A-Z]{2}[A-Z0-9]{9}[0-9]$` 패턴을 만족해야 한다. |
| `IdentifierCollisionShape` | 동일 scheme/namespace/value가 서로 다른 상품에 연결되는 충돌을 탐지한다. |
| `SecurityShape` | canonical Security는 authoritative identifier evidence를 가져야 하며, issuer가 있으면 Organization이어야 한다. |
| `HoldsRelationShape` | `holds`는 FinancialProduct에서 Security로만 연결되어야 하며 Organization으로 직접 연결할 수 없다. |
| `SecurityIssuerRelationShape` | `securityIssuedBy`는 Security에서 Organization으로만 연결되어야 한다. |
| `SourceRecordShape` | source record는 dataset, primary key, describesEntity를 가져야 한다. |

상품군별 SHACL은 ETF/ETN, FundShareClass, SaleLot의 grain 오류를 방지한다. `ETFShape`는 ETF에 `issuedBy`를 사용하지 못하게 하고, `ETNShape`는 ETN에 ETF 전용 운용/복제 속성이나 `managedBy`가 들어가지 않도록 한다. `FundShareClassShape`는 판매 클래스가 정확히 하나의 parent Fund에 연결되도록 하며, `SaleLotShape`는 판매 LOT가 정확히 하나의 Bond에 속하도록 검증한다.

운영 제약은 answerability gate에서 사용된다.

| Constraint | 의미 |
|---|---|
| `S045` | 핵심 entity는 정확히 하나로 resolve되어야 한다. |
| `S046` | 정의되지 않은 metric은 유사 metric으로 대체하거나 임의 계산하지 않는다. |
| `S047` | 요청일/기간을 커버하는 observedAt이 없으면 snapshot/update date로 대체하지 않는다. |
| `S048` | NULL, zero, N/A, not-provided를 서로 구분한다. |
| `S049` | 사용자 필터를 만족하는 후보가 없으면 조건을 자동 완화하지 않는다. |
| `S050` | evidence 충돌이 해결되지 않으면 답변 생성을 중단한다. |
| `S051` | retrieval failure와 data absence를 구분한다. |
| `S052` | 모든 factual claim은 evidence item에 연결되어야 한다. |
| `S053` | multi-slot 질의는 근거가 있는 slot만 답하고 누락 slot을 명시한다. |
| `S054` | 단위, 기간, ratio scale이 불명확하면 정규화/비교/랭킹/파생 계산을 금지한다. |

결론적으로 최종 온톨로지는 상품 지식 그래프를 단순한 관계 저장소가 아니라, 금융상품 QA의 의미 grounding, entity resolution, metric answerability, provenance validation을 함께 제어하는 계약으로 설계한다. 그래프는 안정적인 의미 관계를 제공하고, RDB는 수치 fact의 기준일과 단위를 보장하며, provenance와 constraint 계층은 답변이 실제 source evidence에 근거하도록 통제한다.

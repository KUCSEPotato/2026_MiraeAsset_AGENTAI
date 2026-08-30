# Crawl-2A Trusted ETF Holdings Source Contract Report

조사일은 2026-08-30이며, 평가용 전역 데이터 기준일은 주최 측 최신 지침에 따라 **2026-08-24**이다. 이 문서는 공개 페이지와 공개 응답을 소량 조회한 source-contract 조사 결과다. ETF뿐 아니라 공모펀드 holdings, 성과/AUM 및 sector/theme source contract를 포함한다. parser, crawler adapter, canonical 연동은 구현하지 않았다.

## 1. Mirae Asset Securities Audit

### Product Identity

미래에셋증권 공개 웹사이트에서는 국내 ETF 전 종목 시세 화면과 ETF 주문 화면 안내를 확인했다. 전 종목 화면은 운용사·테마·거래량·괴리율 필터와 종목코드를 제공한다고 설명하고, 주문 화면은 선택 ETF의 구성종목을 보여준다고 설명한다. 그러나 조사 범위에서 로그인 없이 재현 가능한 대량 상품 목록/상세 JSON·CSV 계약은 발견하지 못했다.

미래에셋증권이 직접 발행한 ETN 상세 페이지는 거래소, 6자리 종목번호, 발행일, 만기일과 기초지수를 명시한다. 이는 해당 ETN의 발행사 정보에는 fact authority가 있지만, 제3자 운용 ETF의 portfolio authority는 아니다.

판정:

- 상품명: 화면 단위 제공, 대량 수집 계약 미확정
- ticker/종목코드: 국내 ETF 화면에서 제공된다고 명시, 공개 bulk 계약 미확정
- ISIN: 안정적인 공개 ETF 계약을 확인하지 못함
- exchange/market: 일부 상품 상세에서 확인 가능
- provider-specific product ID: ETN의 `S_CD`는 확인했으나 ETF 전반 계약은 미확정
- market availability at Mirae Securities: 미래에셋증권이 적절한 authority일 수 있으나 이번 holdings milestone의 사실은 아님

### Numeric Product Information

ETF 전 종목 화면은 현재가, 거래량, 괴리율을 제공한다고 설명하지만 각 필드의 공개 응답, 통화, 기준시각, bulk pagination을 확정하지 못했다. 따라서 수집 승인 대상이 아니다.

미래에셋증권 발행 ETN 상세에는 다음 의미가 명시돼 있다.

- 일별 지표가치: 전일 종가 기준으로 한국예탁결제원이 산출한 공식 평가가치
- 현재가: 조회 시점 시세
- 실시간 지표가치: 전일 종가 지표가치에 당일 시세 움직임을 반영
- 연간 운용보수: `%` 단위가 화면에 명시

이 값들은 ETN에는 유용하지만 ETF holdings를 대신하지 않는다. 또한 현재가를 2026-08-24 관측값으로 소급할 수 없다.

### ETF Holdings

미래에셋증권 HTS 안내는 ETF 주문 화면에서 구성종목을 볼 수 있다고 명시한다. 그러나 HTS 화면은 자동화 가능한 공개 source contract가 아니고, 구성종목의 ticker/ISIN/비중/수량/평가금액/portfolio date가 공개 웹 응답으로 확인되지 않았다.

결론: 미래에셋증권은 현재 ETF holdings 수집 provider로 승인하지 않는다. “화면에 보인다”와 “재현 가능한 공개 취득 계약이 있다”를 구분한다.

### Risk / Strategy Information

미래에셋증권은 상품별 투자설명서 PDF와 ETN 위험요소를 공개한다. 자사가 발행한 ETN 위험·전략 정보에는 높은 authority가 있다. 제3자 ETF의 고유 위험은 판매사 페이지보다 운용사 투자설명서·공시를 우선한다. 리서치 보고서의 구성종목 표는 작성 기준일이 있는 보조 문서로 보존할 수 있지만 공식 portfolio disclosure를 대체하지 않는다.

### Temporal Information

- ETN 상세: `상품정보(YYYY/MM/DD 기준)`과 발행일·만기일이 명시됨
- 공지/리서치: 작성일 또는 보고서 기준일이 있는 경우가 있음
- ETF holdings: 공개적으로 재현 가능한 portfolio/effective date 계약 미확정
- `retrieved_at`은 어느 경우에도 `published_at`이나 `effective_date`로 대체하지 않음

### Access / robots / Technical Contract

- URL 계열: `https://securities.miraeasset.com/kairos/...`(HTS 안내), `.../hks/...`(상품 상세), `.../bbs/...`(공지/PDF)
- 접근: 공개 HTML/PDF가 있으나 ETF 구성종목의 실제 사용 화면은 HTS 의존
- 인증: 공개 안내·일부 상세는 불필요, HTS 실제 기능은 자동 수집 계약으로 승인하지 않음
- robots: 2026-08-30 조회 시 `User-agent: Yeti`, `Allow: /`만 존재했다. `*` 정책을 명시하지 않으므로 이를 포괄적 자동수집 허가로 확대 해석하지 않는다.
- pagination/API/schema: ETF holdings에 대해 미확정
- anti-bot: 우회 시도하지 않음
- parser risk: 공개 HTML은 중간, HTS/비공개 내부 호출은 허용하지 않음
- terms: 별도 법무/운영 검토 전 대량 수집하지 않음

## 2. Missing Capabilities After Mirae

다음 holdings 핵심 계약이 남는다.

- 운용사 전체를 아우르는 국내 ETF portfolio snapshot
- ETF와 constituent의 안정 식별자
- 비중·수량·평가금액의 단위와 계산 기준
- 2026-08-24 당일 또는 그 이전 최종 snapshot을 명시적으로 조회하는 기능
- 해외 ETF issuer별 holdings 계약과 시장별 coverage
- 과거 snapshot을 제공하지 않는 “current only” 페이지의 fail-closed 처리

현재 기준 유니버스는 국내 ETF 1,235개, 해외 ETF 5,972개, 공모펀드 판매 클래스 23,676개다. 국내 운용사 표기는 다수 기관으로 분산되어 있으므로 단일 운용사 사이트로 전체를 덮을 수 없다. 공모펀드는 portfolio와 share class를 구분해야 하며, 같은 portfolio의 여러 판매 클래스를 holdings가 다른 펀드인 것처럼 중복시키면 안 된다.

## 3. Candidate Primary Sources for Missing Holdings

### Provider A — KRX ETF Portfolio Deposit File

Organization: 한국거래소(KRX)

Fact authority: 국내 상장 ETF의 거래소 게시 PDF(Portfolio Deposit File). KRX는 PDF를 ETF 설정에 필요한 현물 바스켓 내역이라고 정의한다.

Trust tier: 1, `AUTHORITATIVE`

Market: 한국거래소 상장 ETF

Access method: KRX Data Marketplace의 “PDF(Portfolio Deposit File)” 화면. 공개 web form/다운로드이나 이 조사에서 안정적인 공식 Open API 상품으로 확인하지는 못했다.

Identifiers: ETF 종목/ISIN 검색키와 구성 종목 코드가 기대되지만 실제 응답 header 계약은 Crawl-2 착수 전 fixture probe로 확정해야 한다.

Holding fields: PDF 구성 종목과 수량이 핵심. 비중·평가금액의 실제 제공 여부와 header는 미확정이다.

Numeric contract: “PDF”는 문서 파일 형식이 아니라 Portfolio Deposit File이며 CU 설정 바스켓이다. 수량을 펀드 전체 실제 보유량으로 해석하면 안 된다.

Temporal contract: 조회일/발표일 기준 일별 자료가 목적이나 2026-08-24 자료의 실제 반환과 보존기간은 아직 검증하지 못했다.

Coverage: 내부 국내 ETF 1,235개 모두 `pd_itm_no`가 ISIN 형태이고 고유하다. 따라서 eligible 1,235, ISIN-key candidate 1,235, ticker+exchange 및 exact-name-only confirmed 0, unmatched confirmed 1,235다. 이 수치는 catalog join을 실행하지 않은 후보 coverage이지 100% confirmed coverage가 아니다.

robots/access: `robots.txt` 경로는 404였고 KRX 화면 meta는 `index,follow`였다. robots 부재를 이용 허가로 간주하지 않는다. Data Marketplace 이용약관과 호출 빈도 검토가 선행돼야 한다.

Drift risk: 중간. 구조화 다운로드가 가능하면 낮아지지만 화면 내부 POST 계약에 의존하면 변경 위험이 있다.

Limitations: ETF의 실제 전체 보유자산과 PDF 설정 바스켓은 동일 개념이 아닐 수 있다. 합성 ETF, 현금·파생, 대체바스켓은 별도 quality flag가 필요하다.

Historical snapshot available: 미확정

Closest snapshot on or before cutoff: 미확정

Effective date source: KRX 조회/발표일 필드가 확인돼야 함

Cutoff compatible: 아직 No

### Provider B — Samsung Asset Management KODEX

Organization: 삼성자산운용

Fact authority: 자사 KODEX ETF의 공식 구성종목(PDF)

Trust tier: 1, `AUTHORITATIVE`

Market: KODEX 국내 상장 ETF

Access method: 공개 상품 HTML이 사용하는 JSON GET 및 Excel download. 검증한 패턴은 `/api/v1/kodex/product-pdf/{fId}.do?gijunYMD=YYYY.MM.DD`다. 인증은 필요하지 않았다.

Identifiers: ETF `fId`는 provider source ID이고 상품 페이지에 6자리 종목코드가 있다. constituent 응답은 `itmNo`와 `secNm`을 제공한다. `itmNo`가 항상 ticker/ISIN이라는 가정은 금지한다. 예금은 `KRD010010001`처럼 별도 자산 코드다.

Holding fields: `secNm`, `itmNo`, `ratio`, `applyQ`, `evalA`, `curp`, `risep`, `totalCnt`, 응답 `gijunYMD`, `pdfExcelDownloadUrl`.

Numeric contract:

- `ratio`: 공식 페이지 설명상 국내외 현금성 자산과 예금을 제외해 계산한 비중(%). 원문과 percent-point scale을 보존한다.
- `applyQ`: 구성수량. PDF가 1CU 기준이라는 계약을 adapter metadata로 고정하기 전 상품 설명/Excel header를 재검증한다.
- `evalA`: 평가금액. 국내 예제는 원화이나 통화는 상품/column contract로 증명할 때만 KRW를 기록한다.
- `curp`: 현재가, `risep`: 가격 변화값으로 보이나 명칭만으로 의미를 확정하지 않고 초기 holdings v1에는 사용하지 않는다.

Temporal contract: 변경된 cutoff인 2026-08-24 요청은 응답 `gijunYMD=20260824`를 반환했다. adapter는 요청일이 아니라 응답 `gijunYMD`를 effective date로 사용한다. `rcvTime=2026.08.28 15:30:00`은 data transport/display timestamp이며 effective date로 쓰지 않는다.

Coverage: 내부 운용사 문자열에 `삼성`이 포함된 국내 ETF는 268개(21.70%)이고 모두 ISIN 형태 `pd_itm_no`를 가진다. 이는 eligible candidate 268, ISIN candidate 268, confirmed provider catalog match 0, unmatched pending 268이다. 브랜드/법인명이 섞여 있으므로 문자열만으로 KODEX 소유권을 확정하지 않는다.

robots/access: `User-agent: *`, `allow: /`. 인증 불필요. Cloudflare cookie가 응답에 존재하나 단일 공개 GET은 정상 동작했다. CAPTCHA나 challenge 우회는 금지한다.

Drift risk: 낮음~중간. JSON은 구조적이지만 공개 제품 UI의 내부 API로 별도 SLA가 없다. fixture/header fail-fast가 필요하다.

Limitations: `fId` catalog mapping 필요, 현금/파생 코드 분류 필요, 비중은 현금 제외 기준, 현재 응답의 `rcvTime`이 cutoff 이후여도 `gijunYMD`가 cutoff 이전이면 portfolio fact만 사용 가능하다.

Historical snapshot available: Yes

Closest snapshot on or before cutoff: 2026-08-24

Effective date source: JSON `pdf.gijunYMD`

Cutoff compatible: Yes

### Provider C — Mirae Asset Global Investments TIGER

Organization: 미래에셋자산운용

Fact authority: 자사 TIGER ETF 공식 구성종목(PDF), 상품정보, 투자설명서

Trust tier: 1, `AUTHORITATIVE`

Market: TIGER 국내 상장 ETF

Access method: 공개 HTML과 동적 AJAX/Excel. 상품 URL은 KSD fund code(`ksdFund`, ISIN)를 사용한다. 공개 페이지에 “자산구성(구성종목 PDF)”과 Excel download UI가 있다.

Identifiers: ETF `ksdFund`(ISIN), 6자리 상품코드, 구성종목 원천 코드/명칭이 예상된다. 구성종목 실제 응답 header는 아직 확정하지 않았다.

Holding fields: 공식 페이지는 구성종목 TOP10 비중과 1CU 기준 PDF임을 명시한다. 전체 Excel의 필드 계약은 미확정이다.

Numeric contract: 화면 비중은 `%`; PDF 내역은 최소 설정단위 1CU 기준. 수량·평가금액·통화는 응답 계약 확인 전 normalization하지 않는다.

Temporal contract: 상품 페이지의 기준가격에는 날짜 범위가 있고 PDF 영역은 동적이다. 2026-08-24 holdings 반환 여부를 확정하지 못했다.

Coverage: 내부 운용사 문자열에 `미래에셋`이 포함된 국내 ETF는 236개(19.11%)이고 모두 ISIN 형태 `pd_itm_no`를 가진다. eligible 236, ISIN candidate 236, confirmed catalog match 0, unmatched pending 236이다.

robots/access: 공개 페이지는 인증 없이 열리지만 robots/terms와 전체 Excel endpoint 계약은 Crawl-2 전 확인 필요. 접근 제한 우회 금지.

Drift risk: 중간. 서버 렌더 HTML + RequireJS/AJAX이며 session suffix가 나타난다.

Limitations: historical cutoff compatibility와 stable endpoint 미확정.

Historical snapshot available: 미확정

Closest snapshot on or before cutoff: 미확정

Effective date source: provider 응답에서 확정 필요

Cutoff compatible: No, 현재 계약만으로는 미검증

### Provider D — KraneShares KSTR

Organization: Krane Funds Advisors / KraneShares

Fact authority: 자사 KSTR ETF holdings와 공식 dated factsheet/presentation

Trust tier: 1, `AUTHORITATIVE`

Market: 미국 상장 KSTR, NYSE Arca

Access method: 공개 HTML, Full Holdings CSV, dated PDF factsheet/presentation

Identifiers: ETF ticker `KSTR`, ISIN `US5007676944`, CUSIP `500767694`, exchange; constituent name, ticker `688256`, ISIN `CNE1000041R8`(현재 holdings page), source row identity.

Holding fields: rank, name, percent of net assets, ticker, identifier, shares held, USD market value.

Numeric contract: weight is explicitly `% of Net Assets`; market value is explicitly USD; shares held is a count. CSV header/body를 raw artifact로 보존한다.

Temporal contract: current HTML/CSV는 현재 as-of만 제공하므로 cutoff 증거로 backdate할 수 없다. 공식 presentation은 2026-06-30 기준 Cambricon과 구성종목/비중을 명시한다. 이는 cutoff 이전 holding을 증명하지만 2026-08-24에 여전히 보유했다는 증명은 아니다. 평가 질문이 “기준일 당시 보유”를 요구하면 cutoff 시점까지 유효한 별도 snapshot이 필요하다.

Coverage: 내부 해외 ETF에서 Krane manager-name candidate 38개, 모두 ISIN과 RIC가 있다. KSTR은 RIC `KSTR.K`, ticker `KSTR`, ISIN `US5007676944`로 정확히 1개 확인된다. provider 전체 catalog join은 미실행이다.

robots/access: 공개 페이지/문서. robots와 terms, CSV stable URL은 adapter 착수 전 확인한다.

Drift risk: HTML은 중간, dated PDF는 높음, CSV는 낮음~중간.

Limitations: historical daily CSV archive가 확인되지 않았고 PDF는 Top 10만 포함할 수 있다.

Historical snapshot available: Yes, dated official documents; daily archive는 미확정

Closest snapshot on or before cutoff: 확인된 증거는 2026-06-30

Effective date source: official document의 `as of`

Cutoff compatible: Partially; “on/before cutoff evidence”에는 적합하지만 “held exactly at cutoff”에는 불충분

### Provider E — Global X Hong Kong 3191

Organization: Global X ETFs Hong Kong / Mirae Asset Global Investments group

Fact authority: 자사 3191/9191 ETF daily holdings

Trust tier: 1, `AUTHORITATIVE`

Market: Hong Kong

Access method: 공개 HTML과 Full Holdings CSV

Identifiers: ETF stock code 3191/9191; constituent raw name, exchange ticker, exchange. 현재 페이지는 Cambricon `688256 C1`, Shanghai를 제공한다.

Holding fields: security name, exchange ticker, exchange, RMB market price, shares held, RMB market value, net assets percent.

Numeric contract: 각 열에 RMB, shares, `%`가 명시돼 있어 scale/currency를 증명할 수 있다.

Temporal contract: Daily Holdings `As of`가 명시되지만 현재 페이지는 2026-08-27로 cutoff 이후다. historical date selector/archive를 찾지 못했다.

Coverage: 내부 해외 ETF에서 Global X manager-name candidate 123개(2.06%), 모두 ISIN과 RIC가 있다. 홍콩 3191은 내부 reference에서 확인되지 않아 confirmed match 0이다.

robots/access: 공개 HTML/CSV. robots/terms와 CSV URL stability는 adapter 전 확인한다.

Drift risk: 낮음~중간(CSV), 중간(HTML).

Limitations: 현재-only라면 평가 cutoff에 사용할 수 없다.

Historical snapshot available: No, 이번 조사에서 발견하지 못함

Closest snapshot on or before cutoff: 없음

Effective date source: page/CSV `As of`

Cutoff compatible: No

## 4. Source Authority Policy

Product availability: 미래에셋증권에서 거래/판매 가능하다는 사실은 미래에셋증권이 우선한다.

ETF holdings: 해당 ETF 운용사/issuer의 dated portfolio disclosure가 우선한다. 국내 KRX PDF는 공식 거래소 source이나 “설정 바스켓” 의미를 보존하고 실제 전체 보유량과 혼동하지 않는다.

Listing facts: KRX 또는 해당 해외 거래소가 우선한다.

Corporate relationships: DART/규제 공시 또는 공식 기업 공시가 우선한다. 이번 milestone에서 관계 수집은 하지 않는다.

Risk information: issuer/운용사 투자설명서가 우선하고, 미래에셋증권 자사 발행 ETN은 미래에셋증권이 우선한다.

Temporal/news evidence: 원 발행기관의 timestamped document가 우선한다. 뉴스는 보조 증거일 뿐 holdings를 확정하지 않는다.

`source_trust_tier`와 `fact_authority`는 별도 필드다. 같은 Tier 1이라도 판매사, 운용사, 거래소는 소유하는 사실이 다르다. 충돌 시 전역 provider 순위가 아니라 `(fact_type, market, effective_date, authority_role)`로 결정한다.

## 5. Existing ETF Universe Coverage

Reference workbook 기준:

| 범위 | ETF 수 | 안정 식별자 현황 | 후보 source coverage | 확인 상태 |
|---|---:|---|---:|---|
| 국내 전체 | 1,235 | `pd_itm_no` 1,235/1,235, ISIN 형태 | KRX eligible 1,235 | live join 미실행 |
| KODEX manager-name 후보 | 268 | ISIN 후보 268 | 21.70% | provider catalog join 미실행 |
| TIGER manager-name 후보 | 236 | ISIN 후보 236 | 19.11% | provider catalog join 미실행 |
| 해외 전체 | 5,972 | ISIN 5,960, RIC 5,972 | 다중 issuer 필요 | 12개 ISIN 결측 |
| iShares/BlackRock 후보 | 498 | ISIN/RIC 498 | 8.34% | 미확인 |
| Invesco 후보 | 260 | ISIN/RIC 260 | 4.35% | 미확인 |
| State Street/SPDR 후보 | 182 | ISIN/RIC 182 | 3.05% | 미확인 |
| Global X 후보 | 123 | ISIN/RIC 123 | 2.06% | 미확인 |
| Vanguard 후보 | 113 | ISIN/RIC 113 | 1.89% | 미확인 |
| KraneShares 후보 | 38 | ISIN/RIC 38 | 0.64% | KSTR 1개 exact 확인 |

위 여섯 해외 issuer 문자열 후보의 합집합은 1,214개(20.33%)이며 4,758개가 남는다. 이는 fuzzy match가 아니라 manager 문자열 포함 통계지만, provider catalog와의 identity confirmation은 아니다. 정확 이름만 일치하는 후보도 confirmed coverage에 포함하지 않았다.

Mirae: holdings confirmed coverage 0.

Official asset-manager sources: KODEX historical contract 1개 상품 probe 성공; KSTR exact identity/dated Cambricon evidence 성공. 다른 숫자는 eligible candidate다.

Other: KRX는 국내 전체 coverage 잠재력이 가장 크지만 cutoff/history/terms 계약이 아직 미확정이다. 해외 전체를 공식 source만으로 덮으려면 issuer adapter registry가 필요하다.

## ETF Holdings Coverage

국내 ETF는 KRX PDF를 universe-wide source 후보로 두고, KODEX·TIGER 등 운용사 disclosure를 fact-owner source로 보강하는 구조가 현실적이다. KODEX는 2026-08-24 historical JSON probe까지 통과했다. TIGER와 KRX는 source contract가 확정되지 않아 아직 confirmed coverage에 포함하지 않는다.

해외 ETF는 단일 primary source가 없다. 현재 내부 universe의 manager-name 기준 상위 adapter 후보는 BlackRock/iShares 498, Invesco 260, State Street/SPDR 182, Global X 123, Vanguard 113, KraneShares 38개다. 이 여섯 후보의 합집합도 1,214/5,972(20.33%)이므로 provider registry와 unsupported-market 상태가 필요하다. ISIN이 없는 해외 ETF 12개는 RIC + ticker + exchange를 통해서만 후보를 만들고 name-only로 확정하지 않는다.

coverage 통계는 2026-08-24 master workbook에 대한 read-only 후보 통계다. source catalog join을 수행하지 않은 숫자를 “수집 가능” 또는 “식별 완료”로 표현하지 않는다.

## Public Fund Holdings Coverage

### Mirae Asset Securities

미래에셋증권 공개 펀드 검색/상세는 상품명, 상품유형, 투자지역, 12개월을 포함한 기간 수익률, 순자산, 전략 설명과 포트폴리오 탭을 노출한다. 판매 가능 상품과 판매 class 정보에는 적절한 source지만, 제3자 운용사의 portfolio fact는 실제 운용사 또는 공식 공시가 우선한다. 공개 페이지에서 모든 판매 펀드의 holdings를 일괄 취득할 stable contract는 확인하지 못했다.

### Mirae Asset Global Investments public funds

미래에셋자산운용 공식 펀드 상세는 `fundCd`를 source product ID로 사용하며, 주요보유종목 TOP10, raw 종목명, 비중과 `YY.MM.DD 종가기준`을 제공한다. 예를 들어 미래에셋대형주포커스 펀드는 2026-07-26 기준 삼성전자 비중을 명시한다. 상품 class 표와 portfolio-level 본문이 함께 있으므로 adapter는 share class ID와 portfolio source ID를 별도로 보존해야 한다.

이 source는 “공개된 TOP10에 포함됨”을 증명할 수 있지만 다음은 증명하지 못한다.

- TOP10 밖 종목을 보유하지 않았다는 사실
- 전체 portfolio weight 합계
- raw name만 있는 종목의 canonical security identity
- 2026-07-26 이후 2026-08-24까지 holdings가 유지됐다는 사실

historical_snapshot_available: Yes, dated page/report

closest_snapshot_on_or_before_cutoff: 상품별 상이; 확인 예시는 2026-07-26

effective_date_source: 페이지의 `기준일(... 종가기준)` 또는 자산운용보고서 당기말

cutoff_compatible: Partially; source별 가장 가까운 명시 날짜에 한함

### KOFIA Disclosure

금융투자협회 전자공시의 자산운용보고서는 펀드 개요, 운용경과·수익률, 자산현황, 비용과 투자자산 매매내역을 포함하는 공식 정기보고서다. 이는 전 운용사 공모펀드의 권위 있는 공통 discovery layer 후보이며, 보고서 원문은 raw PDF로 보존해야 한다.

다만 자산운용보고서의 주요 보유자산 표는 일반적으로 전체 보유자산의 상위 10종목, 자산총액 5% 초과 종목, 발행주식 총수 1% 초과 종목 등을 표시하는 disclosure subset이다. 따라서 reverse lookup의 positive evidence에는 사용할 수 있지만 exhaustive negative lookup에는 사용할 수 없다. PDF table 품질이 낮으면 `PARTIAL` 또는 `PARSE_FAILED`로 닫는다.

historical_snapshot_available: Yes, 정기보고서

closest_snapshot_on_or_before_cutoff: 펀드 결산/보고주기별 상이, 개별 discovery 필요

effective_date_source: 보고서의 당기말/자산기준일

cutoff_compatible: 조건부 Yes; 명시된 당기말이 2026-08-24 이하일 때만

### Existing public-fund reference coverage

내부 공모펀드 판매 class는 23,676개다. `itm_no`는 전행 고유하지만 source-level 내부 ID이고, 유효한 `std_itm_no`는 19,319행, `ksd_itm_no`는 21,309행이다. `fss_itm_no`에는 `000000000000`, `rptt_ksd_itm_no`에는 `KR0000000000` 같은 sentinel이 있어 identifier로 쓰면 안 된다.

동일 portfolio가 여러 class로 반복되므로 23,676을 holdings 대상 portfolio 수로 간주하지 않는다. 공식 운용사/KOFIA source와 `KOFIA/KSD/FSS/manager source ID`로 portfolio를 먼저 확인한 뒤 class를 연결해야 한다. 현재 전체 portfolio denominator가 확정되지 않아 public-fund confirmed holdings coverage %는 산출하지 않는다.

## Constituent-to-Product Reverse Lookup Feasibility

목표 index는 canonicalization 전 source level에서 다음 키를 보존해야 한다.

```text
(constituent_isin)
or (constituent_ticker, constituent_exchange)
or (source_provider, constituent_source_id)
    -> (product_category, product_source_id, effective_date, source_record_id)
```

Domestic ETF: KODEX는 6자리 구성종목 코드로 positive reverse lookup 가능. KRX/TIGER가 확정되면 coverage 확대 가능.

Foreign ETF: KSTR처럼 ticker + ISIN + exchange가 있는 source는 가능. issuer마다 identifier 품질이 달라 name-only row는 후보로만 반환한다.

Public Fund: 공식 운용사 TOP10과 KOFIA 보고서는 positive lookup 가능하지만 종목명이 유일 식별자인 경우 `NAME_ONLY`다. disclosure subset이므로 결과가 없다고 “미보유”로 판정하면 안 된다.

결론: `Security → held by → Product` positive evidence retrieval은 가능하다. 전체 시장에 대한 완전한 역색인과 negative proof는 현재 Unsupported다.

## Sector-to-Product Data Feasibility

구조화 source 후보와 사용 규칙은 다음과 같다.

- 국내 ETF master: `pd_sect_cd`가 있으나 공식 코드표가 없으므로 raw/unmapped code로만 보존
- 국내/해외 ETF master: `wu_inv_ast_type`, `wu_inv_rgn`, `ref_ast_type`, `ref_geo_focus`, `cu_base_index`, `cu_strtegy`; 원천 설명과 코드 의미가 확인된 필드만 source classification으로 사용
- 공모펀드 master: `zrin_btyp_cd`/`zrin_btyp_nm` 18개 유형, `zrin_attr_nms`, `fd_ivst_rgn_desc`, benchmark 명칭. `zrin_btyp_nm`처럼 code-name pair가 있는 분류는 source concept로 보존 가능
- 미래에셋증권 펀드 검색: 투자지역과 상품유형의 structured filter
- KOFIA: 운용실적 분류, 주요투자지역, 집합투자기구 종류
- 운용사 상품 페이지: theme/strategy 설명과 공식 theme collection

crawler는 source classification과 raw authoritative strategy text만 저장한다. `반도체`, `우주항공` 같은 canonical sector/theme 관계는 공식 structured label이 있을 때만 asserted source field로 내보내고, 임의 본문 키워드 출현으로 관계를 만들지 않는다.

Sector → Product는 KOFIA/운용사 structured category 범위에서 Supported, arbitrary theme text의 canonical relation은 Unsupported다.

## Annual Return Source Contract

### Domestic ETF

Candidate sources: KRX/운용사 공식 NAV performance, 기존 master `du_er_1y`.

- metric definition: master schema는 `수익률_1Y`라고만 정의해 NAV/시장가격, 분배금 재투자, 세전 여부를 증명하지 못함
- period: trailing 1 year로 보이나 exact endpoints 포함 규칙 미확정
- unit/scale: 실제 값과 명칭상 percent 후보지만 공식 단위 계약 미확정
- observation date: `du_nav_base_dt` 또는 별도 source response date를 row-level로 연결해야 함
- AUM: `du_last_aum`; 단위/통화는 source contract 없이는 비교 금지
- cross-product comparability: No, 현재 정의만으로는 불충분

KODEX 공식 상품 페이지는 NAV 수익률이 분배금 재투자를 가정한 세전 수익률이라고 설명한다. 이 정의와 명시 observation date를 사용하면 KODEX 내부 비교는 가능하지만, 다른 category가 같은 정의임을 별도로 증명해야 한다.

### Foreign ETF

Candidate sources: 각 ETF issuer의 official performance/holdings page.

- metric definition: issuer별 Fund NAV total return 또는 market-price return을 구분
- period: trailing one year 또는 calendar annual return을 분리
- unit/scale: percent
- observation date: issuer `Data as of` 필수
- adjusted semantics: distribution reinvestment, fees, NAV/market price를 각각 명시
- AUM: net assets와 currency를 함께 저장
- cross-product comparability: No by default; 같은 `TRAILING_1Y + NAV_TOTAL_RETURN + DISTRIBUTIONS_REINVESTED + NET_OF_FUND_EXPENSES + 동일 observation date`일 때만 candidate

현재 해외 master에는 `du_er_1d`만 있고 1년 수익률 칼럼은 없다. 따라서 외부 issuer performance contract 없이는 TOP10 연 수익률 질의를 지원하지 못한다.

### Public Fund

Candidate sources: KOFIA 운용실적 비교공시, 미래에셋증권 펀드 검색, 실제 운용사.

- metric definition: KOFIA는 Time Weighted 방식으로 기간 중 분배율을 반영한 일별 등락률을 평가일 기준 과거 12개월 등에 대해 곱해 산정
- period: 평가일 기준 과거 12개월
- unit/scale: percent
- observation date: KOFIA 평가일/기준년월 또는 source 기준일
- AUM: KOFIA 순자산총액은 평가일 현재, 억원 단위; 내부 `fd_nast_suma`는 schema상 펀드 순자산이나 단위는 별도 계약 필요
- existing master: `fd_yr1_ern_r`(펀드 1년수익률) 7,022/23,676, `fd_nast_suma` 9,413/23,676, 관련 기준일 `fd_price_bas_dt`/`fd_daily_bas_dt`; 2026-08-21 값이 다수여서 cutoff에는 적합할 수 있으나 source definition 검증이 필요
- cross-product comparability: KOFIA public funds끼리는 동일 공시 정의 내 비교 가능; ETF와는 NAV total-return 정의·분배금·보수·세전·기간 끝점이 모두 일치할 때만 가능

### Comparison gate

연 수익률 ranking 입력에는 다음 tuple이 완전히 확인돼야 한다.

```text
metric_kind = TRAILING_1Y_TOTAL_RETURN
price_basis = NAV
distribution_treatment = REINVESTED
fee_basis = NET_OF_FUND_EXPENSES
tax_basis = PRE_TAX
observation_date = same evaluation date (or documented market-calendar alignment)
unit = PERCENT
scale = PERCENT_POINTS
```

하나라도 미확정이면 원 값은 보존하되 cross-product sort 대상에서 제외한다. calendar-year return과 trailing-one-year return을 섞지 않는다.

## Cross-Product Query Feasibility

Capability probe: “삼성전자를 보유한 국내/해외 ETF와 공모펀드를 연 수익률 기준 TOP10”.

1. 삼성전자 security identity: canonicalization team이 ISIN/ticker/exchange를 확정해야 한다. crawler는 source identifier만 제공한다.
2. 국내 ETF holdings: KODEX subset은 가능, KRX/TIGER 및 타 운용사 미확정.
3. 해외 ETF holdings: issuer adapter가 있는 subset만 가능. 삼성전자 원주/DR/GDR와 name variant를 crawler가 병합하지 않는다.
4. 공모펀드 holdings: 미래에셋자산운용 TOP10/KOFIA disclosed subset에서 positive evidence 가능. 전체 portfolio exhaustiveness는 없음.
5. 1년 수익률: 공모펀드는 KOFIA 정의가 가장 명확하다. 국내·해외 ETF는 동일 NAV total-return 계약을 provider별로 확보해야 한다.
6. ranking: crawler 밖의 deterministic Agent 단계가 동일 metric semantics와 observation date를 통과한 상품만 정렬한다.

현재 판정은 **Partially Supported**다. source-backed positive holding 후보 생성은 가능하지만 ETF 전 운용사/해외 issuer/public-fund 전체 coverage와 cross-product comparable return contract가 완성되지 않았다. missing product를 0 holding으로 취급하거나 서로 다른 return 정의를 섞어 TOP10을 만들면 안 된다.

## 6. Cambricon Feasibility

Can identify ETF: Yes. 내부 KSTR은 RIC `KSTR.K`, ticker `KSTR`, ISIN `US5007676944`; 공식 issuer도 ticker, ISIN, NYSE Arca를 제공한다.

Can identify Cambricon constituent: Yes. 공식 source raw name `CAMBRICON TECHNOLOGIES-A`, ticker `688256`; 현재 official holdings는 identifier `CNE1000041R8`도 제공한다.

Can prove holding: Yes, source effective date에 한정한다. KraneShares 공식 2026-06-30 자료는 KSTR의 Cambricon 편입과 비중을 명시한다. Global X HK 공식 현재 페이지도 3191의 Cambricon 편입을 보여주지만 cutoff 이후다.

Can prove effective date: Yes for 2026-06-30 KraneShares document. 2026-08-24 시점까지 지속됐다는 것은 증명하지 않는다.

Evidence source: KraneShares official dated presentation/factsheet and holdings page.

Partially Supported. “2026-08-24 이전에 편입 이력이 있는 ETF”는 지원하지만, “2026-08-24 현재 편입”은 cutoff 인접 snapshot 없이는 `CUTOFF_UNVERIFIED`다.

## 7. EcoPro Holdings-Side Feasibility

국내 KODEX probe는 구성종목 `itmNo`로 6자리 종목코드를 제공하므로, 별도 corporate pipeline이 확정한 에코프로 자회사 상장증권 코드 집합과 exact join할 수 있다. KRX/TIGER도 constituent code가 확정되면 같은 방식이 가능하다.

지원 조건:

- subsidiary fact와 security identity는 별도 authoritative pipeline이 제공
- crawler는 회사명을 fuzzy merge하지 않음
- holdings constituent code + exchange 또는 ISIN으로만 confirmed join
- 현금·채권·파생 코드와 equity ticker를 구분

KODEX 범위는 Supported, 국내 전체 범위는 KRX/TIGER 계약 확정 전 Partially Supported다.

## 8. Weight / Numeric Contract

모든 numeric은 JSONL에서 JSON number가 아니라 precision-safe decimal string으로 저장한다.

| source field | raw example | normalized rule | unit/scale |
|---|---|---|---|
| KODEX `ratio` | `"25.11"` | source가 percent임을 증명했을 때만 `"0.2511"` | `PERCENT_OF_NON_CASH_ASSETS`, `PERCENT_POINTS` |
| KODEX `applyQ` | `"6467"` | `"6467"` | `UNITS_PER_CREATION_UNIT`은 재검증 후 확정 |
| KODEX `evalA` | `"233458700"` | `"233458700"` | source currency가 증명된 경우만 currency 기록 |
| KSTR `% of Net Assets` | `"9.90%"` | `"0.0990"` | `PERCENT_OF_NET_ASSETS`, `PERCENT_POINTS` |
| KSTR shares | `"222,860"` | `"222860"` | `SHARES` |
| KSTR market value | `"33,809,828"` | `"33809828"` | `USD` |
| Global X HK Net Assets | `"8.86"` | `"0.0886"` | `PERCENT_OF_NET_ASSETS`, `PERCENT_POINTS` |

`5`, `5%`, `0.05`는 source contract 없이 서로 변환하지 않는다. `rank`는 원문 순서가 “비중순”으로 명시될 때만 생성하고 그렇지 않으면 null이다.

## 9. Temporal Contract

- global `data_cutoff_date`: `2026-08-24`
- eligible holdings: 가장 가까운 명시적 `effective_date <= 2026-08-24`
- non-business-day fallback: crawler가 달력을 추측하지 않고 provider가 반환한 effective date를 채택
- current-only source: cutoff snapshot에서 제외하고 `CUTOFF_UNVERIFIED`
- `retrieved_at`: 실제 UTC 다운로드 시각
- `published_at`: source가 제공할 때만
- `effective_date`: portfolio/as-of 날짜
- `source_retrieved_at`이 cutoff 이후여도 response가 과거 effective date를 명시하면 그 과거 fact는 후보가 될 수 있음
- 과거 문서가 cutoff 이전 holding을 증명해도 별도 종료 의미가 없으면 “cutoff 당일 current”로 승격하지 않음

Manifest에는 향후 다음을 추가해야 한다. 이는 이번 milestone에서 코드로 구현하지 않았다.

```json
{
  "data_cutoff_date": "2026-08-24",
  "crawler_run_date": "...",
  "source_effective_date": "...",
  "source_published_at": null,
  "source_retrieved_at": "..."
}
```

## 10. Proposed external-holdings-v1 Schema

Schema ID: `external-holdings-v1`

```text
holding_record_id: string (required, deterministic)

product_category: enum (required)             # DOMESTIC_ETF, FOREIGN_ETF, PUBLIC_FUND
product_name_raw: string|null
product_ticker: string|null
product_isin: string|null
product_exchange: string|null
product_market: string|null
product_source_id: string (required)
product_portfolio_source_id: string|null       # fund portfolio identity
product_share_class_source_id: string|null     # distribution/share class identity

constituent_name_raw: string (required)
constituent_name_normalized: string|null
constituent_ticker: string|null
constituent_isin: string|null
constituent_exchange: string|null
constituent_source_id: string|null
constituent_asset_type_raw: string|null

weight_raw: string|null
weight_normalized: decimal-string|null       # proportion, not percent points
weight_unit: enum|null                       # PERCENT_OF_NET_ASSETS, PERCENT_OF_NON_CASH_ASSETS, UNKNOWN
weight_scale: enum|null                      # PERCENT_POINTS, PROPORTION, UNKNOWN
quantity_raw: string|null
quantity_normalized: decimal-string|null
quantity_unit: enum|null                     # SHARES, UNITS_PER_CREATION_UNIT, UNKNOWN
market_value_raw: string|null
market_value_normalized: decimal-string|null
market_value_currency: ISO-4217-string|null
rank: integer|null

effective_date: ISO-date|null
published_at: ISO-datetime|null
retrieved_at: ISO-datetime (required, UTC)
data_cutoff_date: ISO-date (required; 2026-08-24 for evaluation)
cutoff_status: enum (ELIGIBLE, POST_CUTOFF, CUTOFF_UNVERIFIED)

source_record_id: string (required)
source_provider: string (required)
source_url: string (required)
source_trust_tier: enum (required)
fact_authority_role: enum (ASSET_MANAGER, ISSUER, EXCHANGE)
snapshot_id: string (required)
raw_artifact_path: string (required)
source_schema_version: string (required)

product_identity_status: enum (VERIFIED_IDENTIFIER, SOURCE_ID_ONLY, NAME_ONLY, UNRESOLVED)
constituent_identity_status: enum (VERIFIED_IDENTIFIER, SOURCE_ID_ONLY, NAME_ONLY, NON_SECURITY, UNRESOLVED)
numeric_status: enum (VALIDATED, PARTIAL, RAW_ONLY, INVALID)
temporal_status: enum (EFFECTIVE_DATE_VERIFIED, PUBLISHED_ONLY, RETRIEVAL_ONLY, CUTOFF_UNVERIFIED)
validation_status: enum (VALID, PARTIAL, VALIDATION_FAILED)
```

Deterministic ID input is provider + product category + product source ID + constituent source ID or raw-name hash + effective date + source record ID. 공모펀드는 portfolio source ID를 identity 중심으로 사용하고 share class는 별도 연결한다. No canonical product/company/ontology/Neo4j ID를 포함하지 않는다. `effective_date`가 null이면 evaluation-ready row가 될 수 없다.

## 11. Proposed Provider Adapter Architecture

구현은 아직 하지 않는다. 승인 후 구조는 다음과 같다.

```text
app/external_data/holdings/
├── models.py                 # external-holdings-v1 only
├── normalize.py              # safe shared lexical/date/decimal helpers
├── contract.py               # provider-neutral validation + cutoff gate
└── providers/
    ├── kodex.py              # fId/gijunYMD and KODEX field semantics
    ├── krx_pdf.py            # only after terms/history contract approval
    ├── tiger.py              # only after stable endpoint/date approval
    ├── kraneshares.py        # CSV + dated-document variants
    └── global_x_hk.py        # current-only until history is proven
```

각 adapter는 raw provider response와 provider contract version을 보존하고 같은 `external-holdings-v1`만 emit한다. `normalize.py`는 identity merge, currency/date/percent 추측을 하지 않는다.

## 12. Source Quality / Report Metadata

각 snapshot source quality record에 다음을 넣는다.

```text
provider
organization
fact_type
fact_authority_role
why_authoritative
source_trust_tier
markets_covered
access_method
authentication_required
robots_result
terms_review_status
identifiers_available
holding_fields_available
numeric_semantics
temporal_semantics
update_frequency
historical_snapshot_available
closest_snapshot_on_or_before_cutoff
effective_date_source
cutoff_compatible
eligible_etf_count
confirmed_isin_matches
confirmed_ticker_exchange_matches
name_only_candidates
unmatched_count
schema_drift_risk
known_limitations
failure_rate
recommended_use
```

Competition trace는 `Source → authority rationale → access/raw hash → deterministic parse → validation → source-level normalization → immutable snapshot/manifest → later canonicalization → retrieval/evidence` 순서로 남긴다.

## 13. Risks

- 미래에셋증권 HTS에 보이는 구성종목은 공개 자동수집 계약이 아니다.
- KRX PDF는 실제 펀드 전체 보유량이 아니라 creation/redemption basket일 수 있다.
- KODEX 비중은 현금성 자산을 제외한 기준이라 다른 issuer의 net-assets weight와 직접 비교하면 안 된다.
- provider 내부 API는 공개 UI가 사용해도 API SLA가 없고 schema drift 가능성이 있다.
- 국내 manager 문자열은 브랜드·운용법인 변형이 있어 coverage 확인 근거가 아니다.
- 해외 5,972개는 382개 manager 문자열을 포함해 adapter 폭발 위험이 있다.
- 현재-only holdings는 2026-08-24 평가 snapshot에 사용할 수 없다.
- dated factsheet Top 10은 full holdings가 아니어서 negative evidence로 사용할 수 없다.
- 토요일 cutoff에는 직전 영업일 반환을 source response로 확인해야 하며 자체 달력 보정은 금지한다.
- 공식 source라도 이용약관, rate limit, 재배포 조건 검토가 필요하다.

## 14. Recommended Strategy

C

Reason: 미래에셋증권은 상품·위험 정보에는 유용하지만 재현 가능한 ETF holdings bulk source로는 불충분하다. 국내는 공식 KRX와 운용사 adapter가 필요하고, 해외는 issuer별 adapter가 필수다. 상위 여섯 해외 issuer 후보만으로도 현재 유니버스의 20.33%에 불과하므로, A–C source가 없는 시장에는 출처가 명시된 trusted secondary provider가 필요할 가능성이 높다. 단, cutoff/history를 증명하지 못하는 범위는 Strategy D처럼 unsupported로 남긴다.

## 15. Ready for Crawl-2 Implementation

No. KODEX 단일 ETF adapter는 구현 가능한 수준이지만, 최신 지침이 요구하는 ETF와 공모펀드 양쪽의 source contract를 Crawl-2 acceptance 범위로 보면 KOFIA 조회/다운로드 계약, portfolio 식별자, report cutoff 선택 규칙이 아직 미확정이다. 이 상태에서 ETF만 자동 구현하지 않는다.

Research-approved providers, not yet a command to implement:

- Samsung Asset Management KODEX public holdings JSON/Excel
- KraneShares KSTR official dated document/current CSV, 서로 다른 temporal capability로 분리
- Mirae Asset Global Investments public-fund dated TOP10/report, positive evidence only
- KOFIA asset-management reports, access/header contract 확인 조건부

Allowed fact types per provider:

- KODEX: 자사 ETF 구성종목(PDF), 구성수량, source-defined 비중, 평가금액, 응답 effective date
- KraneShares: 자사 ETF holdings; cutoff snapshot에는 명시적 on/before-cutoff dated artifact만

Required adapters after approval:

- `kodex.py`를 Crawl-2 첫 acceptance adapter로 구현
- `kraneshares.py`는 dated document와 current CSV parser를 분리하고 cutoff gate 적용
- `kofia_fund_report.py`는 portfolio/share-class를 분리하고 disclosed subset을 표시
- `mirae_fund.py`는 공식 TOP10을 full portfolio로 오인하지 않도록 `disclosure_scope=TOP10`을 강제

Known unsupported scopes:

- 미래에셋증권 ETF holdings 자동 수집
- KRX 전체 국내 coverage: terms/history/response header 확정 전
- TIGER 전체 holdings: stable endpoint와 2026-08-24 snapshot 확인 전
- Global X HK current-only holdings의 2026-08-24 사용
- 나머지 국내 운용사와 해외 issuer
- 미래에셋 이외 공모펀드 운용사의 full holdings 및 KOFIA PDF 밖 종목
- official source가 없거나 historical cutoff를 증명할 수 없는 시장

Do not begin Crawl-2 automatically.

## Data Cutoff Compatibility

Global cutoff: 2026-08-24

Mirae Asset Securities:

- Historical snapshot available: ETF holdings는 No/미확정
- Closest valid date: 없음
- Cutoff compatible: No

KRX:

- Historical snapshot available: 미확정
- Closest valid date: 미확정
- Cutoff compatible: No, 확인 전 fail-closed

KODEX:

- Historical snapshot available: Yes
- Closest valid date: 2026-08-24
- Cutoff compatible: Yes

TIGER:

- Historical snapshot available: 미확정
- Closest valid date: 미확정
- Cutoff compatible: No

KraneShares KSTR:

- Historical snapshot available: Yes, dated official document
- Closest valid date found: 2026-06-30
- Cutoff compatible: Partially; cutoff-day current assertion은 불가

Global X HK:

- Historical snapshot available: 발견하지 못함
- Closest valid date: 없음
- Cutoff compatible: No

Mirae Asset Global Investments public funds:

- Historical snapshot available: Yes, dated TOP10 pages and asset-management reports
- Closest valid date: product-specific; verified example 2026-07-26
- Cutoff compatible: Partially, disclosed positive holdings at the stated date only

KOFIA public-fund disclosures:

- Historical snapshot available: Yes
- Closest valid date: fund/report-cycle specific, not yet enumerated
- Cutoff compatible: Conditionally; report asset date must be on or before 2026-08-24

Post-cutoff data excluded:

- KSTR current holdings dated 2026-08-26
- Global X HK 3191 holdings dated 2026-08-27
- 미래에셋자산운용 펀드 페이지 중 holdings 기준일이 2026-08-24 이후인 자료
- cutoff 이후의 current price/NAV/AUM

Cutoff-unverified facts:

- 미래에셋증권 HTS ETF 구성종목
- KRX/TIGER 2026-08-24 조회 미검증 범위
- historical archive가 없는 모든 current-only issuer holdings

## 조사 근거

- [미래에셋증권 ETF 주문 화면 안내](https://securities.miraeasset.com/kairos/0777.htm)
- [미래에셋증권 ETF 전종목 시세 안내](https://securities.miraeasset.com/kairos/0780.htm)
- [미래에셋증권 ETN 상품 상세 예시](https://securities.miraeasset.com/hks/hks4318/n01.do?S_CD=520075&ZONE_SECT=08)
- [KRX ETF settlement/PDF 설명](https://global.krx.co.kr/contents/GLB/06/0605/0605010101/GLB0605010101T2.jsp)
- [KRX Data Marketplace](https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd)
- [KODEX 공식 상품·구성종목 예시](https://www.samsungfund.com/etf/product/view.do?id=2ETF15)
- [TIGER 공식 상품·구성종목 예시](https://www.tigeretf.com/ko/product/search/detail/index.do?ksdFund=KR7138530001)
- [KraneShares KSTR 공식 상품/holdings](https://kraneshares.com/etf/kstr/)
- [KraneShares KSTR 공식 2026-06-30 자료](https://engage.kraneshares.com/s/b264ceb1/kstr-presentation/)
- [Global X HK 3191 공식 상품/holdings](https://www.globalxetfs.com.hk/funds/global-x-china-semiconductor-etf/)
- [미래에셋증권 공모펀드 검색](https://trading.securities.miraeasset.com/hks/hks4116/r01.do)
- [미래에셋자산운용 공모펀드 holdings 예시](https://develop.investments.miraeasset.com/magi/fund/view.do?fundCd=482000&fundGb=2)
- [KOFIA 운용실적 비교공시 기준](https://dis.kofia.or.kr/wq/fundann/DISMngResCmpAnnNtcPop.html)
- [KOFIA 전자공시서비스 매뉴얼](https://dis.kofia.or.kr/doc/dis_manual.pdf)

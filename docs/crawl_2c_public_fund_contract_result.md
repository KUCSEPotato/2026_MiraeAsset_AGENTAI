# Crawl-2C Public Fund Contract Result

조사 기준일은 2026-08-30이고 평가용 외부 데이터 cutoff는 2026-08-24다. 이 단계는 source contract 조사만 수행했다. 공모펀드 adapter, canonical entity resolution 및 Agent 연동은 구현하지 않았다.

## KOFIA Access Contract

금융투자협회 전자공시서비스에서 다음 공식 화면과 입력 계약을 확인했다.

- 펀드 수시공시 검색: `/wq/fundann/DISFundAnnSrch.xml`
- 펀드 운용실적 비교: `/wq/fundann/DISFundROPCmpAnn.xml`
- 수시공시 검색 DTO: `DISFTimeAnnInsDTO`; 조회기간, 보고서 구분, 회사 코드와 펀드 선택값을 전달
- 펀드 선택값: 화면 계약상 `standardCd`
- 운용실적 조회 입력: `standardDt`, `manageResBrnCd`, `fundGb`, `mngScl`, `standardCurr` 등
- 수익률 기간 선택: `ropGb5=1년`

하지만 화면 XML은 데이터 자체가 아니다. 실제 조회는 WebSquare/ProFrame 서비스 호출에 의존하며, 조사 범위에서 아래 항목을 재현 가능한 공개 계약으로 고정하지 못했다.

- 안정된 request/response endpoint 및 공식 지원 여부
- 응답 header와 자료형
- pagination 또는 전체 다운로드 경계
- 보고서 다운로드 token과 원문 파일 URL의 수명
- 오류·빈 결과·schema drift의 구분
- 요청 빈도와 이용 조건

KOFIA OpenAPI 공개 목록에서도 펀드 공시·포트폴리오 API는 확인하지 못했다. 따라서 화면 내부 호출을 추측해 adapter를 만들지 않는다. 공식 근거는 [전자공시서비스 이용자 매뉴얼](https://dis.kofia.or.kr/doc/dis_manual.pdf)과 [운용실적 비교공시 안내](https://dis.kofia.or.kr/wq/fundann/DISMngResCmpAnnNtcPop.html)에 한정한다.

판정: authoritative discovery source는 확인했지만 acquisition/download contract는 미확정이다.

## Product vs Share-Class Grain

KOFIA 공식 안내는 운용펀드와 클래스펀드를 구별한다.

- 운용실적 비교 대상은 모신탁, 자신탁 및 종류형의 운용펀드/클래스펀드를 포함한다.
- 종류형펀드의 대상 선정은 운용펀드를 기준으로 하되 클래스별 순위 조건도 별도로 적용한다.
- 따라서 portfolio holdings의 주체와 투자자가 매입하는 share class는 같은 grain이 아니다.

외부 source adapter가 만들어질 경우 최소한 다음 두 source-level 개체를 분리해야 한다.

```text
source fund/portfolio record
    └── source share-class record(s)
```

다만 KOFIA 조회 결과의 어느 식별자가 portfolio이고 어느 식별자가 class인지 실제 응답과 보고서 원문을 대조하지 못했다. 이름의 클래스 suffix만으로 관계를 만들거나 같은 이름을 병합해서는 안 된다.

## Internal PRFD Grain Reconciliation

현재 프로젝트의 최신 rebuild 계약은 PRFD를 다음처럼 구분한다.

| 구분 | 건수 | 의미 |
|---|---:|---|
| raw PRFD source records | 23,676 | 원본 workbook 행 |
| valid PRFD source records | 23,676 | source schema 검증을 통과한 행 |
| resolved `FundShareClass` entities | 16,574 | 안전한 대표 펀드 식별자와 연결돼 class entity로 생성된 행 |
| unresolved PRFD source records | 7,102 | `UNRESOLVED_PARENT`; source record와 assertion은 보존되지만 canonical class로 확정되지 않음 |
| resolved parent `Fund` entities | 6,867 | 여러 class가 공유할 수 있는 parent portfolio |

공모(`prvo_pbff_desc == "공모"`) subset은 resolved 11,481행, unresolved 3,235행이다. 이 수치는 source row 수이지 외부 KOFIA portfolio coverage가 아니다.

따라서 다음 등식은 성립하지 않는다.

```text
PRFD source record identity != canonical FundShareClass identity
PRFD row count != distinct portfolio count
```

Crawler는 raw identity와 source provenance만 보존한다. 외부 record가 기존 `FundShareClass` 또는 parent `Fund`와 같은지 판정하는 일은 이후 Entity Resolution의 책임이다.

## Stable Identifiers

내부 PRFD 후보 식별자의 현재 해석은 다음과 같다.

| 필드 | 후보 grain | 사용 조건 |
|---|---|---|
| `itm_no` | PRFD source row / resolved class source key | 전행 고유하지만 dataset-local source ID이며 canonical ID가 아님 |
| `std_itm_no` | share class 후보 ISIN | 형식 검증과 충돌 검사를 통과한 값만 사용 |
| `ksd_itm_no` | KSD source identifier | sentinel·공란 제외, grain을 외부 contract로 확인해야 함 |
| `rptt_ksd_itm_no` | representative parent fund 후보 | parent `Fund` 연결 근거; `KR0000000000` 등 unsafe sentinel 제외 |
| `fss_itm_no` | FSS source identifier 후보 | `000000000000` 등 sentinel 제외 |
| `mtco_itm_no` | 미래에셋 source identifier 후보 | provider-local ID로만 보존 |
| exact product name/class suffix | 보조 identity evidence | 단독 merge key로 사용 금지 |
| manager | organization evidence | 상품 식별자가 아니며 단독 merge key로 사용 금지 |

KOFIA 화면의 `standardCd`가 표준코드 선택값임은 확인했지만, 실제 결과에서 portfolio code, share-class code, ISIN, manager code의 grain과 상호관계를 아직 증명하지 못했다. 외부 adapter는 이 관계가 확정될 때까지 구현하지 않는다.

## Holdings Availability

KOFIA 자산운용보고서는 펀드 개요, 운용경과·수익률, 자산현황, 비용 및 매매내역을 포함하는 authoritative document source다. 보고서의 주요 보유자산은 source-backed positive evidence로 사용할 수 있다.

그러나 공시 표는 일반적으로 전체 portfolio 원장을 의미하지 않는다. 상위 종목이나 중요도 기준을 충족한 종목만 표시될 수 있으므로 다음 규칙이 필요하다.

- 발견된 종목: 명시된 보고기간 말 기준 positive holding evidence
- 발견되지 않은 종목: 미보유 증거가 아님
- PDF 표 추출 실패 또는 셀 경계 불명확: `PARTIAL` 또는 `PARSE_FAILED`
- 보고서의 당기말/자산기준일이 없는 행: cutoff-ready holding으로 사용 금지

현재 미해결 사항은 보고서 목록 전체 조회, 원문 파일의 안정 URL, 보고서별 portfolio ID, 표 header 변형 및 실제 coverage 규칙이다.

## Constituent Identifiers

KOFIA 안내만으로 주요 보유자산 표가 constituent ISIN, ticker, exchange 또는 KSD code를 항상 제공하는지 확인되지 않았다. 따라서 초기 contract에서는 다음 원칙만 승인할 수 있다.

- 원문 종목명과 표에 존재하는 source identifier를 그대로 보존
- ISIN/ticker/exchange가 실제 표에 있을 때만 각각 저장
- 이름만 존재하면 `NAME_ONLY`
- 동일·유사 이름을 crawler가 canonical company/security로 병합하지 않음
- portfolio 보고서와 페이지/표/행 provenance를 함께 보존

이 식별자 계약이 fixture로 고정되기 전에는 공모펀드 holdings adapter를 승인할 수 없다.

## 1-Year Return Contract

KOFIA 공식 운용실적 비교공시가 정의하는 1년 수익률은 다음과 같다.

- 방식: Time Weighted
- 분배: 기간 중 분배율 반영
- 계산: 일별 등락률을 곱하여 산정
- 기간: 평가일 현재를 기준으로 과거 12개월
- 표시 단위: percent
- observation date: 화면의 평가일/기준년월
- 대상: 공모펀드 운용실적 비교공시 대상

이는 calendar-year return이 아니다. `retrieved_at`을 평가일로 대신할 수 없다. NAV/기준가격 계열의 수익률로 이해할 근거는 있으나, ETF와 비교할 때 필요한 보수·세금·분배금 재투자·기간 끝점의 완전한 동등성은 별도로 증명해야 한다.

내부 PRFD의 `fd_yr1_ern_r`는 7,022/23,676행에 존재하지만 schema 설명만으로 KOFIA와 동일 산식이라고 확정하지 않는다. 원 값과 관련 기준일을 보존하되 source contract가 입증되기 전 cross-product ranking에는 사용하지 않는다.

## AUM Contract

KOFIA 운용실적 비교공시의 `순자산총액`은 다음 계약이 공식 안내에 명시돼 있다.

- 의미: 평가일 현재 순자산총액
- 표시 단위: 억원
- observation date: 평가일
- 종류형 대상 선정: 운용펀드 설정원본 기준을 사용하므로 class와 parent portfolio grain을 혼동하면 안 됨

다만 실제 조회 결과에서 표시되는 순자산총액이 각 클래스 행인지 운용펀드 합계인지, 종류형 화면의 반복 방식이 무엇인지는 응답 fixture로 확인하지 못했다. 따라서 AUM의 subject grain은 아직 미확정이다.

내부 `fd_nast_suma`는 9,413/23,676행에 존재하고 `fd_price_bas_dt`/`fd_daily_bas_dt`가 관련 날짜 후보지만, 원천 단위와 class/portfolio grain을 외부 KOFIA 값에 맞춰 추정하지 않는다.

## Cutoff Compatibility

전역 cutoff는 2026-08-24다.

- 운용실적 값: 명시 평가일이 2026-08-24 이하인 record만 허용
- 자산운용보고서 holdings: 명시 당기말/자산기준일이 cutoff 이하인 record만 허용
- 공시 문서: `published_at <= 2026-08-24`
- cutoff 이후 조회한 current 화면을 과거 값으로 backdate하지 않음
- `retrieved_at`은 effective date 또는 published date를 대체하지 않음

KOFIA는 기준일 입력 UI와 과거 정기보고서가 있어 historical source 가능성은 확인됐다. 하지만 2026-08-24 이전의 가장 가까운 응답을 동일한 request/response 계약으로 재현하지 못했으므로 adapter 수준의 cutoff compatibility는 아직 `Unresolved`다.

## Cross-Product Comparability

KOFIA 내부의 같은 운용실적 공시 정의와 같은 평가일을 사용하는 public-fund rows끼리는 비교 후보가 된다. ETF/해외 ETF와의 1년 수익률 비교는 다음 tuple이 모두 일치할 때만 허용해야 한다.

```text
metric_kind = TRAILING_1Y_TOTAL_RETURN
price_basis = NAV
distribution_treatment = REINVESTED/REFLECTED
fee_basis = proven equivalent
tax_basis = proven equivalent
observation_date = same date or documented calendar alignment
unit = PERCENT
scale = PERCENT_POINTS
```

현재 KODEX holdings adapter는 holdings만 수집하며 performance adapter가 아니다. 공모펀드 `fd_yr1_ern_r`와 국내 ETF `du_er_1y`, 해외 issuer return을 직접 한 열에 넣어 정렬할 contract도 확정되지 않았다.

따라서 “삼성전자를 보유한 국내/해외 ETF와 공모펀드를 연 수익률 기준 TOP10”은 다음 이유로 아직 end-to-end 지원되지 않는다.

1. 공모펀드 holdings가 disclosure subset이며 acquisition contract가 미확정이다.
2. constituent identifier coverage가 미확정이다.
3. ETF/Fund 수익률 정의의 동등성이 입증되지 않았다.
4. 상품 및 종목 canonical resolution은 crawler 범위 밖이다.

## Remaining Blockers

1. KOFIA 공식 bulk/download endpoint 또는 승인된 수집 방식
2. 실제 응답 schema, pagination, empty/error contract와 최소 fixture
3. `standardCd`를 포함한 portfolio/share-class identifier grain
4. report row와 parent portfolio/share class의 연결 규칙
5. holdings 표의 identifier·단위·coverage 규칙과 PDF 변형
6. cutoff 이전 최신 report/metric을 선택하는 deterministic 규칙
7. 1년 수익률 및 AUM의 실제 응답 field, scale, subject grain
8. 이용약관·rate limit·raw artifact 보존 방식의 운영 승인
9. 외부 source row와 내부 PRFD source/canonical entity 사이의 후속 Entity Resolution 계약

## Ready for Public Fund Adapter

No.

공식 공시의 의미와 화면 입력 필드는 확인했지만 안정적인 acquisition/download 계약, grain, identifier 및 cutoff fixture가 아직 부족하다. 이 상태에서 화면 내부 호출을 추정해 구현하면 재현성과 source provenance를 보장할 수 없다.

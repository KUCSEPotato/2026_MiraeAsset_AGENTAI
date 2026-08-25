# Financial Product Ontology

금융상품 추천 Agent가 채권, ETF, ETN, 펀드 클래스를 공통 어휘로 탐색하면서도 원본 행과 시점별 관측값을 잃지 않도록 만든 실행 가능한 OWL/SHACL 패키지입니다.

## 파일 구성

- `core.ttl`: 출처, 원본 행, 원본 칼럼 assertion, 품질 상태
- `products.ttl`: 상품 계층, 식별자, 채권 판매 LOT, 펀드 클래스
- `entities.ttl`: 기관, 시장, 지수, 지역, 자산유형과 관계
- `observations.ttl`: 가격·NAV·AUM·수익률 등 관측값
- `mappings.ttl`: Agent runtime canonical field와 자연어 alias
- `shapes.ttl`: 상품·식별자·원본 행·관측값 SHACL 제약
- `mappings/column_mapping.csv`: 최신 스키마 280개 전체 칼럼 매핑과 실제 값 프로파일
- `examples/sample_instances.ttl`: 실제 원본 구조를 반영한 5개 상품군 샘플
- `queries/*.rq`: 대표 SPARQL 질의
- `docs/design.md`: 설계 결정과 Agent 연결 방법
- `docs/source_analysis.md`: 스키마·실데이터 대조 결과와 불확실성

## 검증

```bash
uv run python scripts/validate_ontology.py
```

원본 Excel이 로컬에 있으면 샘플의 원본 키와 전체 280개 매핑 커버리지까지 검증합니다. Excel은 `material/` 아래에만 두며 Git에 포함하지 않습니다.

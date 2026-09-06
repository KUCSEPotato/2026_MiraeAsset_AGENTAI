# Source Code

## Final Commit

`a1d76c9366d78cc0ea6de33a57a2171e8950de5a`

준비 시점의 로컬 `HEAD`와 `origin/main`이 위 SHA로 일치했습니다.

## Exact Source Snapshot

- File: `source-code-a1d76c9.tar.gz`
- Creation: `git archive` from the full final commit SHA
- Size: 5,000,363 bytes
- SHA256: `ac2cdec0633ef3a9e798251a7d0cb202146a7b6c2512952fc852a07df1bbdb45`
- Archive entries: 400

이 archive는 working tree를 압축한 것이 아닙니다. 따라서 현재 uncommitted `Report/`
교체 내용, `Submission/` 문서, 로컬 `.env`, private key 및 provisioned raw material은
포함되지 않습니다.

## Repository Contents

Final commit에는 다음 주요 구성이 포함되어 있습니다.

- `app/`: FastAPI, semantic pipeline, canonical data, retriever 및 evidence 구현
- `tests/`: 단위·회귀·PostgreSQL·Neo4j·외부 데이터 contract 테스트
- `alembic/`, `alembic.ini`: PostgreSQL schema migration
- `ontology/`: modular Team Ontology, SHACL 및 runtime mappings
- `scripts/`: ingestion, validation, smoke, deployment 지원 명령
- `Dockerfile`
- `docker-compose.dev.yml`
- `docker-compose.prod.yml`
- `pyproject.toml`
- `uv.lock`
- `requirements.txt`
- `README.md`
- `.env.example`: placeholder-only 환경 변수 예시

## Reproducibility

### Runtime Requirements

- Production assumption: Ubuntu 24.04 LTS
- Container runtime: Docker Engine and Docker Compose v2
- Local Python: Python 3.12
- Package manager: `uv`
- Databases: PostgreSQL and Neo4j

### Dependency Installation

Repository root에서:

```bash
uv sync --all-groups
```

`pyproject.toml`과 `uv.lock`이 authoritative dependency definition입니다.
`requirements.txt`는 pip 기반 환경용 package 목록을 제공합니다.

### Local API Startup

필요한 환경 변수를 별도로 설정한 뒤:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Tests

```bash
uv run python -m pytest
```

PostgreSQL/Neo4j/live HyperCLOVA 테스트에는 해당 격리 환경과 credential이 별도로
필요합니다.

### Production Containers

`Dockerfile`과 `docker-compose.prod.yml`이 production container 정의를 제공합니다.
세부 build, readiness, persistent storage, rollback 절차는 root `README.md`와
`deploy/README.md`에 있습니다.

### Environment Configuration

환경 변수 이름과 placeholder는 `.env.example`에 있습니다. 주요 변수군은 다음과
같습니다.

- `RUNTIME_DATA_VERSION`
- `DATABASE_URL`, PostgreSQL connection settings
- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
- `CLOVASTUDIO_API_KEY`, HyperCLOVA model/timeout settings
- semantic artifact/index paths and versions
- ontology, graph, transformer and snapshot versions
- API runtime timeout, bind and logging settings

실제 값이 있는 `.env`는 source archive와 Docker build context에 포함되지 않습니다.

## Data and Artifact Boundary

Authoritative source workbooks, populated PostgreSQL/Neo4j stores 및 semantic artifacts는
Git source와 별도로 provision됩니다. Source archive는 코드와 재현 가능한 환경 정의를
제공하지만 production 데이터 bundle 자체를 포함하지 않습니다.


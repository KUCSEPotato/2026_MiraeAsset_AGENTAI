from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_docker_build_context_excludes_secrets_and_runtime_data() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text()
    for required in (".env", "*.pem", "*.key", "material", "data/canonical_v2"):
        assert required in dockerignore
    assert "!.env.example" in dockerignore


def test_compose_only_publishes_api_and_persists_state() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    postgres = compose.split("  postgres:", 1)[1].split("  neo4j:", 1)[0]
    neo4j = compose.split("  neo4j:", 1)[1].split("  agent-api:", 1)[0]
    api = compose.split("  agent-api:", 1)[1].split("\nvolumes:\n", 1)[0]

    assert "ports:" not in postgres
    assert "ports:" not in neo4j
    assert "ports:" in api
    assert "postgres-data:/var/lib/postgresql/data" in postgres
    assert "neo4j-data:/data" in neo4j
    assert "read_only: true" in api
    assert "SEMANTIC_ARTIFACT_ROOT" in api


def test_compose_keeps_runtime_readiness_and_config_driven_rollback() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "RUNTIME_DATA_VERSION" in compose
    assert "CANONICAL_V2_SEMANTIC_INDEX_PATH" in compose
    assert "http://127.0.0.1:8000/health" in compose
    assert "--workers\", \"1" in dockerfile
    assert "COPY .env" not in dockerfile

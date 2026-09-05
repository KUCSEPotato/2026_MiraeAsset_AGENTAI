#!/usr/bin/env bash
set -euo pipefail

code_sha="${1:?code SHA is required}"
image_ref="${2:?immutable image reference is required}"
artifact_release_id="${3:?artifact release id is required}"
frontend_image_ref="${4:?frontend image reference is required}"
base=/opt/mirae-agent
code_release_id="code-$code_sha"
code_release_dir="$base/releases/$code_release_id"
app_dir="$code_release_dir/app"
artifact_release_dir="$base/releases/$artifact_release_id"
artifact_dir="$artifact_release_dir"
incoming="$base/incoming/$artifact_release_id.tar"
environment_file="$base/.env"
previous=""

case "$artifact_release_id" in
  *[!A-Za-z0-9._-]*|'') echo "invalid artifact release id" >&2; exit 2 ;;
esac
case "$code_sha" in
  *[!0-9a-f]*|'') echo "invalid git SHA" >&2; exit 2 ;;
esac
test "${#code_sha}" = 40

test -f "$incoming"
test -f "$incoming.sha256"
test -f "$environment_file"
test -f "$app_dir/docker-compose.prod.yml"
test -f "$artifact_dir/release.json"
(cd "$(dirname "$incoming")" && sha256sum -c "$(basename "$incoming").sha256")

if test -L "$base/current"; then
  previous="$(readlink -f "$base/current")"
fi

image_name="${image_ref%:*}"
image_tag="${image_ref##*:}"
test "$image_tag" = "$code_sha"
test "${frontend_image_ref##*:}" = "$code_sha"

export FRONTEND_IMAGE="${frontend_image_ref%:*}"
export FRONTEND_IMAGE_TAG="${frontend_image_ref##*:}"
export AGENT_IMAGE="$image_name"
export AGENT_IMAGE_TAG="$image_tag"
export APP_GIT_COMMIT="$code_sha"
export ARTIFACT_RELEASE_ID="$artifact_release_id"
export SEMANTIC_ARTIFACT_ROOT="$artifact_dir"

rollback() {
  trap - ERR
  previous_state="$previous/deployment-state"
  if test -n "$previous" && test -f "$previous_state"; then
    IFS=' ' read -r previous_code_sha previous_image_ref previous_artifact_release_id previous_frontend_image_ref < "$previous_state"
    export AGENT_IMAGE="${previous_image_ref%:*}"
    export AGENT_IMAGE_TAG="${previous_image_ref##*:}"
    export APP_GIT_COMMIT="$previous_code_sha"
    export ARTIFACT_RELEASE_ID="$previous_artifact_release_id"
    export SEMANTIC_ARTIFACT_ROOT="$base/releases/$previous_artifact_release_id"
    if test -n "$previous_frontend_image_ref"; then
      export FRONTEND_IMAGE="${previous_frontend_image_ref%:*}"
      export FRONTEND_IMAGE_TAG="${previous_frontend_image_ref##*:}"
      docker compose --env-file "$environment_file" \
        -f "$previous/app/docker-compose.prod.yml" up -d --no-deps agent-api frontend
    else
      # A pre-split release owns the public port on its API container.
      docker compose --env-file "$environment_file" \
        -f "$app_dir/docker-compose.prod.yml" stop frontend
      docker compose --env-file "$environment_file" \
        -f "$previous/app/docker-compose.prod.yml" up -d --no-deps agent-api
    fi
    ln -sfn "$previous" "$base/current"
    for attempt in $(seq 1 20); do
      if curl --fail --silent --show-error http://127.0.0.1:8000/live >/dev/null \
        && curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null; then
        echo "rollback restored $previous_code_sha" >&2
        return 0
      fi
      sleep 5
    done
    echo "rollback health verification failed" >&2
    return 1
  fi
  echo "no previous known-good code release is available for rollback" >&2
}
docker compose --env-file "$environment_file" \
  -f "$app_dir/docker-compose.prod.yml" pull agent-api frontend
docker compose --env-file "$environment_file" \
  -f "$app_dir/docker-compose.prod.yml" up -d postgres neo4j

# Validate the selected release with the new image before replacing the live API.
# A bad artifact must not interrupt the currently healthy application.
docker compose --env-file "$environment_file" \
  -f "$app_dir/docker-compose.prod.yml" run --rm --no-deps -T \
  --entrypoint python agent-api -c '
import asyncio
from app.main import app

async def check():
    async with app.router.lifespan_context(app):
        print("production runtime preflight passed")

asyncio.run(check())
'

# Validate the frontend image without publishing its port.
docker compose --env-file "$environment_file" \
  -f "$app_dir/docker-compose.prod.yml" run --rm --no-deps -T frontend -t

trap rollback ERR
docker compose --env-file "$environment_file" \
  -f "$app_dir/docker-compose.prod.yml" up -d --no-deps agent-api
# The API releases the old public port before frontend acquires it.
docker compose --env-file "$environment_file" \
  -f "$app_dir/docker-compose.prod.yml" up -d --no-deps frontend

for attempt in $(seq 1 40); do
  if curl --fail --silent --show-error http://127.0.0.1:8000/live >/dev/null \
    && curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null; then
    break
  fi
  if test "$attempt" = 40; then
    echo "deployment health gate failed" >&2
    # Explicit exit bypasses ERR traps; fail a command so rollback runs.
    python3 "$app_dir/scripts/deployment_diagnostics.py" || true
    false
  fi
  sleep 5
done

for path in / /chat /assets/app.js /assets/styles.css /assets/logo.png /assets/ory.png; do
  curl --fail --silent --show-error "http://127.0.0.1:8000$path" >/dev/null
done

smoke_response="$(curl --fail --silent --show-error --get \
  http://127.0.0.1:8000/answer \
  --data-urlencode 'question_id=deployment-smoke' \
  --data-urlencode 'question=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘')"
printf '%s' "$smoke_response" | docker compose \
  --env-file "$environment_file" -f "$app_dir/docker-compose.prod.yml" \
  exec -T agent-api python -c 'import json,sys; value=json.load(sys.stdin); expected={"question_id","question","retrieved_context","think_trace","answer"}; assert set(value)==expected and all(isinstance(value[key],str) for key in expected)'

printf '%s %s %s %s\n' "$code_sha" "$image_ref" "$artifact_release_id" "$frontend_image_ref" \
  > "$code_release_dir/deployment-state"
ln -sfn "$code_release_dir" "$base/current"
printf '%s\n' "$code_sha $image_ref $artifact_release_id $frontend_image_ref" \
  > "$code_release_dir/promotion-record.txt"
trap - ERR

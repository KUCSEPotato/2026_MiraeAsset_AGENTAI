#!/usr/bin/env bash
set -Eeuo pipefail

code_sha="${1:?code SHA is required}"
image_ref="${2:?immutable image reference is required}"
artifact_release_id="${3:?artifact release id is required}"
frontend_image_ref="${4:?frontend image reference is required}"
base=/opt/mirae-agent
release_base="$base/releases"
code_release_id="code-$code_sha"
code_release_dir="$release_base/$code_release_id"
app_dir="$code_release_dir/app"
artifact_dir="$release_base/$artifact_release_id"
incoming="$base/incoming/$artifact_release_id.tar"
environment_file="$base/.env"
release_environment_file="$code_release_dir/deployment.env"
previous=""
api_container_id=""
frontend_container_id=""
deployment_mutated=0

case "$artifact_release_id" in
  *[!A-Za-z0-9._-]*|'') echo "invalid artifact release id" >&2; exit 2 ;;
esac
case "$code_sha" in
  *[!0-9a-f]*|'') echo "invalid git SHA" >&2; exit 2 ;;
esac
for selected_image_ref in "$image_ref" "$frontend_image_ref"; do
  case "$selected_image_ref" in
    *[!A-Za-z0-9./:_-]*|'') echo "invalid image reference" >&2; exit 2 ;;
  esac
  test "${selected_image_ref##*:}" = "$code_sha"
done
test "${#code_sha}" = 40
test "${image_ref%:*}" != "$image_ref"
test "${frontend_image_ref%:*}" != "$frontend_image_ref"

test -f "$incoming"
test -f "$incoming.sha256"
test -f "$environment_file"
test -f "$app_dir/docker-compose.prod.yml"
test -f "$artifact_dir/release.json"
(cd "$(dirname "$incoming")" && sha256sum -c "$(basename "$incoming").sha256")

if test -L "$base/current"; then
  previous="$(readlink -f "$base/current")"
fi

write_release_environment() {
  local target="$1"
  local selected_image_ref="$2"
  local selected_code_sha="$3"
  local selected_artifact_release_id="$4"
  local selected_frontend_image_ref="${5:-}"
  local selected_image_name="${selected_image_ref%:*}"
  local selected_image_tag="${selected_image_ref##*:}"
  local temporary="$target.tmp"

  test "$selected_image_tag" = "$selected_code_sha"
  if test -n "$selected_frontend_image_ref"; then
    test "${selected_frontend_image_ref##*:}" = "$selected_code_sha"
  fi
  umask 077
  {
    printf 'AGENT_IMAGE=%s\n' "$selected_image_name"
    printf 'AGENT_IMAGE_TAG=%s\n' "$selected_image_tag"
    printf 'APP_GIT_COMMIT=%s\n' "$selected_code_sha"
    printf 'ARTIFACT_RELEASE_ID=%s\n' "$selected_artifact_release_id"
    printf 'SEMANTIC_ARTIFACT_ROOT=%s\n' \
      "$release_base/$selected_artifact_release_id"
    if test -n "$selected_frontend_image_ref"; then
      printf 'FRONTEND_IMAGE=%s\n' "${selected_frontend_image_ref%:*}"
      printf 'FRONTEND_IMAGE_TAG=%s\n' "${selected_frontend_image_ref##*:}"
    fi
  } > "$temporary"
  mv "$temporary" "$target"
}

compose() {
  local compose_file="$1"
  local release_env="$2"
  shift 2
  docker compose \
    --env-file "$environment_file" \
    --env-file "$release_env" \
    -f "$compose_file" "$@"
}

write_release_environment \
  "$release_environment_file" "$image_ref" "$code_sha" \
  "$artifact_release_id" "$frontend_image_ref"

deployment_preflight() {
  local selected_image_ref="$1"
  local selected_code_sha="$2"
  local selected_artifact_release_id="$3"
  local selected_release_env="$4"
  local selected_artifact_dir="$release_base/$selected_artifact_release_id"

  docker run --rm --read-only --network none \
    --env-file "$environment_file" \
    --env-file "$selected_release_env" \
    --mount \
      "type=bind,source=$selected_artifact_dir,target=/var/lib/financial-semantic-agent,readonly" \
    --entrypoint python "$selected_image_ref" \
    -m app.deployment.consistency preflight \
    --code-sha "$selected_code_sha" \
    --image-ref "$selected_image_ref" \
    --host-artifact-root "$selected_artifact_dir" \
    --release-base "$release_base"
}

health_ready() {
  local compose_file="$1"
  local release_env="$2"
  local health_response

  curl --fail --silent --show-error http://127.0.0.1:8000/live >/dev/null
  health_response="$(
    curl --fail --silent --show-error http://127.0.0.1:8000/health
  )"
  printf '%s' "$health_response" | compose "$compose_file" "$release_env" \
    exec -T agent-api python -m app.deployment.consistency health >/dev/null
}

inspect_api_release() {
  local compose_file="$1"
  local release_env="$2"
  local expected_image_ref="$3"
  local expected_artifact_root="$4"
  local expected_code_sha="${expected_image_ref##*:}"
  local expected_artifact_release_id="${expected_artifact_root##*/}"
  local expected_image_id actual_image_id actual_image_ref actual_artifact_root

  api_container_id="$(compose "$compose_file" "$release_env" ps -q agent-api)"
  test -n "$api_container_id"
  expected_image_id="$(docker image inspect --format '{{.Id}}' "$expected_image_ref")"
  actual_image_id="$(docker inspect --format '{{.Image}}' "$api_container_id")"
  actual_image_ref="$(docker inspect --format '{{.Config.Image}}' "$api_container_id")"
  actual_artifact_root="$(
    docker inspect --format \
      '{{range .Mounts}}{{if eq .Destination "/var/lib/financial-semantic-agent"}}{{.Source}}{{end}}{{end}}' \
      "$api_container_id"
  )"
  test "$actual_image_ref" = "$expected_image_ref"
  test "$actual_image_id" = "$expected_image_id"
  test "$actual_artifact_root" = "$expected_artifact_root"
  docker exec "$api_container_id" python -c \
    'import os,sys; code_sha,release_id=sys.argv[1:]; assert os.environ.get("APP_GIT_COMMIT")==code_sha; assert os.environ.get("AGENT_IMAGE_TAG")==code_sha; assert os.environ.get("ARTIFACT_RELEASE_ID")==release_id' \
    "$expected_code_sha" "$expected_artifact_release_id"
}

inspect_frontend_release() {
  local compose_file="$1"
  local release_env="$2"
  local expected_image_ref="$3"
  local expected_image_id actual_image_id actual_image_ref

  frontend_container_id="$(compose "$compose_file" "$release_env" ps -q frontend)"
  test -n "$frontend_container_id"
  expected_image_id="$(docker image inspect --format '{{.Id}}' "$expected_image_ref")"
  actual_image_id="$(docker inspect --format '{{.Image}}' "$frontend_container_id")"
  actual_image_ref="$(docker inspect --format '{{.Config.Image}}' "$frontend_container_id")"
  test "$actual_image_ref" = "$expected_image_ref"
  test "$actual_image_id" = "$expected_image_id"
}

wait_until_ready() {
  local compose_file="$1"
  local release_env="$2"
  local attempts="$3"

  for attempt in $(seq 1 "$attempts"); do
    if health_ready "$compose_file" "$release_env"; then
      return 0
    fi
    if test "$attempt" = "$attempts"; then
      echo "deployment health gate failed" >&2
      return 1
    fi
    sleep 5
  done
}

rollback() {
  local previous_state previous_code_sha previous_image_ref
  local previous_artifact_release_id previous_frontend_image_ref
  local previous_release_env previous_compose

  if test -z "$previous"; then
    echo "no previous known-good code release is available for rollback" >&2
    return 1
  fi
  previous_state="$previous/deployment-state"
  if ! test -f "$previous_state"; then
    echo "previous deployment state is unavailable for rollback" >&2
    return 1
  fi

  IFS=' ' read -r previous_code_sha previous_image_ref \
    previous_artifact_release_id previous_frontend_image_ref < "$previous_state"
  previous_release_env="$previous/deployment.env"
  previous_compose="$previous/app/docker-compose.prod.yml"
  write_release_environment \
    "$previous_release_env" "$previous_image_ref" "$previous_code_sha" \
    "$previous_artifact_release_id" "$previous_frontend_image_ref"
  test -f "$release_base/$previous_artifact_release_id/release.json"

  if test -z "$previous_frontend_image_ref"; then
    # A pre-split release owns the public port on its API container.
    compose "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
      stop frontend
  fi
  compose "$previous_compose" "$previous_release_env" \
    up -d --no-deps --force-recreate agent-api
  inspect_api_release \
    "$previous_compose" "$previous_release_env" "$previous_image_ref" \
    "$release_base/$previous_artifact_release_id"
  if test -n "$previous_frontend_image_ref"; then
    compose "$previous_compose" "$previous_release_env" \
      up -d --no-deps --force-recreate frontend
    inspect_frontend_release \
      "$previous_compose" "$previous_release_env" \
      "$previous_frontend_image_ref"
  fi
  if ! wait_until_ready "$previous_compose" "$previous_release_env" 20; then
    echo "rollback health verification failed" >&2
    return 1
  fi
  ln -sfn "$previous" "$base/current"
  echo "rollback restored $previous_code_sha" >&2
}

deployment_failed() {
  local status="$?"
  trap - ERR
  if test -n "$api_container_id"; then
    python3 "$app_dir/scripts/deployment_diagnostics.py" \
      "$api_container_id" >&2 || true
  fi
  if test -n "$frontend_container_id"; then
    python3 "$app_dir/scripts/deployment_diagnostics.py" \
      "$frontend_container_id" >&2 || true
  fi
  if test "$deployment_mutated" = 1; then
    rollback || true
  fi
  exit "$status"
}
trap deployment_failed ERR

compose "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  pull agent-api frontend
deployment_preflight \
  "$image_ref" "$code_sha" "$artifact_release_id" "$release_environment_file"

compose "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  up -d --no-recreate postgres neo4j

# Exercise the complete target-image startup path without replacing the live
# API. Artifact-only preflight above remains independent of DB/graph state.
compose "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  run --rm --no-deps -T --entrypoint python agent-api -c '
import asyncio
from app.main import app

async def check():
    async with app.router.lifespan_context(app):
        print("production runtime preflight passed")

asyncio.run(check())
'

# Validate the frontend image without publishing its port.
compose "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  run --rm --no-deps -T frontend -t

deployment_mutated=1
compose "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  up -d --no-deps --force-recreate agent-api
inspect_api_release \
  "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  "$image_ref" "$artifact_dir"

# The API no longer owns the public port; replace the proxy only after its
# upstream has passed image, mount, and runtime identity inspection.
compose "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  up -d --no-deps --force-recreate frontend
inspect_frontend_release \
  "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  "$frontend_image_ref"
wait_until_ready \
  "$app_dir/docker-compose.prod.yml" "$release_environment_file" 40

for path in / /chat /assets/app.js /assets/styles.css /assets/logo.png /assets/ory.png; do
  curl --fail --silent --show-error "http://127.0.0.1:8000$path" >/dev/null
done

smoke_response="$(curl --fail --silent --show-error --get \
  http://127.0.0.1:8000/answer \
  --data-urlencode 'question_id=deployment-smoke' \
  --data-urlencode 'question=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘')"
printf '%s' "$smoke_response" | compose \
  "$app_dir/docker-compose.prod.yml" "$release_environment_file" \
  exec -T agent-api python -c 'import json,sys; value=json.load(sys.stdin); expected={"question_id","question","retrieved_context","think_trace","answer"}; assert set(value)==expected and all(isinstance(value[key],str) for key in expected)'

printf '%s %s %s %s\n' \
  "$code_sha" "$image_ref" "$artifact_release_id" "$frontend_image_ref" \
  > "$code_release_dir/deployment-state"
ln -sfn "$code_release_dir" "$base/current"
printf '%s\n' \
  "$code_sha $image_ref $artifact_release_id $frontend_image_ref" \
  > "$code_release_dir/promotion-record.txt"
trap - ERR

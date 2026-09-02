#!/usr/bin/env bash
set -euo pipefail

release_id="${1:?release id is required}"
image_ref="${2:?immutable image reference is required}"
git_sha="${3:?git SHA is required}"
base=/opt/mirae-agent
release_dir="$base/releases/$release_id"
app_dir="$release_dir/app"
artifact_dir="$release_dir/artifacts"
incoming="$base/incoming/$release_id.tar"
environment_file="$base/env/production.env"
previous=""

case "$release_id" in
  *[!A-Za-z0-9._-]*|'') echo "invalid release id" >&2; exit 2 ;;
esac
case "$git_sha" in
  *[!0-9a-f]*|'') echo "invalid git SHA" >&2; exit 2 ;;
esac

test -f "$incoming"
test -f "$incoming.sha256"
test -f "$environment_file"
mkdir -p "$artifact_dir"
(cd "$(dirname "$incoming")" && sha256sum -c "$(basename "$incoming").sha256")
tar -xf "$incoming" -C "$artifact_dir"
test -f "$artifact_dir/release.json"
ln -sfn release.json "$artifact_dir/production-artifacts.json"

if test -L "$base/current"; then
  previous="$(readlink "$base/current")"
fi

image_name="${image_ref%:*}"
image_tag="${image_ref##*:}"
test "$image_tag" = "$git_sha"

export AGENT_IMAGE="$image_name"
export AGENT_IMAGE_TAG="$image_tag"
export SEMANTIC_ARTIFACT_ROOT="$artifact_dir"

rollback() {
  if test -n "$previous" && test -d "$previous/app"; then
    ln -sfn "$previous" "$base/current"
    docker compose --env-file "$environment_file" -f "$previous/app/docker-compose.prod.yml" up -d
  fi
}
trap rollback ERR

docker compose --env-file "$environment_file" -f "$app_dir/docker-compose.prod.yml" pull agent-api
docker compose --env-file "$environment_file" -f "$app_dir/docker-compose.prod.yml" up -d

for attempt in $(seq 1 40); do
  if curl --fail --silent --show-error http://127.0.0.1:8000/live >/dev/null \
    && curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null; then
    break
  fi
  if test "$attempt" = 40; then
    echo "deployment health gate failed" >&2
    exit 1
  fi
  sleep 5
done

curl --fail --silent --show-error --get http://127.0.0.1:8000/answer \
  --data-urlencode 'question_id=deployment-smoke' \
  --data-urlencode '질문=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘' >/dev/null

ln -sfn "$release_dir" "$base/current"
printf '%s\n' "$release_id $git_sha $image_ref" > "$release_dir/promotion-record.txt"
trap - ERR

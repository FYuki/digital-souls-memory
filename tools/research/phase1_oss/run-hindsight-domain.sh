#!/usr/bin/env bash
# このrunが作成したコンテナだけを削除する。既存サービス・外部モデルに接続しない。
set -euo pipefail
repo_root=$(cd "$(dirname "$0")/../../.." && pwd)
probe_root=${OSS_PROBE_ROOT:-/tmp/phase1-oss-reassessment}
pg_image=${OSS_PROBE_POSTGRES_IMAGE:-memory-retrieval-postgres@sha256:52000025fc027d9c9ca22ba99e4c4e0409efb9a23e773ab096a43da81a648454}
test "$(git -C "$probe_root/hindsight" rev-parse HEAD)" = f29ba70c825094c839daaf27b2b5a564de46fb4e
git -C "$probe_root/hindsight" diff --exit-code --quiet HEAD --
socket_dir=$(mktemp -d "$probe_root/domain-socket.XXXXXXXX")
container_name=phase1-hindsight-domain-$(id -u)-$$
created=false
cleanup() {
  if "$created"; then docker rm --force "$container_name" >/dev/null; fi
  rm -f -- "$socket_dir/.s.PGSQL.5432" "$socket_dir/.s.PGSQL.5432.lock"
  rmdir -- "$socket_dir"
}
trap cleanup EXIT
docker run --detach --pull never --name "$container_name" --network none --memory 1g \
  --user "$(id -u):$(id -g)" \
  --tmpfs "/var/lib/postgresql:rw,size=512m,uid=$(id -u),gid=$(id -g),mode=0700" \
  --mount "type=bind,src=$socket_dir,dst=/var/run/postgresql" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=oss_probe "$pg_image" \
  postgres -c listen_addresses= -c max_connections=40 >/dev/null
created=true
ready=false
for attempt in {1..50}; do
  if docker exec "$container_name" sh -c 'test "$(cat /proc/1/comm)" = postgres' \
    && docker exec "$container_name" pg_isready -q -U postgres -d oss_probe; then
    ready=true
    break
  fi
  sleep 1
done
if ! "$ready"; then docker logs --tail 30 "$container_name"; exit 1; fi
export OSS_PROBE_ROOT="$probe_root" OSS_PROBE_SOCKET="$socket_dir"
export OSS_PROBE_POSTGRES_IMAGE="$pg_image"
export OSS_DOMAIN_OUTPUT=${OSS_DOMAIN_OUTPUT:-$probe_root/hindsight-domain-results.json}
cd "$repo_root"
"$probe_root/venv/bin/python" tools/research/phase1_oss/probe_hindsight_domain.py

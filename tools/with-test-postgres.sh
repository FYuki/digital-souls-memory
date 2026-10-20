#!/usr/bin/env bash
# Synthetic disposable DB only; no published ports and no container networking.
set -euo pipefail
repo_root=$(cd "$(dirname "$0")/.." && pwd)
image=$(docker compose -f "$repo_root/compose.yml" config --no-interpolate --images)
fixture_dir=$(mktemp -d /tmp/memory-pg.XXXXXXXX)
container_name="memory-pg-${fixture_dir##*.}"
cleanup() {
  docker rm --force "$container_name" >/dev/null 2>&1 || true
  rm -f -- "$fixture_dir/socket/.s.PGSQL.5432" "$fixture_dir/socket/.s.PGSQL.5432.lock" || true
  rmdir -- "$fixture_dir/socket" "$fixture_dir" || true
}
trap cleanup EXIT
mkdir -m 700 "$fixture_dir/socket"
docker run --detach --name "$container_name" --network none --cpus 1 --memory 512m \
  --user "$(id -u):$(id -g)" \
  --tmpfs "/var/lib/postgresql:uid=$(id -u),gid=$(id -g),mode=0700" \
  --mount "type=bind,source=$fixture_dir/socket,target=/var/run/postgresql" \
  --env POSTGRES_HOST_AUTH_METHOD=trust --env POSTGRES_DB=memory_synthetic \
  --env POSTGRES_USER=memory_synthetic "$image" \
  postgres -c listen_addresses= -c unix_socket_permissions=0700 >/dev/null
ready=false
for attempt in {1..50}; do
  if docker exec "$container_name" sh -c 'test "$(cat /proc/1/comm)" = postgres' \
    && docker exec "$container_name" pg_isready -q -U memory_synthetic -d memory_synthetic; then
    ready=true
    break
  fi
  sleep 1
done
if [ "$ready" != true ]; then
  docker logs "$container_name"
  exit 1
fi
export MEMORY_TEST_DATABASE_URL="postgresql://memory_synthetic@/memory_synthetic?host=$fixture_dir/socket"
if [ "$#" -eq 0 ]; then
  set -- uv run --no-sync pytest -m postgres -q
fi
cd "$repo_root"
"$@"

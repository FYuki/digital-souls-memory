#!/usr/bin/env bash
# Research fixtures only. Never connect to the application's database or model.
set -euo pipefail
repo_root=$(cd "$(dirname "$0")/../../.." && pwd)
probe_root=${OSS_PROBE_ROOT:-/tmp/phase1-oss-reassessment}
export OSS_PROBE_ROOT="$probe_root"
pg_image=${OSS_PROBE_POSTGRES_IMAGE:-memory-retrieval-postgres@sha256:52000025fc027d9c9ca22ba99e4c4e0409efb9a23e773ab096a43da81a648454}
neo_image=${OSS_PROBE_NEO4J_IMAGE:-neo4j@sha256:f1e52d4dc6da122468c4a3c1050a4f5e117f6d3268625237c34d49166f0d6082}
test -x "$probe_root/venv/bin/python"
test -x "$probe_root/retrieval-venv/bin/python"
python3 - "$repo_root/tools/research/phase1_oss/sources.lock.json" "$probe_root" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

lock = json.loads(Path(sys.argv[1]).read_text())
for name, source in lock["sources"].items():
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(sys.argv[2]) / name, text=True
    ).strip()
    if actual != source["commit"]:
        raise SystemExit(f"Source mismatch: {name}: {actual}")
    subprocess.run(
        ["git", "diff", "--exit-code", "--quiet", "HEAD", "--"],
        cwd=Path(sys.argv[2]) / name, check=True,
    )
print("PASS: source commits match lock; tracked upstream code is unmodified")
PY
socket_dir=$(mktemp -d "$probe_root/socket.XXXXXXXX")
export OSS_PROBE_SOCKET="$socket_dir"
pg_created=false
neo_created=false
cleanup() {
  if "$neo_created"; then docker rm --force phase1-oss-probe-neo4j >/dev/null; fi
  if "$pg_created"; then docker rm --force phase1-oss-probe-pg >/dev/null; fi
  rm -f -- "$socket_dir/.s.PGSQL.5432" "$socket_dir/.s.PGSQL.5432.lock"
  rmdir -- "$socket_dir"
}
trap cleanup EXIT
docker run --detach --pull never --name phase1-oss-probe-pg --network none --memory 1g \
  --user "$(id -u):$(id -g)" \
  --tmpfs "/var/lib/postgresql:rw,size=512m,uid=$(id -u),gid=$(id -g),mode=0700" \
  --mount "type=bind,src=$socket_dir,dst=/var/run/postgresql" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=oss_probe \
  "$pg_image" \
  postgres -c listen_addresses= -c max_connections=40 >/dev/null
pg_created=true
docker run --detach --pull never --name phase1-oss-probe-neo4j \
  --publish 127.0.0.1:18957:7687 --memory 1g \
  --tmpfs /data:rw,size=512m --tmpfs /logs:rw,size=64m \
  -e NEO4J_AUTH=none -e NEO4J_dbms_usage__report_enabled=false \
  -e NEO4J_db_tx__log_preallocate=false -e NEO4J_db_tx__log_rotation_size=10m \
  -e NEO4J_server_memory_heap_initial__size=128m \
  -e NEO4J_server_memory_heap_max__size=256m \
  -e NEO4J_server_memory_pagecache_size=128m "$neo_image" >/dev/null
neo_created=true
ready=false
for attempt in {1..50}; do
  if docker exec phase1-oss-probe-pg sh -c 'test "$(cat /proc/1/comm)" = postgres' \
    && docker exec phase1-oss-probe-pg pg_isready -q -U postgres -d oss_probe \
    && docker exec phase1-oss-probe-neo4j cypher-shell -a bolt://localhost:7687 'RETURN 1' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if ! "$ready"; then
  docker logs --tail 30 phase1-oss-probe-pg
  docker logs --tail 30 phase1-oss-probe-neo4j
  exit 1
fi
cd "$repo_root"
"$probe_root/venv/bin/python" tools/research/phase1_oss/probe_hindsight.py
"$probe_root/retrieval-venv/bin/python" tools/research/phase1_oss/probe_graphiti.py
"$probe_root/retrieval-venv/bin/python" tools/research/phase1_oss/probe_mem0.py
node --experimental-vm-modules tools/research/phase1_oss/probe_extensions.mjs
node "$probe_root/smart-memory/test-harness.mjs" --parsers --quiet

#!/usr/bin/env bash
set -euo pipefail
test -f compose.core-st.yml
mode=${1:-functional}
evidence_dir=${MEMORY_CORE_ST_EVIDENCE_DIR:-$(mktemp -d /tmp/memory-core-st-evidence.XXXXXXXX)}
mkdir -p "$evidence_dir"
evidence_dir=$(realpath "$evidence_dir")
export MEMORY_CORE_ST_EVIDENCE_DIR="$evidence_dir"
export BUILDX_CONFIG=${BUILDX_CONFIG:-/tmp/memory-core-st-buildx}
project="memory-core-st-$(date +%s)-$$"
compose=(docker compose -p "$project" -f compose.core-st.yml)
if [ "$mode" = performance ]; then
  test -s "$evidence_dir/manifest.json"
  : "${MEMORY_EMBEDDING_ENDPOINT:?required}"
  : "${MEMORY_RERANKER_ENDPOINT:?required}"
  compose+=(-f compose.core-st.performance.yml)
elif [ "$mode" != functional ]; then
  echo "Expected functional or performance" >&2
  exit 2
fi
cleanup() {
  status=$?
  trap - EXIT INT TERM
  "${compose[@]}" logs --no-color > "$evidence_dir/compose.log" 2>&1 || status=1
  "${compose[@]}" down --volumes --remove-orphans > "$evidence_dir/cleanup.log" 2>&1 || status=1
  remaining=$(docker ps -aq --filter "label=com.docker.compose.project=$project")
  networks=$(docker network ls -q --filter "label=com.docker.compose.project=$project")
  volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$project")
  if [ -n "$remaining$networks$volumes" ]; then
    status=1
    echo "Dedicated ST resources remain" >> "$evidence_dir/cleanup.log"
  fi
  printf 'exit_status=%s\nproject=%s\n' "$status" "$project" > "$evidence_dir/result.txt"
  echo "ST evidence: $evidence_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
git rev-parse HEAD > "$evidence_dir/revision.txt"
git diff --stat > "$evidence_dir/diff-stat.txt"
git ls-files --others --exclude-standard > "$evidence_dir/untracked-files.txt"
git ls-files -z --modified --others --exclude-standard |
  sort -zu | xargs -0 -r sha256sum > "$evidence_dir/source-sha256.txt"
date -u +%Y-%m-%dT%H:%M:%SZ > "$evidence_dir/started-at.txt"
printf 'bash tools/with-core-st.sh %s\n' "$mode" > "$evidence_dir/command.txt"
"${compose[@]}" config --no-interpolate --quiet
"${compose[@]}" build > "$evidence_dir/build.log" 2>&1
"${compose[@]}" up -d --wait memory > "$evidence_dir/start.log" 2>&1
"${compose[@]}" run --no-deps --rm runner > "$evidence_dir/tests.log" 2>&1

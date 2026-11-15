#!/usr/bin/env bash
# Print what the running Compose stack looks like from the outside: the database settings
# that depend on the size of the server, the memory of each container and of Docker itself.
# With "check" as the first argument it also fails when a container was killed for
# exceeding its memory limit or was restarted by Docker.
set -euo pipefail

sql() { docker compose exec -T db psql -U fxlab -d fxlab -Atc "$1"; }

echo "database settings:"
for name in shared_buffers max_connections max_worker_processes \
  timescaledb.max_background_workers timescaledb.telemetry_level; do
  printf '  %s = %s\n' "$name" "$(sql "show $name")"
done
echo "init scripts the database container saw at its first start:"
docker compose exec -T db ls /docker-entrypoint-initdb.d | sed 's/^/  /'

mapfile -t ids < <(docker compose ps -q)
echo "containers (memory in use / limit):"
docker stats --no-stream --format '  {{.Name}}: {{.MemUsage}}   cpu {{.CPUPerc}}' "${ids[@]}"
bad=0
for id in "${ids[@]}"; do
  state="$(docker inspect -f '{{.Name}} oom_killed={{.State.OOMKilled}} restarts={{.RestartCount}}' "$id")"
  echo "  $state"
  case "$state" in
    *"oom_killed=false restarts=0") ;;
    *) bad=1 ;;
  esac
done

echo "Docker engine, resident memory:"
ps -eo rss=,comm= | awk '
  $2 ~ /^(dockerd|containerd|containerd-shim|docker-proxy)/ { part[$2] += $1; total += $1 }
  END {
    for (name in part) printf "  %s: %d MB\n", name, part[name] / 1024
    printf "  together: %d MB\n", total / 1024
  }'
echo "host:"
free -m | sed 's/^/  /'

if [ "${1:-}" = "check" ] && [ "$bad" -ne 0 ]; then
  echo "ERROR: a container ran out of memory or was restarted (see above)" >&2
  exit 1
fi

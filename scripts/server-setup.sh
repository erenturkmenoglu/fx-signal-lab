#!/usr/bin/env bash
# FX Signal Lab: first-time setup on a fresh Ubuntu or Debian server.
#
#   ./scripts/server-setup.sh
#
# It installs Docker when it is missing, writes .env (asking for the Massive API key
# without echoing it), builds and starts the stack and waits until the application
# answers.  Running it again is safe: an existing .env is kept and nothing is deleted.
#
# Non-interactive use:  MASSIVE_API_KEY=... ./scripts/server-setup.sh
set -euo pipefail

cd "$(dirname "$0")/.."

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

if [ ! -f docker-compose.yml ] || [ ! -f .env.example ]; then
  fail "run this from the fx-signal-lab folder"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "this script needs root or sudo"
  SUDO="sudo"
fi

# ----------------------------------------------------------------------------- 1. Docker
say "1/4  Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  note "already installed: $(docker --version)"
else
  command -v curl >/dev/null 2>&1 || fail "curl is missing; install it with: $SUDO apt-get install -y curl"
  note "installing Docker Engine with the official script (get.docker.com)"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  $SUDO sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
fi

DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  DOCKER="$SUDO docker"
  $DOCKER info >/dev/null 2>&1 || fail "the Docker daemon is not running (try: $SUDO systemctl start docker)"
  if [ -n "$SUDO" ] && [ -n "${USER:-}" ]; then
    # so that later commands (make verify, docker compose logs) work without sudo
    if $SUDO usermod -aG docker "$USER" 2>/dev/null; then
      NEEDS_RELOGIN=1
    fi
  fi
fi

if ! command -v make >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  note "installing make"
  $SUDO apt-get install -y -qq make >/dev/null
fi

mem_mb="$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "$mem_mb" -gt 0 ] && [ "$mem_mb" -lt 1800 ]; then
  note "WARNING: only ${mem_mb} MB of memory. The first build may fail; 4 GB is recommended."
fi
if command -v timedatectl >/dev/null 2>&1; then
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = "yes" ]; then
    note "clock: synchronised"
  else
    note "WARNING: the clock is not reported as synchronised. Latency figures depend on it"
    note "         (check with: timedatectl status)."
  fi
fi

# ------------------------------------------------------------------------------- 2. .env
say "2/4  Configuration (.env)"
if [ -f .env ]; then
  note ".env already exists: kept as it is"
else
  key="${MASSIVE_API_KEY:-}"
  if [ -z "$key" ] && [ -t 0 ]; then
    printf '  Massive API key (input stays hidden; Enter alone = start without a key): '
    read -rs key
    printf '\n'
  fi
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  if [ -n "$key" ] && ! printf '%s' "$key" | grep -Eq '^[A-Za-z0-9_-]{8,200}$'; then
    fail "that does not look like an API key (letters, digits, _ and - only)"
  fi
  password="$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32 || true)"
  [ "${#password}" -ge 24 ] || fail "could not generate a database password"

  umask 077
  sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${password}|" \
    -e "s|^MASSIVE_API_KEY=.*|MASSIVE_API_KEY=${key}|" .env.example >.env
  chmod 600 .env
  unset password
  if [ -n "$key" ]; then
    note "written: .env (readable by you only) with your API key and a generated database password"
  else
    note "written: .env without an API key. The application will start and collect nothing."
    note "Add the key later: edit MASSIVE_API_KEY in .env, then: docker compose up -d"
  fi
  unset key
fi

# ------------------------------------------------------------------------- 3. build, start
say "3/4  Build and start (the first build takes a few minutes)"
$DOCKER compose up -d --build

# ------------------------------------------------------------------------------- 4. wait
say "4/4  Waiting for the application"
ready=""
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health/ready >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ -z "$ready" ]; then
  $DOCKER compose ps || true
  $DOCKER compose logs --tail 40 app || true
  fail "the application did not become ready. The log lines above say why."
fi

status="$(curl -fsS --max-time 5 http://127.0.0.1:8000/api/status 2>/dev/null || true)"
label="$(printf '%s' "$status" | grep -o '"label":"[^"]*"' | head -n 1 | cut -d'"' -f4 || true)"
state="$(printf '%s' "$status" | grep -o '"state":"[^"]*"' | head -n 1 | cut -d'"' -f4 || true)"
note "application is up.  data: ${label:-unknown}   feed: ${state:-unknown}"

say "Done"
note "Dashboard on this machine: http://127.0.0.1:8000 (not reachable from outside, on purpose)"
note ""
note "Next:"
if [ "${NEEDS_RELOGIN:-}" = "1" ]; then
  note "  log out and in once so that docker works without sudo (until then: sudo make verify)"
fi
note "  make verify        probes Massive with your key and prints a report"
note "  docs/DEPLOY.md     phone access, updates, backups"

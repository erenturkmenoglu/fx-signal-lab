#!/usr/bin/env bash
# FX Signal Lab: first-time setup on a fresh Ubuntu or Debian server.
#
#   ./scripts/server-setup.sh
#
# It installs Docker when it is missing, adds a swap file on a server with less than 4 GB
# of memory, writes .env (asking for the Massive API key without echoing it), builds and
# starts the stack and waits until the application answers.  Running it again is safe:
# an existing .env and an existing swap file are kept and nothing is deleted.
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

# Memory.  The running stack needs about 1 GB (docs/DEPLOY.md, "Server size").  Below 4 GB
# a swap file makes sure that an image build next to the running stack cannot exhaust it.
SWAP_FILE=/swapfile
SWAP_MB=2048

meminfo_mb() {
  awk -v key="$1:" '$1 == key {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || true
}

add_swap() {
  # Creates the swap file if it is missing, turns it on and keeps it on across reboots.
  # On failure it prints the reason and returns 1; nothing half-made is left behind.
  local created="" free_mb
  if [ ! -e "$SWAP_FILE" ]; then
    free_mb="$(df -Pm / 2>/dev/null | awk 'NR == 2 {print $4}')"
    if [ "${free_mb:-0}" -lt 8000 ]; then
      echo "less than 8 GB of free disk space"
      return 1
    fi
    if ! $SUDO fallocate -l "${SWAP_MB}M" "$SWAP_FILE" 2>/dev/null &&
      ! $SUDO dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_MB" status=none 2>/dev/null; then
      $SUDO rm -f "$SWAP_FILE"
      echo "could not create $SWAP_FILE"
      return 1
    fi
    created=1
    if ! $SUDO chmod 600 "$SWAP_FILE" || ! $SUDO mkswap "$SWAP_FILE" >/dev/null 2>&1; then
      $SUDO rm -f "$SWAP_FILE"
      echo "could not format $SWAP_FILE as swap"
      return 1
    fi
  fi
  if ! $SUDO swapon "$SWAP_FILE" >/dev/null 2>&1 && ! grep -q "^$SWAP_FILE " /proc/swaps 2>/dev/null; then
    if [ -n "$created" ]; then
      $SUDO rm -f "$SWAP_FILE"
    fi
    echo "this system did not accept $SWAP_FILE as swap"
    return 1
  fi
  if ! grep -Eq "^${SWAP_FILE}[[:space:]]" /etc/fstab 2>/dev/null; then
    if ! printf '%s none swap sw 0 0\n' "$SWAP_FILE" | $SUDO tee -a /etc/fstab >/dev/null; then
      echo "swap is on now, but /etc/fstab could not be changed: it will be off after a reboot"
      return 1
    fi
  fi
  # Swap is for emergencies: keep the application and the database in memory.
  if printf 'vm.swappiness = 10\n' | $SUDO tee /etc/sysctl.d/60-fxlab-swap.conf >/dev/null 2>&1; then
    $SUDO sysctl -q -w vm.swappiness=10 >/dev/null 2>&1 || true
  fi
  return 0
}

mem_mb="$(meminfo_mb MemTotal)"
mem_mb="${mem_mb:-0}"
swap_mb="$(meminfo_mb SwapTotal)"
swap_mb="${swap_mb:-0}"
if [ "$mem_mb" -ge 3500 ]; then
  note "memory: ${mem_mb} MB"
elif [ "$mem_mb" -gt 0 ] && [ "$swap_mb" -ge 1000 ]; then
  note "memory: ${mem_mb} MB, swap: ${swap_mb} MB"
elif [ "$mem_mb" -gt 0 ]; then
  if reason="$(add_swap)"; then
    note "memory: ${mem_mb} MB, plus a $((SWAP_MB / 1024)) GB swap file ($SWAP_FILE, kept across reboots),"
    note "so that a build next to the running stack cannot run out of memory."
  else
    note "WARNING: ${mem_mb} MB of memory and no swap file (${reason:-unknown reason})."
    note "         A build while the stack is running may run out of memory."
  fi
fi
if [ "$mem_mb" -gt 0 ] && [ "$mem_mb" -lt 1700 ]; then
  note "WARNING: less than 2 GB of memory. The stack starts, but the system, the database"
  note "         and the application leave almost nothing free. 2 GB is the tested minimum."
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
if [ -n "$status" ]; then
  label="$(printf '%s' "$status" | grep -o '"label":"[^"]*"' | head -n 1 | cut -d'"' -f4 || true)"
  state="$(printf '%s' "$status" | grep -o '"state":"[^"]*"' | head -n 1 | cut -d'"' -f4 || true)"
  note "application is up.  data: ${label:-unknown}   feed: ${state:-unknown}"
else
  note "application is up.  (A dashboard password is set, so its details need a login.)"
fi

say "Done"
note "Dashboard on this machine: http://127.0.0.1:8000 (not reachable from outside, on purpose)"
note ""
note "Next:"
if [ "${NEEDS_RELOGIN:-}" = "1" ]; then
  note "  log out and in once so that docker works without sudo (until then: sudo make verify)"
fi
note "  make verify        probes Massive with your key and prints a report"
note "  make password      sets the dashboard password (needed before any public address)"
note "  docs/DEPLOY.md     access from a phone or any browser, updates, backups"

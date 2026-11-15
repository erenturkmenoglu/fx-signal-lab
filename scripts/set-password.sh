#!/usr/bin/env bash
# FX Signal Lab: set or change the dashboard password.
#
#   ./scripts/set-password.sh            (or: make password)
#
# Asks for the password twice without showing it, turns it into a hash inside the
# application's image and writes only the hash to .env (DASHBOARD_PASSWORD_HASH).  Then it
# restarts the application.  From that moment the dashboard, the API and the live stream
# need a login, and every session that existed has ended.
#
#   Non-interactive:   DASHBOARD_PASSWORD=... ./scripts/set-password.sh
#   Turn the login off: ./scripts/set-password.sh --remove   (private networks only again)
set -euo pipefail

cd "$(dirname "$0")/.."

note() { printf '  %s\n' "$*"; }
fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

if [ ! -f docker-compose.yml ] || [ ! -f .env ]; then
  fail "no .env here: run ./scripts/server-setup.sh first"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  DOCKER="$SUDO docker"
  $DOCKER info >/dev/null 2>&1 || fail "the Docker daemon is not reachable"
fi

write_env() {
  # Replace .env in one step and keep its owner and mode (it holds the API key).
  # $1: the DASHBOARD_PASSWORD_HASH line to add, or nothing to remove it.
  local tmp
  tmp="$(mktemp .env.XXXXXX)"
  chmod --reference=.env "$tmp"
  chown --reference=.env "$tmp" 2>/dev/null || true
  grep -v '^DASHBOARD_PASSWORD_HASH=' .env >"$tmp" || true
  if [ -n "${1:-}" ]; then
    printf '%s\n' "$1" >>"$tmp"
  fi
  mv "$tmp" .env
}

restart() {
  $DOCKER compose up -d app
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  $DOCKER compose logs --tail 20 app || true
  fail "the application did not become ready again. The log lines above say why."
}

if [ "${1:-}" = "--remove" ]; then
  write_env ""
  restart
  note "the login is off. The dashboard is open to everyone who can reach it:"
  note "keep it on a private network (if Tailscale Funnel is on: sudo tailscale funnel reset)."
  exit 0
fi

password="${DASHBOARD_PASSWORD:-}"
if [ -z "$password" ]; then
  if [ ! -t 0 ]; then
    fail "no terminal to ask on (for scripts: DASHBOARD_PASSWORD=... $0)"
  fi
  printf '  New dashboard password, at least 12 characters (input stays hidden): '
  IFS= read -rs password
  printf '\n  Again: '
  IFS= read -rs again
  printf '\n'
  if [ "$password" != "$again" ]; then
    fail "the two entries differ. Nothing was changed."
  fi
  unset again
fi

# The hash is made by the application itself, in a one-off container; the password travels
# on standard input only (never as an argument or an environment variable of that container).
if ! hash="$(printf '%s\n' "$password" | $DOCKER compose run --rm -T --no-deps app python -m fxlab.cli hash-password --stdin)"; then
  fail "the password was not accepted (reason above). Nothing was changed."
fi
unset password
case "$hash" in
  pbkdf2_sha256:*:*:*) ;;
  *) fail "unexpected answer from hash-password. Nothing was changed." ;;
esac

write_env "DASHBOARD_PASSWORD_HASH=${hash}"
unset hash
restart

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/api/status || true)"
if [ "$code" = "401" ]; then
  note "password set. The dashboard, the API and the live stream now need a login;"
  note "sessions that existed have ended. Only a hash of the password is stored (.env)."
else
  fail "the password was written, but the API still answers ${code:-nothing} without a login. Check: docker compose logs app"
fi

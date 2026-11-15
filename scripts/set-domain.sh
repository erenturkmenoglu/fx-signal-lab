#!/usr/bin/env bash
# FX Signal Lab: serve the dashboard on a domain of your own, over HTTPS.
#
#   ./scripts/set-domain.sh signale.example          (or: make domain ARGS=signale.example)
#
# It first checks that a dashboard password is set and that the name points at this server,
# and changes nothing if either is missing.  Then it writes the name to .env
# (DASHBOARD_DOMAIN, and COMPOSE_PROFILES=domain so that Caddy belongs to the stack),
# restarts the application, starts Caddy, which gets the certificate by itself, and waits
# until https://<name> answers.  If that does not happen it says why and puts everything
# back the way it was.
#
#   Only the checks, nothing is changed:   ./scripts/set-domain.sh --check signale.example
#   Turn the domain off again:             ./scripts/set-domain.sh --remove
#   What is set now:                       ./scripts/set-domain.sh
#
# Three things are needed outside this script: the name itself (bought at a registrar), a DNS
# "A" record from the name to this server's public address, and ports 80 and 443 (TCP) open
# in the server's firewall.  docs/DEPLOY.md, "A domain of your own", walks through them.
#
#   FXLAB_PUBLIC_IP=203.0.113.7   this server's public address, if it cannot be looked up
#   --no-dns-check                do not compare the DNS record with that address
#   A name ending in .localhost is for trials: Caddy signs that certificate itself.
set -euo pipefail

cd "$(dirname "$0")/.."

note() { printf '  %s\n' "$*"; }
fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

# seconds to wait for the certificate before giving up (Let's Encrypt usually needs under a minute)
HTTPS_WAIT_S="${FXLAB_HTTPS_WAIT_S:-240}"
HEALTH=/api/health/ready

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }

# ------------------------------------------------------------------------------ arguments
mode="set"
name=""
dns_check=1
while [ $# -gt 0 ]; do
  case "$1" in
    --remove) mode="remove" ;;
    --check) mode="check" ;;
    --no-dns-check) dns_check=0 ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) fail "unknown option $1 (the top of scripts/set-domain.sh lists them)" ;;
    *)
      if [ -n "$name" ]; then
        fail "one name at a time"
      fi
      name="$1"
      ;;
  esac
  shift
done

if [ ! -f docker-compose.yml ] || [ ! -f .env ]; then
  fail "no .env here: run ./scripts/server-setup.sh first"
fi

current="$(env_value DASHBOARD_DOMAIN)"

if [ "$mode" = "remove" ] && [ -n "$name" ]; then
  fail "--remove takes no name: it turns off whatever is set"
fi
if [ "$mode" = "check" ] && [ -z "$name" ]; then
  name="$current"
fi
if [ "$mode" != "remove" ] && [ -z "$name" ]; then
  if [ -n "$current" ]; then
    note "the dashboard is served on https://${current}"
  else
    note "no domain is set"
  fi
  note "set one:     $0 signale.example      (make domain ARGS=signale.example)"
  note "check first: $0 --check signale.example"
  note "turn it off: $0 --remove"
  exit 0
fi

# ----------------------------------------------------------------------------- the name
trial=0
if [ "$mode" != "remove" ]; then
  name="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  name="${name%.}"
  case "$name" in
    http://* | https://*) fail "give the name alone, without https:// in front (like signale.example)" ;;
  esac
  if [ "${#name}" -gt 253 ] ||
    ! [[ "$name" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
    fail "'$name' is not a domain name (expected something like signale.example or fx.signale.example)"
  fi
  case "$name" in
    *.ts.net) fail "$name is a Tailscale name. Tailscale Funnel serves it; this is for a name of your own." ;;
    *.localhost) trial=1 ;;
  esac
fi

# --------------------------------------------------------------------------- the checks
check_password() {
  if [ -z "$(env_value DASHBOARD_PASSWORD_HASH)" ]; then
    fail "no dashboard password is set. With a public address and no password anyone could open the dashboard: run 'make password' first."
  fi
  note "password: set"
}

public_ip() {
  # The address the internet sees this server as.  Prints nothing when it cannot be found.
  local ip url
  if [ -n "${FXLAB_PUBLIC_IP:-}" ]; then
    printf '%s\n' "$FXLAB_PUBLIC_IP"
    return 0
  fi
  for url in https://checkip.amazonaws.com https://icanhazip.com; do
    ip="$(curl -4 -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]')" || continue
    if [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      printf '%s\n' "$ip"
      return 0
    fi
  done
  return 0
}

addresses_of() {
  # The IPv4 addresses of a name, as this server's resolver and a public resolver see them.
  # The second view does not go through this server's DNS cache, which can hold on to
  # "no such name" for a while after a record was created.
  {
    getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' || true
    curl -fsS --max-time 5 "https://dns.google/resolve?name=$1&type=A" 2>/dev/null |
      grep -Eo '"data": *"[0-9]{1,3}(\.[0-9]{1,3}){3}"' | grep -Eo '[0-9]{1,3}(\.[0-9]{1,3}){3}' || true
  } | sort -u
}

check_dns() {
  local mine seen
  if [ "$trial" -eq 1 ]; then
    note "$name is a trial name: it only works on this machine, and Caddy signs the certificate itself"
    return 0
  fi
  if [ "$dns_check" -eq 0 ]; then
    note "DNS: not checked (--no-dns-check)"
    return 0
  fi
  seen="$(addresses_of "$name" | tr '\n' ' ' | sed 's/ *$//')"
  mine="$(public_ip)"
  if [ -z "$seen" ]; then
    fail "$name has no address yet: no DNS 'A' record is visible from here. Create one that points at ${mine:-the public address of this server}; a new record can take a few minutes to be seen."
  fi
  if [ -z "$mine" ]; then
    note "DNS: $name points at $seen. This server's public address could not be looked up,"
    note "     so the two were not compared (give it with FXLAB_PUBLIC_IP=... to have that done)."
    return 0
  fi
  case " $seen " in
    *" $mine "*) note "DNS: $name points at $mine, this server's public address" ;;
    *)
      fail "$name points at $seen, but this server's public address is $mine. Point the DNS 'A' record at $mine; after a change the old answer can be remembered for a while."
      ;;
  esac
}

port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

check_ports() {
  local port
  if caddy_running; then
    return 0
  fi
  for port in 80 443; do
    if port_in_use "$port"; then
      fail "port $port on this server is already used by another program ('sudo ss -ltnp' shows which). Caddy needs ports 80 and 443."
    fi
  done
  note "ports 80 and 443: free on this server"
}

# ------------------------------------------------------------------------------- Docker
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  DOCKER="$SUDO docker"
  $DOCKER info >/dev/null 2>&1 || fail "the Docker daemon is not reachable"
fi

caddy_running() {
  $DOCKER compose --profile domain ps --status running --services 2>/dev/null | grep -qx caddy
}

write_env() {
  # Replace .env in one step and keep its owner and mode (it holds the API key).
  # $1: the name to serve, or nothing to take the two lines out.
  local tmp
  tmp="$(mktemp .env.XXXXXX)"
  chmod --reference=.env "$tmp"
  chown --reference=.env "$tmp" 2>/dev/null || true
  grep -v -e '^DASHBOARD_DOMAIN=' -e '^COMPOSE_PROFILES=' .env >"$tmp" || true
  if [ -n "${1:-}" ]; then
    printf 'DASHBOARD_DOMAIN=%s\nCOMPOSE_PROFILES=domain\n' "$1" >>"$tmp"
  fi
  mv "$tmp" .env
}

wait_app() {
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 "http://127.0.0.1:8000${HEALTH}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# ------------------------------------------------------------------------------- remove
if [ "$mode" = "remove" ]; then
  case "$(env_value COMPOSE_PROFILES)" in
    "" | domain) ;;
    *) fail "COMPOSE_PROFILES in .env was not written by this script. Take 'domain' out of it by hand, and the DASHBOARD_DOMAIN line with it." ;;
  esac
  if [ -z "$current" ] && ! caddy_running; then
    note "no domain is set. Nothing to do."
    exit 0
  fi
  # the public door first, then the setting
  $DOCKER compose --profile domain rm -sf caddy >/dev/null
  write_env ""
  $DOCKER compose up -d app
  if ! wait_app; then
    $DOCKER compose logs --tail 20 app || true
    fail "the domain is off, but the application did not become ready again. The log lines above say why."
  fi
  note "the domain is off${current:+ ($current)}: Caddy is stopped and ports 80 and 443 of this server are closed again."
  note "The certificate stays in the volume caddy_data, for the day the name is turned on again."
  exit 0
fi

# -------------------------------------------------------------------------------- check
check_password
check_dns
check_ports
if [ "$mode" = "check" ]; then
  if [ "$trial" -eq 0 ]; then
    note "Not checked here: that the server's firewall lets ports 80 and 443 in (docs/DEPLOY.md)."
  fi
  if [ "$dns_check" -eq 0 ]; then
    note "Ready. Turn it on with: make domain ARGS=\"--no-dns-check $name\""
  else
    note "Ready. Turn it on with: make domain ARGS=$name"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------------- set
root_ca="$(mktemp)"
trap 'rm -f "$root_ca"' EXIT

https_answers() {
  # Asked on this machine with the name, so it does not depend on how the server reaches its
  # own public address.  The certificate must be valid for the name.
  local trust=(--noproxy '*')
  if [ "$trial" -eq 1 ]; then
    # the certificate authority Caddy made for itself; it appears a moment after the start
    $DOCKER compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt >"$root_ca" 2>/dev/null || return 1
    [ -s "$root_ca" ] || return 1
    trust+=(--cacert "$root_ca")
  fi
  curl -fsS --max-time 5 "${trust[@]}" --resolve "$name:443:127.0.0.1" "https://${name}${HEALTH}" >/dev/null 2>&1
}

case "$(env_value COMPOSE_PROFILES)" in
  "" | domain) ;;
  *) fail "COMPOSE_PROFILES in .env was not written by this script. Add 'domain' to it by hand and set DASHBOARD_DOMAIN=$name, or remove that line first." ;;
esac

if [ "$current" = "$name" ] && caddy_running && https_answers; then
  note "https://$name is already on and answers. Nothing to do."
  exit 0
fi

before="$(mktemp .env.before.XXXXXX)"
trap 'rm -f "$before" "$root_ca"' EXIT
cp -p .env "$before"

undo() {
  # No half-open public address: back to exactly what was there before this run.
  # Returns 1 when the containers could not be brought back to it.
  cp -p "$before" .env
  if [ -n "$current" ]; then
    $DOCKER compose up -d >/dev/null 2>&1 || return 1
  else
    $DOCKER compose --profile domain rm -sf caddy >/dev/null 2>&1 || return 1
    $DOCKER compose up -d app >/dev/null 2>&1 || return 1
  fi
  wait_app
}

give_up() {
  if ! undo; then
    fail "$* The settings in .env are back as they were, but the containers are not: run 'docker compose up -d' and look at 'docker compose ps'."
  fi
  fail "$* Nothing was left changed${current:+ (the dashboard is still on $current)}."
}

caddy_errors() {
  # the last things Caddy reported as errors, as "what: why" where the line has both
  $DOCKER compose logs --no-color --no-log-prefix --tail 300 caddy 2>/dev/null |
    grep '"level":"error"' |
    sed -E -e 's/.*"msg":"([^"]*)".*"(error|detail)":"(([^"\\]|\\.)*)".*/\1: \3/' -e 's/\\"/"/g' |
    cut -c1-400 | awk '!seen[$0]++' | tail -n 3 || true
}

write_env "$name"
note "restarting the application with the name"
$DOCKER compose up -d app
if ! wait_app; then
  $DOCKER compose logs --tail 20 app || true
  give_up "the application did not become ready (the log lines above say why)."
fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/api/status || true)"
if [ "$code" != "401" ]; then
  give_up "the application answers ${code:-nothing} without a login, so it will not be given a public address."
fi

note "starting Caddy (the first time, its image is downloaded)"
if ! $DOCKER compose up -d caddy; then
  note "If the reason above is 'address already in use', another program holds port 80 or 443"
  note "('sudo ss -ltnp' shows which). Should it be Tailscale Funnel or Serve: sudo tailscale funnel reset"
  give_up "Caddy could not be started."
fi

note "waiting for the certificate and for https://$name"
up=0
for _ in $(seq 1 $((HTTPS_WAIT_S / 2))); do
  if https_answers; then
    up=1
    break
  fi
  sleep 2
done
if [ "$up" -ne 1 ]; then
  printf '\n'
  note "https://$name did not answer within $HTTPS_WAIT_S seconds. What Caddy reported:"
  caddy_errors | sed 's/^/    /'
  note "The usual reasons:"
  note "  - ports 80 and 443 (TCP) are not open in the server's firewall (Lightsail: Networking, IPv4 firewall)"
  note "  - the DNS record is new or was changed a moment ago and is not seen everywhere yet"
  give_up "No certificate, so the domain was not turned on. Run the same command again when the reason is fixed."
fi

note "https://$name is up. Caddy got the certificate and renews it by itself."
note "The login is the dashboard password. Turn the domain off with: make domain ARGS=--remove"
if command -v tailscale >/dev/null 2>&1; then
  note "If Tailscale Funnel is still on, the old address is a second public door: sudo tailscale funnel reset"
fi

#!/usr/bin/env bash
# Run a command and print how much memory the whole machine needed on top of what was in
# use before it started: the drop of MemAvailable (which already counts cache the kernel
# can give back as available), sampled twice a second.
#
#   ./.github/mem-peak.sh "label" command [arguments...]
set -euo pipefail

label="$1"
shift
log="$(mktemp)"

available() { awk '/^MemAvailable:/ {print $2}' /proc/meminfo; }

available >"$log"
(while sleep 0.5; do available; done) >>"$log" &
sampler=$!
status=0
"$@" || status=$?
kill "$sampler" 2>/dev/null || true
wait "$sampler" 2>/dev/null || true

awk -v label="$label" '
  NR == 1 { first = $1; low = $1 }
  $1 < low { low = $1 }
  END { printf "memory peak, %s: %d MB above the level before it (%d samples)\n", label, (first - low) / 1024, NR }' "$log"
rm -f "$log"
exit "$status"

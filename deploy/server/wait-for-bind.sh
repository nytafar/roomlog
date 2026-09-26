#!/usr/bin/env bash
# ExecStartPre for roomlog-ingest: wait until the address in ROOMLOG_BIND exists on some
# interface. The Tailscale address can appear after default.target at boot, and a bind on
# a missing address would lose the race and fall back on Restart=on-failure. Same pattern
# as herdr-spawn's wait-for-bridge drop-in. Wildcard and loopback binds need no wait.
#
#   ROOMLOG_BIND=100.79.124.57:8480 wait-for-bind.sh [timeout_s]   (default 120)
set -u
timeout_s="${1:-120}"
addr="${ROOMLOG_BIND:-}"
addr="${addr%:*}"
case "$addr" in
  ""|0.0.0.0|::|127.*|localhost) exit 0 ;;
esac
for _ in $(seq 1 "$timeout_s"); do
  if ip -o addr show 2>/dev/null | grep -qF " $addr/"; then
    exit 0
  fi
  sleep 1
done
echo "roomlog-ingest: address $addr did not appear within ${timeout_s}s" >&2
exit 1

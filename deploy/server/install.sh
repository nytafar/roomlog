#!/usr/bin/env bash
# roomlog server installer for this host (systemd --user, no sudo). Design section 4.1 / 6.
#
#   deploy/server/install.sh [--dry-run] [--skip-model] [--skip-selftest] [--branch NAME]
#
# What it does, in order:
#   1. clone or fast-forward ~/services/apps/roomlog (ROOMLOG_REPO overrides the remote)
#   2. uv sync in server/ with Python 3.12
#   3. write ~/.config/roomlog/{server.toml,tokens.toml,service.env} from the examples if absent;
#      warn (exit 1) when an existing service.env binds anything but the loopback (ADR 0007)
#   4. install the user units into ~/.config/systemd/user and daemon-reload
#   5. roomlog fetch-model (unless --skip-model) and roomlog selftest (unless --skip-selftest)
#   6. print the tailscale serve and enable commands; it never runs them
set -euo pipefail

REPO_URL="${ROOMLOG_REPO:-https://github.com/nytafar/roomlog.git}"
BRANCH="${ROOMLOG_BRANCH:-main}"
APP_DIR="${ROOMLOG_APP_DIR:-$HOME/services/apps/roomlog}"
CONF_DIR="$HOME/.config/roomlog"
UNIT_DIR="$HOME/.config/systemd/user"
DATA_DIR="$HOME/.local/share/roomlog"
DRY_RUN=0
SKIP_MODEL=0
SKIP_SELFTEST=0
INSTALL_STATUS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-model) SKIP_MODEL=1 ;;
    --skip-selftest) SKIP_SELFTEST=1 ;;
    --branch) BRANCH="$2"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '==> %s\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then printf '    would run: %s\n' "$*"; else "$@"; fi
}

for tool in git uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing: $tool (mise shim expected on PATH)" >&2; exit 1; }
done
if [ "$(id -u)" = 0 ]; then echo "run as the login user, not root" >&2; exit 1; fi

# The examples ship next to this script; when installing from the checkout they are the same files.
EXAMPLES_DIR="$(cd "$(dirname "$0")" && pwd)"

log "1/6 checkout at $APP_DIR (branch $BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
  run git -C "$APP_DIR" fetch --quiet origin
  run git -C "$APP_DIR" checkout --quiet "$BRANCH"
  run git -C "$APP_DIR" pull --ff-only --quiet origin "$BRANCH"
else
  run mkdir -p "$(dirname "$APP_DIR")"
  run git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

log "2/6 uv sync (Python 3.12) in $APP_DIR/server"
if [ "$DRY_RUN" = 1 ]; then
  printf '    would run: (cd %s/server && uv python install 3.12 && uv sync --no-dev)\n' "$APP_DIR"
else
  (cd "$APP_DIR/server" && uv python install 3.12 && uv sync --no-dev)
fi
ROOMLOG="$APP_DIR/server/.venv/bin/roomlog"

log "3/6 config under $CONF_DIR (existing files are left alone)"
run mkdir -p "$CONF_DIR/secrets" "$DATA_DIR"
run chmod 700 "$CONF_DIR/secrets"
for pair in "server.toml.example:server.toml" "tokens.toml.example:tokens.toml" "service.env.example:service.env"; do
  src="$EXAMPLES_DIR/${pair%%:*}"
  dst="$CONF_DIR/${pair##*:}"
  if [ -e "$dst" ]; then
    printf '    keep     %s\n' "$dst"
  else
    printf '    write    %s (from %s)\n' "$dst" "$(basename "$src")"
    run cp "$src" "$dst"
  fi
done
run chmod 600 "$CONF_DIR/tokens.toml"
if [ "$DRY_RUN" = 0 ] && grep -q 'replace-me' "$CONF_DIR/tokens.toml"; then
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i "s/replace-me-with-a-long-random-token/$token/" "$CONF_DIR/tokens.toml"
  printf '    generated a token for device "oma" in %s\n' "$CONF_DIR/tokens.toml"
fi
if [ ! -e "$CONF_DIR/secrets/berget.key" ]; then
  printf '    note     %s/secrets/berget.key is missing; the berget backend will be skipped until it exists (0600)\n' "$CONF_DIR"
fi
# An install from before ADR 0007 pins ROOMLOG_BIND to the tailnet address. tailscale serve
# proxies to 127.0.0.1:8480, so that bind would 502. The file is the operator's: say so, do not edit it.
BIND_WARNING=0
BIND_NOW="$(sed -n 's/^[[:space:]]*ROOMLOG_BIND=//p' "$CONF_DIR/service.env" 2>/dev/null | tail -n 1 | tr -d "\"' ")"
case "$BIND_NOW" in
  ""|127.0.0.1:*|localhost:*) ;;
  *)
    BIND_WARNING=1
    INSTALL_STATUS=1
    printf '    ATTENTION %s/service.env has ROOMLOG_BIND=%s (kept as is).\n' "$CONF_DIR" "$BIND_NOW" >&2
    printf '              Ingest must bind the loopback behind tailscale serve (ADR 0007). Before enabling the services:\n' >&2
    printf "                  sed -i 's/^ROOMLOG_BIND=.*/ROOMLOG_BIND=127.0.0.1:8480/' %s/service.env\n" "$CONF_DIR" >&2
    printf '              and drop any ufw rule that opened 8480 to the tailnet.\n' >&2
    ;;
esac
if [ -e "$CONF_DIR/server.toml" ] && ! grep -q '^\[segmenter\]' "$CONF_DIR/server.toml"; then
  printf '    note     %s/server.toml has no [segmenter] section; the defaults apply (vad = "silero", raw_idle_s = 120,\n' "$CONF_DIR"
  printf '             threshold 0.5 / 0.35). Copy the section from %s/server.toml.example to change them.\n' "$EXAMPLES_DIR"
fi

log "4/6 user units in $UNIT_DIR"
run mkdir -p "$UNIT_DIR"
for unit in roomlog-ingest.service roomlog-worker.service roomlog-health.service roomlog-health.timer; do
  printf '    install  %s\n' "$UNIT_DIR/$unit"
  run cp "$EXAMPLES_DIR/$unit" "$UNIT_DIR/$unit"
done
run systemctl --user daemon-reload

log "5/6 model and selftest"
if [ "$SKIP_MODEL" = 1 ]; then
  printf '    skipped: run %s fetch-model when ready (about 3 GB into %s/models)\n' "$ROOMLOG" "$DATA_DIR"
elif ! run "$ROOMLOG" fetch-model; then
  # Continue through selftest and manual instructions, but report incomplete setup.
  printf '    WARNING: fetch-model failed; rerun %s fetch-model later. Continuing.\n' "$ROOMLOG" >&2
  INSTALL_STATUS=1
fi
if [ "$SKIP_SELFTEST" = 1 ]; then
  printf '    skipped: run %s selftest\n' "$ROOMLOG"
else
  if ! run "$ROOMLOG" selftest; then
    printf '    WARNING: selftest failed; inspect the report before starting services.\n' >&2
    INSTALL_STATUS=1
  fi
fi

log "6/6 manual steps (not run by this script)"
if [ "$BIND_WARNING" = 1 ]; then
  printf '    First fix ROOMLOG_BIND in %s/service.env (see ATTENTION above); the steps below assume 127.0.0.1:8480.\n' "$CONF_DIR"
fi
cat <<EOF
    Publish ingest over HTTPS on the tailnet name (once; HTTPS certificates must be enabled
    in the Tailscale admin console). Ingest itself binds 127.0.0.1:8480, so no ufw rule:
        tailscale serve --bg --https=443 http://127.0.0.1:8480
        tailscale serve status        # expect https://oma.tailf63b9a.ts.net -> http://127.0.0.1:8480
    Then enable and start the services:
        systemctl --user enable --now roomlog-ingest.service roomlog-worker.service roomlog-health.timer
    Check:
        systemctl --user status roomlog-ingest roomlog-worker
        journalctl --user -u roomlog-worker -f
        $ROOMLOG status
EOF
exit "$INSTALL_STATUS"

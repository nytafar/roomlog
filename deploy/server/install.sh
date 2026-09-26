#!/usr/bin/env bash
# roomlog server installer for this host (systemd --user, no sudo). Design section 4.1 / 6.
#
#   deploy/server/install.sh [--dry-run] [--skip-model] [--skip-selftest] [--branch NAME]
#
# What it does, in order:
#   1. clone or fast-forward ~/services/apps/roomlog (ROOMLOG_REPO overrides the remote)
#   2. uv sync in server/ with Python 3.12
#   3. write ~/.config/roomlog/{server.toml,tokens.toml,service.env} from the examples if absent
#   4. install the user units into ~/.config/systemd/user and daemon-reload
#   5. roomlog fetch-model (unless --skip-model) and roomlog selftest (unless --skip-selftest)
#   6. print the ufw rule and the enable command; it never runs them
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
cat <<EOF
    Open the ingest port on the tailnet interface (needs sudo, once):
        sudo ufw allow in on tailscale0 to any port 8480 proto tcp
    Then enable and start the services:
        systemctl --user enable --now roomlog-ingest.service roomlog-worker.service roomlog-health.timer
    Check:
        systemctl --user status roomlog-ingest roomlog-worker
        journalctl --user -u roomlog-worker -f
        $ROOMLOG status
EOF
exit "$INSTALL_STATUS"

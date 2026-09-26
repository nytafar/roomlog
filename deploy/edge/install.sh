#!/usr/bin/env bash
# roomlog-edge installer (design §6).
#
#   install.sh --mode user   [--dry-run] [--device-id ID] [--server-url URL] [--no-model]
#   install.sh --mode system [--dry-run] [--device-id ID] [--server-url URL] [--no-model] [--audio-device DEV]
#
# user:   this workstation. venv = <repo>/edge/.venv via uv; config ~/.config/roomlog-edge/edge.toml;
#         state ~/.local/share/roomlog-edge; units in ~/.config/systemd/user.
# system: the Pi, run as root. apt deps; user roomlog (group audio); venv /opt/roomlog/edge;
#         config /etc/roomlog/edge.toml; state /var/lib/roomlog; units in /etc/systemd/system.
#
# Never enables or starts a unit; it prints the commands to run. With --dry-run it
# prints every step and changes nothing.
set -euo pipefail

MODE=""
DRY_RUN=0
DEVICE_ID=""
SERVER_URL=""
AUDIO_DEVICE=""
FETCH_MODEL=1

MODEL_TAG="v6.2.3"
MODEL_URL="https://raw.githubusercontent.com/snakers4/silero-vad/${MODEL_TAG}/src/silero_vad/data/silero_vad.onnx"
MODEL_BLOB_SHA1="80c5592ef1f4c9ede3e357bbd02eb863358a6a9d"

usage() { sed -n '2,14p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --server-url) SERVER_URL="$2"; shift 2 ;;
    --audio-device) AUDIO_DEVICE="$2"; shift 2 ;;
    --no-model) FETCH_MODEL=0; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done
[ "$MODE" = user ] || [ "$MODE" = system ] || { echo "--mode user|system is required" >&2; usage 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UNITS="$HERE/units"

log() { printf '%s\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then log "+ $*"; else log "+ $*"; "$@"; fi
}
# write_file <path> <mode>  (content on stdin)
write_file() {
  local path="$1" mode="$2" content
  content="$(cat)"
  if [ "$DRY_RUN" = 1 ]; then
    log "+ write $path (mode $mode):"
    printf '%s\n' "$content" | sed 's/^/    /'
  else
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
    chmod "$mode" "$path"
    log "+ wrote $path"
  fi
}

if [ "$MODE" = user ]; then
  CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/roomlog-edge"
  CONFIG="$CONFIG_DIR/edge.toml"
  TOKEN="$CONFIG_DIR/token"
  STATE="${XDG_DATA_HOME:-$HOME/.local/share}/roomlog-edge"
  STATUS_DIR='$XDG_RUNTIME_DIR/roomlog-edge'
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  VENV="$REPO/edge/.venv"
  WANTED_BY="default.target"
  SERVICE_EXTRA=""
  CAPTURE_EXTRA=""   # no Nice=: the user manager has no nice privilege (exec fails 201/NICE)
  SYSTEMCTL="systemctl --user"
  DEVICE_ID="${DEVICE_ID:-oma}"
else
  [ "$(id -u)" = 0 ] || [ "$DRY_RUN" = 1 ] || { echo "--mode system must run as root" >&2; exit 2; }
  CONFIG_DIR="/etc/roomlog"
  CONFIG="$CONFIG_DIR/edge.toml"
  TOKEN="$CONFIG_DIR/token"
  STATE="/var/lib/roomlog"
  STATUS_DIR="/run/roomlog"
  UNIT_DIR="/etc/systemd/system"
  VENV="/opt/roomlog/edge"
  WANTED_BY="multi-user.target"
  SERVICE_EXTRA=$'User=roomlog\nGroup=roomlog\nSupplementaryGroups=audio\nRuntimeDirectory=roomlog\nRuntimeDirectoryPreserve=yes'
  CAPTURE_EXTRA="Nice=-5"
  SYSTEMCTL="systemctl"
  DEVICE_ID="${DEVICE_ID:-$(hostname -s 2>/dev/null || echo pi)}"
fi
BIN="$VENV/bin/roomlog-edge"
SERVER_URL="${SERVER_URL:-http://100.79.124.57:8480}"

log "roomlog-edge install: mode=$MODE dry_run=$DRY_RUN"
log "  repo      $REPO"
log "  venv      $VENV"
log "  config    $CONFIG"
log "  state     $STATE"
log "  units     $UNIT_DIR"
log "  device_id $DEVICE_ID"
log "  server    $SERVER_URL"

# 1. system prerequisites --------------------------------------------------
if [ "$MODE" = system ]; then
  if command -v apt-get >/dev/null 2>&1; then
    run apt-get install -y --no-install-recommends python3-venv libportaudio2 opus-tools chrony curl
  else
    log "! no apt-get: install python3-venv, libportaudio2, opus-tools, chrony and curl yourself"
  fi
  if ! id roomlog >/dev/null 2>&1; then
    run useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin --groups audio roomlog
  else
    run usermod -a -G audio roomlog
  fi
fi

# 2. venv -------------------------------------------------------------------
if [ "$MODE" = user ]; then
  command -v uv >/dev/null 2>&1 || { echo "uv is required for --mode user" >&2; exit 2; }
  run uv sync --project "$REPO/edge"
else
  if [ ! -x "$VENV/bin/python" ]; then
    run python3 -m venv "$VENV"
  fi
  run "$VENV/bin/pip" install --upgrade pip
  run "$VENV/bin/pip" install "$REPO/edge"
fi

# 3. directories --------------------------------------------------------------
run mkdir -p "$CONFIG_DIR" "$STATE/spool"
if [ "$MODE" = system ]; then
  run chown -R roomlog:roomlog "$STATE"
  # root-owned config dir, readable by the service user; the token alone is 0600
  run chown root:roomlog "$CONFIG_DIR"
  run chmod 750 "$CONFIG_DIR"
fi

# 4. config -----------------------------------------------------------------
if [ -e "$CONFIG" ]; then
  log "  config exists, keeping $CONFIG"
else
  rendered="$(sed \
    -e "s|^device_id = .*|device_id = \"$DEVICE_ID\"|" \
    -e "s|^server_url = .*|server_url = \"$SERVER_URL\"|" \
    -e "s|^token_file = .*|token_file = \"$TOKEN\"|" \
    -e "s|^spool_dir = .*|spool_dir = \"$STATE/spool\"|" \
    -e "s|^model_path = .*|model_path = \"$STATE/silero_vad.onnx\"|" \
    -e "s|^status_dir = .*|status_dir = \"$STATUS_DIR\"|" \
    -e "s|^metrics_file = .*|metrics_file = \"$STATE/metrics.prom\"|" \
    "$HERE/edge.toml.example")"
  if [ -n "$AUDIO_DEVICE" ]; then
    rendered="$(printf '%s\n' "$rendered" | sed -e "s|^# device = .*|device = \"$AUDIO_DEVICE\"|")"
  fi
  if [ "$MODE" = system ]; then
    rendered="$(printf '%s\n' "$rendered" | sed -e 's|^backend = "auto"|backend = "opusenc"|')"
  else
    rendered="$(printf '%s\n' "$rendered" | sed -e 's|^backend = "auto"|backend = "ffmpeg"|')"
  fi
  printf '%s\n' "$rendered" | write_file "$CONFIG" 0644
fi

# 5. token ------------------------------------------------------------------
if [ -e "$TOKEN" ]; then
  log "  token exists, keeping $TOKEN"
else
  printf '%s' "" | write_file "$TOKEN" 0600
  log "! put the device token (from the server's tokens.toml, device \"$DEVICE_ID\") in $TOKEN"
fi
if [ "$MODE" = system ]; then
  run chown roomlog:roomlog "$TOKEN"
fi

# 6. model ------------------------------------------------------------------
MODEL="$STATE/silero_vad.onnx"
if [ "$FETCH_MODEL" = 1 ]; then
  if [ -e "$MODEL" ]; then
    log "  model exists, keeping $MODEL"
  else
    run curl -fsSL "$MODEL_URL" -o "$MODEL.tmp"
    if [ "$DRY_RUN" = 1 ]; then
      log "+ verify git blob sha1 of $MODEL.tmp == $MODEL_BLOB_SHA1, then mv to $MODEL"
      log "+ record sha256 of $MODEL as model_sha256 in $CONFIG"
    else
      size="$(stat -c%s "$MODEL.tmp")"
      got="$( { printf 'blob %d\0' "$size"; cat "$MODEL.tmp"; } | sha1sum | cut -d' ' -f1)"
      if [ "$got" != "$MODEL_BLOB_SHA1" ]; then
        rm -f "$MODEL.tmp"
        echo "model blob sha1 mismatch: $got != $MODEL_BLOB_SHA1" >&2
        exit 1
      fi
      mv "$MODEL.tmp" "$MODEL"
      sha256="$(sha256sum "$MODEL" | cut -d' ' -f1)"
      sed -i -e "s|^model_sha256 = .*|model_sha256 = \"$sha256\"|" "$CONFIG"
      log "+ model ok: blob sha1 $got, sha256 $sha256 pinned in $CONFIG"
      [ "$MODE" = system ] && chown roomlog:roomlog "$MODEL"
    fi
  fi
else
  log "  --no-model: skipping model fetch"
fi

# 7. units ------------------------------------------------------------------
render_unit() {
  # replaces @BIN@ @CONFIG@ @WANTED_BY@ and the @SERVICE_EXTRA@ line
  awk -v bin="$BIN" -v config="$CONFIG" -v wanted="$WANTED_BY" -v extra="$SERVICE_EXTRA" -v cextra="$CAPTURE_EXTRA" '
    /^@SERVICE_EXTRA@$/ { if (extra != "") print extra; next }
    /^@CAPTURE_EXTRA@$/ { if (cextra != "") print cextra; next }
    { gsub(/@BIN@/, bin); gsub(/@CONFIG@/, config); gsub(/@WANTED_BY@/, wanted); print }
  ' "$1"
}
for tmpl in "$UNITS"/*.in; do
  name="$(basename "$tmpl" .in)"
  render_unit "$tmpl" | write_file "$UNIT_DIR/$name" 0644
done
run $SYSTEMCTL daemon-reload

# 8. what is left for you ----------------------------------------------------
cat <<EOF

Not done by this script (deliberately):
  token:     write the device token to $TOKEN
  selftest:  $BIN --config $CONFIG selftest
  devices:   $BIN --config $CONFIG devices      (then set [audio] device in $CONFIG on the Pi)
  enable:    $SYSTEMCTL enable --now roomlog-edge-capture.service roomlog-edge-uploader.service roomlog-edge-health.timer
EOF

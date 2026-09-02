#!/usr/bin/env bash
# Interactive Wi-Fi setup via nmtui, then tvstop/tvstart to restart the player.
# Invoked by wifi_hotkey.sh on Ctrl+W. No systemd unit changes.
#
# Requires passwordless sudo for openvt, e.g. in /etc/sudoers.d/vobox-wifi:
#   vobox ALL=(root) NOPASSWD: /usr/bin/openvt, /bin/openvt
set -euo pipefail

PLAYER_PID="${1:?usage: wifi_wizard.sh PLAYER_PID}"
STATE_DIR="${STATE_DIR:-/tmp/player/state}"
WIZARD_LOCK="${WIZARD_LOCK:-${STATE_DIR}/wizard.lock}"
MPV_SOCK="${MPV_SOCK:-/tmp/venditt-mpv.sock}"
CONFIG="${CONFIG:-/data/player/config.env}"

log(){ echo "[$(date '+%F %T')] $*"; }

# Map config.env ORIENTATION (0/90/180/270) to fbcon rotate (0/1/2/3).
# Same clockwise convention as mpv --video-rotate.
fbcon_rotate_from_orientation() {
  case "${1:-0}" in
    90)  echo 1 ;;
    180) echo 2 ;;
    270) echo 3 ;;
    *)   echo 0 ;;
  esac
}

release_lock() {
  rm -rf "$WIZARD_LOCK" >/dev/null 2>&1 || true
}

WIZARD_PLAYER_RESTARTED=0
on_wizard_exit() {
  # Successful path restarts the player; leave the lock for the new process to clear.
  (( WIZARD_PLAYER_RESTARTED )) || release_lock
}

if ! mkdir "$WIZARD_LOCK" 2>/dev/null; then
  log "Wi-Fi wizard already in progress; skipping"
  exit 0
fi
trap on_wizard_exit EXIT

log "Wi-Fi wizard requested (Ctrl+W)"

ORIENTATION=0
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
  ORIENTATION="${ORIENTATION:-0}"
fi
FBCON_ROTATE="$(fbcon_rotate_from_orientation "$ORIENTATION")"

# Free the display / DRM so openvt + nmtui are usable (and Chromium won't eat Ctrl+W).
if [[ -S "$MPV_SOCK" ]]; then
  printf '%s\n' '{"command":["quit"]}' | socat - UNIX-CONNECT:"$MPV_SOCK" >/dev/null 2>&1 || true
fi
pkill -f "input-ipc-server=$MPV_SOCK" >/dev/null 2>&1 || true
rm -f "$MPV_SOCK" >/dev/null 2>&1 || true
pkill -f "/usr/bin/chromium" >/dev/null 2>&1 || true
pkill -f "X :0" >/dev/null 2>&1 || true
pkill -f "Xorg :0" >/dev/null 2>&1 || true
sleep 0.5

internet_ping_ok() {
  ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1
}

NMTUI_BIN="$(command -v nmtui || true)"
if [[ -z "$NMTUI_BIN" ]]; then
  log "ERROR: nmtui not found; install network-manager / nmtui"
else
  log "Launching nmtui on a free VT (openvt, orientation=${ORIENTATION}° / fbcon=${FBCON_ROTATE})..."
  # openvt runs as root: set fbcon rotate to match config.env, then nmtui.
  # sudoers: NOPASSWD openvt (and bash so the wrapper can run)
  if out="$(sudo -n openvt -s -w -- /bin/bash -c \
    "echo ${FBCON_ROTATE} > /sys/class/graphics/fbcon/rotate_all 2>/dev/null || true; exec \"$NMTUI_BIN\"" \
    2>&1)"; then
    [[ -n "$out" ]] && log "  openvt: $out"
    log "nmtui exited"
  else
    log "ERROR: openvt/nmtui failed${out:+: $out}"
    log "HINT: allow passwordless sudo for openvt, e.g.:"
    log "  vobox ALL=(root) NOPASSWD: /usr/bin/openvt, /bin/openvt"
  fi
fi

if internet_ping_ok; then
  log "Network OK after wizard (ping 8.8.8.8); restarting player"
else
  log "WARN: ping 8.8.8.8 still failing after wizard; restarting player for clean display"
fi

log "Running tvstop..."
tvstop
log "Running tvstart..."
if tvstart; then
  WIZARD_PLAYER_RESTARTED=1
else
  log "WARN: tvstart failed"
fi

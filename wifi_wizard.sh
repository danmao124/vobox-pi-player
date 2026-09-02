#!/usr/bin/env bash
# Interactive Wi-Fi setup via nmtui, then restart the player (systemctl restart when under systemd).
# Invoked by wifi_hotkey.sh on Ctrl+I. No systemd unit changes.
#
# Requires passwordless sudo, e.g. in /etc/sudoers.d/vobox-wifi:
#   vobox ALL=(root) NOPASSWD: /usr/bin/openvt, /bin/openvt, /usr/bin/systemd-run, /usr/bin/systemctl
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

log "Wi-Fi wizard requested (Ctrl+I)"

ORIENTATION=0
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
  ORIENTATION="${ORIENTATION:-0}"
fi
FBCON_ROTATE="$(fbcon_rotate_from_orientation "$ORIENTATION")"

# Free the display / DRM so openvt + nmtui are usable (and Chromium won't eat Ctrl+I).
if [[ -S "$MPV_SOCK" ]]; then
  printf '%s\n' '{"command":["quit"]}' | socat - UNIX-CONNECT:"$MPV_SOCK" >/dev/null 2>&1 || true
fi
pkill -f "input-ipc-server=$MPV_SOCK" >/dev/null 2>&1 || true
rm -f "$MPV_SOCK" >/dev/null 2>&1 || true
pkill -f "/usr/bin/chromium" >/dev/null 2>&1 || true
pkill -f "X :0" >/dev/null 2>&1 || true
pkill -f "Xorg :0" >/dev/null 2>&1 || true
# Let mpv/Chromium release DRM before openvt grabs a VT.
sleep 1

internet_ping_ok() {
  ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1
}

launch_nmtui() {
  local rotate="$1"
  local nmtui_bin="$2"
  local inner_cmd out=""

  inner_cmd="echo ${rotate} > /sys/class/graphics/fbcon/rotate_all 2>/dev/null || true; exec \"${nmtui_bin}\""

  log "Launching nmtui (orientation=${ORIENTATION}° / fbcon=${rotate})..."

  # Processes in venditt-player.service cannot open /dev/tty0 ("Couldn't get a file
  # descriptor referring to the console"). Run openvt in a transient unit instead.
  if command -v systemd-run >/dev/null 2>&1; then
    log "Trying systemd-run + openvt -f (outside service cgroup)..."
    if out="$(sudo -n systemd-run \
        --unit=vobox-wifi-nmtui \
        --wait \
        --collect \
        --property=StandardInput=tty \
        --property=StandardOutput=tty \
        --property=TTYPath=/dev/tty2 \
        --property=TTYReset=yes \
        /usr/bin/openvt -f -s -w -- /bin/bash -c "$inner_cmd" \
        2>&1)"; then
      [[ -n "$out" ]] && log "  nmtui: $out"
      log "nmtui exited"
      return 0
    fi
    log "WARN: systemd-run + openvt failed${out:+: $out}"
  fi

  log "Trying openvt -f directly..."
  if out="$(sudo -n openvt -f -s -w -- /bin/bash -c "$inner_cmd" 2>&1)"; then
    [[ -n "$out" ]] && log "  openvt: $out"
    log "nmtui exited"
    return 0
  fi

  log "ERROR: openvt/nmtui failed${out:+: $out}"
  log "HINT: passwordless sudo for openvt and systemd-run, e.g.:"
  log "  vobox ALL=(root) NOPASSWD: /usr/bin/openvt, /bin/openvt, /usr/bin/systemd-run, /usr/bin/systemctl"
  return 1
}

NMTUI_BIN="$(command -v nmtui || true)"
if [[ -z "$NMTUI_BIN" ]]; then
  log "ERROR: nmtui not found; install network-manager / nmtui"
else
  launch_nmtui "$FBCON_ROTATE" "$NMTUI_BIN" || true
fi

restart_player_after_wizard() {
  local svc="${VENDITT_SERVICE:-venditt-player.service}"

  # When tvads.sh runs under systemd, this wizard is in that unit's cgroup.
  # tvstop then tvstart can stop the unit (killing us) before tvstart runs,
  # leaving the player stopped — Restart=always does not restart explicit stops.
  if command -v systemctl >/dev/null 2>&1 \
      && systemctl is-active --quiet "$svc" 2>/dev/null; then
    log "Restarting $svc (atomic systemctl restart)..."
    if sudo -n systemctl restart "$svc" 2>/dev/null \
        || systemctl restart "$svc" 2>/dev/null; then
      WIZARD_PLAYER_RESTARTED=1
      return 0
    fi
    log "WARN: systemctl restart failed; falling back to tvstop/tvstart"
  fi

  if command -v tvstop >/dev/null 2>&1 && command -v tvstart >/dev/null 2>&1; then
    log "Running tvstop..."
    tvstop
    log "Running tvstart..."
    if tvstart; then
      WIZARD_PLAYER_RESTARTED=1
      return 0
    fi
    log "WARN: tvstart failed"
    return 1
  fi

  log "WARN: no restart helper; releasing wizard lock so tvads.sh can resume"
  release_lock
}

if internet_ping_ok; then
  log "Network OK after wizard (ping 8.8.8.8); restarting player"
else
  log "WARN: ping 8.8.8.8 still failing after wizard; restarting player for clean display"
fi

restart_player_after_wizard

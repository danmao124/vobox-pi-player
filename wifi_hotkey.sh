#!/usr/bin/env bash
# Watch /dev/input for Ctrl+W and launch the Wi-Fi wizard.
# Started from tvads.sh (no systemd unit changes). Requires group `input`.
set -euo pipefail

PLAYER_PID="${1:?usage: wifi_hotkey.sh PLAYER_PID WIZARD_SCRIPT}"
WIZARD_SCRIPT="${2:?usage: wifi_hotkey.sh PLAYER_PID WIZARD_SCRIPT}"
WIZARD_LOCK="${3:-/tmp/player/state/wizard.lock}"
DEBOUNCE_SECS="${WIFI_HOTKEY_DEBOUNCE:-3}"

log(){ echo "[$(date '+%F %T')] $*"; }

if ! command -v python3 >/dev/null 2>&1; then
  log "ERROR: python3 required for Ctrl+W Wi-Fi hotkey"
  exit 1
fi

log "Wi-Fi hotkey watcher started (Ctrl+W -> $WIZARD_SCRIPT)"

# Stdlib-only evdev reader: tracks Left/Right Ctrl + W (KEY_W=17).
exec python3 - "$PLAYER_PID" "$WIZARD_SCRIPT" "$WIZARD_LOCK" "$DEBOUNCE_SECS" <<'PY'
import glob
import os
import struct
import subprocess
import sys
import time

player_pid = int(sys.argv[1])
wizard_script = sys.argv[2]
wizard_lock = sys.argv[3]
debounce_secs = float(sys.argv[4])

EV_KEY = 0x01
KEY_LEFTCTRL = 29
KEY_RIGHTCTRL = 97
KEY_W = 17

# struct input_event: timeval (2x long) + type + code + value
fmt = "llHHi"
event_size = struct.calcsize(fmt)

ctrl_down = False
last_fire = 0.0


def log(msg: str) -> None:
    ts = time.strftime("%F %T")
    print(f"[{ts}] {msg}", flush=True)


def open_keyboards():
    fds = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        fds.append(fd)
    return fds


def fire_wizard() -> None:
    global last_fire
    now = time.time()
    if now - last_fire < debounce_secs:
        return
    if os.path.isdir(wizard_lock):
        log("Wi-Fi wizard already in progress; ignoring Ctrl+W")
        return
    last_fire = now
    log("Ctrl+W detected; launching Wi-Fi wizard")
    try:
        subprocess.Popen(
            [wizard_script, str(player_pid)],
            start_new_session=True,
        )
    except OSError as e:
        log(f"ERROR: failed to launch wizard: {e}")


fds = open_keyboards()
if not fds:
    log("ERROR: no readable /dev/input/event* (is user in group input?)")
    sys.exit(1)

try:
    import select

    while True:
        # Re-scan occasionally in case a USB keyboard is plugged in later
        readable, _, _ = select.select(fds, [], [], 5.0)
        if not readable:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            fds = open_keyboards()
            if not fds:
                time.sleep(2)
                fds = open_keyboards()
            continue

        for fd in readable:
            try:
                data = os.read(fd, event_size * 32)
            except OSError:
                continue
            offset = 0
            while offset + event_size <= len(data):
                _, _, etype, code, value = struct.unpack_from(fmt, data, offset)
                offset += event_size
                if etype != EV_KEY:
                    continue
                if code in (KEY_LEFTCTRL, KEY_RIGHTCTRL):
                    # value: 1=press, 0=release, 2=repeat
                    if value == 0:
                        ctrl_down = False
                    elif value == 1:
                        ctrl_down = True
                elif code == KEY_W and value == 1 and ctrl_down:
                    fire_wizard()
finally:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass
PY

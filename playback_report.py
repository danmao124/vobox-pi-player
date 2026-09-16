#!/usr/bin/env python3
"""Record mpv playback events and supply bounded history for signed heartbeats."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

WINDOW_MS = 180_000


def now_ms():
    return time.time_ns() // 1_000_000


def read_history(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []


def save_history(path, history):
    cutoff = now_ms() - WINDOW_MS
    history = [x for x in history if x.get("endMs", now_ms()) >= cutoff][-256:]
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(history, separators=(",", ":")))
    os.replace(tmp, path)


def finish(path):
    history = read_history(path)
    if history and "endMs" not in history[-1]:
        history[-1]["endMs"] = now_ms()
        save_history(path, history)


def next_cursor(playlist, batch_hash):
    try:
        cursor = json.loads(Path(str(playlist) + ".cursor").read_text())
        if cursor.get("playlistHash") != batch_hash:
            return {}
        values = {name: cursor[name] for name in ("nextIndex", "nextBlastIndex")}
        if any(type(value) is not int or value < 0 for value in values.values()):
            return {}
        return values
    except (OSError, ValueError, KeyError, AttributeError):
        return {}


def load_and_record(sock_path, src, playlist, position, history_path):
    # Subscribe before loadfile: its command response precedes actual playback.
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(sock_path)
        sock.sendall((json.dumps({"command": ["loadfile", src, "replace"]}) + "\n").encode())
        loaded = False
        deadline = time.monotonic() + 5
        with sock.makefile("r") as stream:
            while time.monotonic() < deadline:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                line = stream.readline()
                if not line:
                    break
                event = json.loads(line)
                if event.get("event") == "file-loaded":
                    loaded = True
                if loaded and event.get("event") == "playback-restart":
                    start = now_ms()
                    batch = hashlib.sha256(Path(playlist).read_bytes()).hexdigest()
                    history = read_history(history_path)
                    history.append({"key": f"{batch}:{int(position)}", "startMs": start,
                                    **next_cursor(playlist, batch)})
                    save_history(history_path, history)
                    return
    raise RuntimeError("mpv did not confirm playback within 5 seconds")


def snapshot(path):
    sampled = now_ms()
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=2, check=False)
        synced = result.returncode == 0 and result.stdout.strip() == "yes"
    except (OSError, subprocess.TimeoutExpired):
        synced = False
    history = read_history(path)
    segments = [dict(x, endMs=min(x.get("endMs", sampled), sampled))
                for x in history if x.get("endMs", sampled) >= sampled - WINDOW_MS]
    return {"playback": {"sampledAtMs": sampled, "clockSynced": synced,
                         "segments": segments[-256:]}}


if __name__ == "__main__":
    try:
        action, *args = sys.argv[1:]
        if action == "load":
            load_and_record(*args)
        elif action == "finish":
            finish(*args)
        elif action == "snapshot":
            print(json.dumps(snapshot(*args), separators=(",", ":")))
        else:
            raise ValueError("Unknown playback report action")
    except Exception as error:
        print(f"Playback reporting: {error}", file=sys.stderr)
        sys.exit(1)

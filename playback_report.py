#!/usr/bin/env python3
"""Record mpv playback events and supply bounded history for signed heartbeats."""
import hashlib
import json
import math
import os
import socket
import sys
import time
from pathlib import Path
from playback_mode import browser_content

WINDOW_MS = 120_000
DIAGNOSTIC_PROPERTIES = (
    "path", "filename", "idle-active", "pause", "paused-for-cache", "eof-reached",
    "time-pos", "duration", "vid", "video-format", "video-codec", "hwdec-current",
    "vo-configured", "video-params", "video-out-params",
)


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
        started = loaded = False
        deadline = time.monotonic() + 5
        with sock.makefile("r") as stream:
            while time.monotonic() < deadline:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                line = stream.readline()
                if not line:
                    break
                event = json.loads(line)
                if "event" not in event and event.get("error", "success") != "success":
                    raise RuntimeError(f"mpv rejected loadfile: {event['error']}")
                if event.get("event") == "shutdown":
                    raise RuntimeError("mpv shut down while loading")
                if event.get("event") == "start-file":
                    started = True
                # Replacing the previous asset also emits end-file. Only act on
                # the new file's end, after its start-file/file-loaded event.
                if event.get("event") == "end-file" and (started or loaded):
                    raise RuntimeError(
                        f"mpv ended before playback: reason={event.get('reason')} "
                        f"error={event.get('file_error', event.get('error', 'unknown'))}"
                    )
                if event.get("event") == "file-loaded":
                    loaded = True
                if loaded and event.get("event") == "playback-restart":
                    started_ns = time.monotonic_ns()
                    # Reporting failure is not playback failure. Never reload
                    # or skip a confirmed asset because telemetry cannot save.
                    try:
                        start = now_ms()
                        batch = hashlib.sha256(Path(playlist).read_bytes()).hexdigest()
                        history = read_history(history_path)
                        history.append({"key": f"{batch}:{int(position)}", "startMs": start,
                                        **next_cursor(playlist, batch)})
                        save_history(history_path, history)
                    except Exception as error:
                        print(f"Playback confirmed; reporting failed: src={src!r}: {error}", file=sys.stderr)
                    return started_ns
    raise RuntimeError("mpv did not confirm playback within 5 seconds")


def sync_assets_ready(playlist, asset_dir):
    """Use the same URL normalization/cache filenames as tvads.sh."""
    try:
        urls = [line.replace("\r", "").rstrip().rstrip(",")
                for line in Path(playlist).read_text().splitlines()]
        urls = [url for url in urls if url]
        if not urls:
            return False
        for url in urls:
            filename = url.split("?", 1)[0].rsplit("/", 1)[-1]
            cached = Path(asset_dir) / filename
            if not cached.is_file() or cached.stat().st_size <= 0:
                return False
        return True
    except (OSError, UnicodeError):
        return False


def inspect_mpv(sock_path):
    """Read a bounded diagnostic snapshot without changing playback."""
    result = {}
    pending = {i: prop for i, prop in enumerate(DIAGNOSTIC_PROPERTIES, 1)}
    deadline = time.monotonic() + 2
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(2)
            sock.connect(sock_path)
            commands = [json.dumps({"command": ["get_property", prop], "request_id": i})
                        for i, prop in pending.items()]
            sock.sendall(("\n".join(commands) + "\n").encode())
            with sock.makefile("r") as stream:
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("mpv diagnostic timeout")
                    sock.settimeout(remaining)
                    line = stream.readline()
                    if not line:
                        raise RuntimeError("mpv disconnected during diagnostics")
                    reply = json.loads(line)
                    prop = pending.pop(reply.get("request_id"), None)
                    if prop is not None:
                        result[prop] = (reply.get("data") if reply.get("error") == "success"
                                        else {"error": reply.get("error", "unknown")})
    except (OSError, ValueError, RuntimeError) as error:
        result["diagnosticError"] = str(error)
    if pending:
        result["unavailable"] = list(pending.values())
    return result


def wait_image(started_ns, duration, wizard_lock, sync_at, sync_list, mode_file="", station="", asset_dir="/data/assets"):
    """Return 2 for a timed sync, or 3 when the backend requests Chromium."""
    seconds = float(duration)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Image duration must be finite and non-negative")
    # An unconfirmed/fallback load has no timestamp; start its timer here.
    start = int(started_ns) if started_ns else time.monotonic_ns()
    deadline = start + int(seconds * 1_000_000_000)
    while True:
        if Path(wizard_lock).is_dir():
            return 0
        if mode_file and browser_content(mode_file, station):
            return 3
        try:
            at = Path(sync_at).read_text().strip()
            if (at.isascii() and at.isdecimal()
                    and time.time_ns() // 1_000_000_000 >= int(at) - 1
                    and sync_assets_ready(sync_list, asset_dir)):
                return 2
        except OSError:
            pass  # No complete staged sync yet; files can change between reads.
        remaining = (deadline - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            return 0
        time.sleep(min(0.2, remaining))


def snapshot(path):
    sampled = now_ms()
    history = read_history(path)
    segments = [dict(x, endMs=min(x.get("endMs", sampled), sampled))
                for x in history if x.get("endMs", sampled) >= sampled - WINDOW_MS]
    return {"playback": {"sampledAtMs": sampled, "segments": segments[-256:]}}


if __name__ == "__main__":
    try:
        action, *args = sys.argv[1:]
        if action == "load":
            print(load_and_record(*args))
        elif action == "wait-image":
            sys.exit(wait_image(*args))
        elif action == "sync-ready":
            sys.exit(0 if sync_assets_ready(*args) else 1)
        elif action == "inspect":
            print(json.dumps(inspect_mpv(*args), indent=2))
        elif action == "finish":
            finish(*args)
        elif action == "snapshot":
            print(json.dumps(snapshot(*args), separators=(",", ":")))
        else:
            raise ValueError("Unknown playback report action")
    except Exception as error:
        print(f"Playback reporting: {error}", file=sys.stderr)
        sys.exit(1)

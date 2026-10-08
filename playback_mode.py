#!/usr/bin/env python3
"""Share the backend's desired playback mode across the player and its pollers."""
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def read_state(path, station):
    try:
        state = json.loads(Path(path).read_text())
        if isinstance(state, dict) and state.get("stationId") == station.strip().upper():
            return state
    except (OSError, ValueError):
        pass
    return {}


def browser_content(path, station):
    state = read_state(path, station)
    if state.get("hasYoutube") is True:
        return "ads"
    return state.get("webContent") or ""


def update_state(path, station, requested_at, payload):
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict) or response.get("success") is not True:
        return
    updates = {}
    if type(response.get("hasYoutube")) is bool:
        updates["hasYoutube"] = response["hasYoutube"]
    if "webContent" in response and (
        response["webContent"] is None or isinstance(response["webContent"], str)
    ):
        updates["webContent"] = response["webContent"]
    if not updates:
        return  # Missing flags and failed responses must not switch a running display.

    path = Path(path)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path, station)
        state["stationId"] = station.strip().upper()
        versions = state.setdefault("requestedAt", {})
        for key, value in updates.items():
            # A slow prefetch must not undo a newer heartbeat (or vice versa).
            if requested_at >= versions.get(key, -1):
                state[key] = value
                versions[key] = requested_at
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(state, stream, separators=(",", ":"))
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


if __name__ == "__main__":
    action, *args = sys.argv[1:]
    if action == "clock":
        print(time.monotonic_ns())
    elif action == "update":
        path, station, requested_at = args
        update_state(path, station, int(requested_at), json.load(sys.stdin))
    else:
        raise ValueError("Unknown playback mode action")

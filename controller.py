#!/usr/bin/env python3
import os
import time
import json
import hmac
import hashlib
import requests
from pathlib import Path


def load_env_file(path: Path) -> dict:
    """
    Minimal .env parser: KEY=VALUE lines, ignores blanks/comments.
    Strips surrounding quotes.
    """
    env = {}
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        env[k] = v
    return env


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(secret: bytes, msg: bytes) -> str:
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def build_headers(device_id: str, secret: str, body_bytes: bytes) -> dict:
    ts = str(int(time.time()))
    body_hash = sha256_hex(body_bytes)
    canonical = f"{ts}.{body_hash}".encode("utf-8")
    sig = hmac_sha256_hex(secret.encode("utf-8"), canonical)

    return {
        "Content-Type": "application/json",
        "X-Device-Id": device_id,
        "X-Timestamp": ts,
        "X-Signature": sig,
    }


def main():
    # config.env in the same dir as this script
    here = Path(__file__).resolve().parent
    cfg = load_env_file(here / "config.env")

    api_base = cfg.get("API_BASE", "").rstrip("/")
    if not api_base:
        raise ValueError("API_BASE missing in config.env")

    # DEVICE_ID = hostname
    device_id = os.uname().nodename  # same as `hostname`

    # secret = "pi's device id" (assuming your config.env has ID=...)
    secret = cfg.get("ID") or cfg.get("DEVICE_SECRET") or cfg.get("SECRET")
    if not secret:
        raise ValueError("Secret missing in config.env (expected ID=... or DEVICE_SECRET=... or SECRET=...)")

    interval = int(cfg.get("HEARTBEAT_SECONDS", "10"))  # default 10 seconds
    url = f"{api_base}/api/v1/device/askforevent"
    payload = {}

    while True:
        # IMPORTANT: sign exact bytes that you send
        body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = build_headers(device_id, secret, body_bytes)

        try:
            r = requests.post(url, data=body_bytes, headers=headers, timeout=5)
            print(f"[heartbeat] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[heartbeat] request failed: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    main()

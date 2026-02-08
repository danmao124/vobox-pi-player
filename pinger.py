#!/usr/bin/env python3
"""
Heartbeat service that periodically sends requests to the Venditt API.
"""
import time
from pathlib import Path
from api_client import load_env_file, api_post, get_device_credentials


def main():
    # config.env in the same dir as this script
    here = Path(__file__).resolve().parent
    cfg = load_env_file(here / "config.env")

    api_base = cfg.get("API_BASE", "")
    if not api_base:
        raise ValueError("API_BASE missing in config.env")

    device_id, secret = get_device_credentials()

    interval = int(cfg.get("HEARTBEAT_SECONDS", "10"))  # default 10 seconds
    url = f"{api_base}/device/askforevent"
    payload = {}

    print(f"[heartbeat] url: {url}")

    while True:
        try:
            r = api_post(url, payload, device_id=device_id, secret=secret, timeout=5)
            print(f"[heartbeat] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[heartbeat] request failed: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Shared API client functions for making authenticated requests to the Venditt API.
"""
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
    """Compute SHA256 hash of data and return as hex string."""
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(secret: bytes, msg: bytes) -> str:
    """Compute HMAC-SHA256 of message with secret and return as hex string."""
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def build_headers(device_id: str, secret: str, payload: dict, debug: bool = False) -> tuple[dict, bytes]:
    """
    Build request headers and body bytes from payload.
    Returns (headers_dict, body_bytes) to ensure signed bytes match sent bytes.
    
    Args:
        device_id: Device identifier (typically hostname)
        secret: Secret key for HMAC signing (typically machine-id)
        payload: Dictionary to send as JSON body
        debug: If True, print debug information
    
    Returns:
        Tuple of (headers_dict, body_bytes)
    """
    # IMPORTANT: sign exact bytes that you send
    body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    
    timestamp = str(int(time.time()))
    body_hash = sha256_hex(body_bytes)
    canonical = f"{timestamp}.{body_hash}".encode("utf-8")
    signature = hmac_sha256_hex(secret.encode("utf-8"), canonical)

    if debug:
        print("timestamp", timestamp)
        print("deviceSecret", secret)
        print("canonical", canonical)
        print("signature", signature)

    headers = {
        "Content-Type": "application/json",
        "X-Device-Id": device_id,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }
    
    return headers, body_bytes


def get_device_credentials() -> tuple[str, str]:
    """
    Get device ID and secret from system.
    
    Returns:
        Tuple of (device_id, secret)
    """
    # DEVICE_ID = hostname
    device_id = os.uname().nodename  # same as `hostname`

    # secret = machine-id from /etc/machine-id
    machine_id_path = Path("/etc/machine-id")
    if not machine_id_path.exists():
        raise FileNotFoundError("Missing /etc/machine-id file")
    secret = machine_id_path.read_text().strip()
    if not secret:
        raise ValueError("Empty machine-id in /etc/machine-id")
    
    return device_id, secret


def api_post(url: str, payload: dict, device_id: str = None, secret: str = None, 
             timeout: float = 5.0, debug: bool = False) -> requests.Response:
    """
    Make an authenticated POST request to the API.
    
    Args:
        url: Full URL to POST to
        payload: Dictionary to send as JSON body
        device_id: Device ID (if None, will be fetched from system)
        secret: Secret key (if None, will be fetched from system)
        timeout: Request timeout in seconds
        debug: If True, print debug information
    
    Returns:
        requests.Response object
    """
    if device_id is None or secret is None:
        device_id, secret = get_device_credentials()
    
    headers, body_bytes = build_headers(device_id, secret, payload, debug=debug)
    return requests.post(url, data=body_bytes, headers=headers, timeout=timeout)

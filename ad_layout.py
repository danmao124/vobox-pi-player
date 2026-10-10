#!/usr/bin/env python3
"""Compile config.env LAYOUT data to the website's v1 base64url wire format.

Grammar/geometry follow vobox-website scripts/ad-layout.mjs; validation follows
src/app/module/advertisement/tvads/ad-layout.mjs in authenticated kiosk mode.
Keep this standard-library-only so deployment needs no Node or website checkout.
"""
import base64
import binascii
import json
import math
import re
import sys

from playback_mode import station_ids


MAX_PANELS = 16
MAX_ENCODED_LENGTH = 8192
MAX_SPLIT_LENGTH = 2048
MAX_DEPTH = 16
FITS = ("contain", "cover", "stretch")
# JavaScript's whitespace set (including BOM), used by the website's grammar.
WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
STATION = re.compile(r"[^" + WHITESPACE + r"|/():]+")
INVALID_STATION = re.compile("[" + WHITESPACE + r",/?#&=+%\\\x00-\x1f\x7f]")
WEIGHT = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")


def js_length(value):
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def validate_layout(value, stations):
    """Normalize kiosk IDs, preserving panel/layer order and authorization."""
    if not isinstance(value, dict) or type(value.get("v")) not in (int, float) or value["v"] != 1:
        raise ValueError("Unsupported layout version; expected v:1.")
    if value.keys() - {"v", "panels"}:
        raise ValueError("Unknown layout option; only v and panels are supported.")
    panels = value.get("panels")
    if not isinstance(panels, list) or not 1 <= len(panels) <= MAX_PANELS:
        raise ValueError("A layout needs 1–16 panels.")
    result, seen = [], set()
    for panel in panels:
        if not isinstance(panel, dict) or panel.keys() - {"station", "x", "y", "w", "h", "fit"}:
            raise ValueError("Invalid panel options.")
        station = panel.get("station")
        if (not isinstance(station, str) or not station or js_length(station) > 128
                or station in (".", "..") or INVALID_STATION.search(station)):
            raise ValueError("Each panel needs a valid station ID (at most 128 characters, no URL separators).")
        station = station.upper()
        if js_length(station) > 128:
            raise ValueError("Normalized panel station IDs must be at most 128 characters.")
        if station in seen:
            raise ValueError("Repeated stations are supported only on public links; authenticated Pi kiosks "
                             "require one panel per station to avoid competing playback reports.")
        seen.add(station)
        coords = [panel.get(key) for key in ("x", "y", "w", "h")]
        if not all(type(number) is int or (type(number) is float and math.isfinite(number)) for number in coords):
            raise ValueError("Panel rectangles must contain finite numeric x, y, w and h.")
        x, y, w, h = coords
        if (x < 0 or y < 0 or w <= 0 or h <= 0 or any(number > 100.000001 for number in coords)
                or x + w > 100.000001 or y + h > 100.000001):
            raise ValueError("Panel rectangles must have positive sizes and stay within 0–100%.")
        item = dict(station=station, x=x, y=y, w=w, h=h)
        if "fit" in panel:
            if panel["fit"] not in FITS:
                raise ValueError("Fit must be contain, cover, or stretch.")
            item["fit"] = panel["fit"]
        result.append(item)
    allowed = set(stations)
    if seen != allowed:
        # ascii() escapes any control characters in the configured station list.
        missing = ", ".join(ascii(id_) for id_ in sorted(allowed - seen)) or "none"
        unexpected = ", ".join(ascii(id_) for id_ in sorted(seen - allowed)) or "none"
        raise ValueError(f"Layout stations must match ID exactly (missing: {missing}; unexpected: {unexpected}).")
    return {"v": 1, "panels": result}


def parse_split(source):
    """Parse a bounded split expression as data; mixed axes need parentheses."""
    if not source.strip(WHITESPACE) or js_length(source) > MAX_SPLIT_LENGTH:
        raise ValueError("Provide a split expression of at most 2048 characters.")
    offset, leaves = 0, 0

    def skip():
        nonlocal offset
        while offset < len(source) and source[offset] in WHITESPACE:
            offset += 1

    def peek():
        return source[offset:offset + 1]

    def fail(message):
        raise ValueError(f"{message} (position {js_length(source[:offset]) + 1}).")

    def term(depth):
        nonlocal offset, leaves
        if depth > MAX_DEPTH:
            fail("Splits may nest at most 16 levels")
        skip()
        if peek() == "(":
            offset += 1
            node = expression(depth + 1)
            skip()
            if peek() != ")":
                fail("Expected a closing parenthesis")
            offset += 1
        else:
            match = STATION.match(source, offset)
            if not match:
                fail("Expected a station ID or a grouped split")
            node = {"station": match[0], "weight": 1}
            offset = match.end()
            leaves += 1
            if leaves > MAX_PANELS:
                fail("A layout supports at most 16 panels")
        skip()
        if peek() == ":":
            offset += 1
            skip()
            match = WEIGHT.match(source, offset)
            if not match:
                fail("Expected a positive split weight")
            node["weight"] = float(match[0])
            offset = match.end()
            if not 0 < node["weight"] <= 1000000:
                fail("Split weights must be greater than zero and at most 1000000")
        return node

    def expression(depth):
        nonlocal offset
        children, axis = [term(depth)], None
        skip()
        while peek() in ("|", "/"):
            next_axis = peek()
            offset += 1
            if axis and axis != next_axis:
                fail("Use parentheses when mixing | and /")
            axis = next_axis
            children.append(term(depth))
            skip()
        return {"axis": axis, "children": children, "weight": 1} if axis else children[0]

    tree = expression(0)
    skip()
    if offset != len(source):
        fail("Unexpected text")
    panels = []

    def rounded(value):
        # Match Math.round for nonnegative geometry, not Python's ties-to-even.
        scaled = value * 1000000
        integer = math.floor(scaled)
        value = (integer + (scaled - integer >= 0.5)) / 1000000
        return int(value) if value.is_integer() else value

    def place(node, x, y, w, h):
        if "station" in node:
            panels.append(dict(station=node["station"], x=rounded(x), y=rounded(y),
                               w=rounded(rounded(x + w) - rounded(x)),
                               h=rounded(rounded(y + h) - rounded(y))))
            return
        # Accumulate like JS reduce; newer Python sum uses compensated addition.
        total = 0
        for child in node["children"]:
            total += child["weight"]
        used = 0
        for child in node["children"]:
            start = used / total
            used += child["weight"]
            end = used / total
            if node["axis"] == "|":
                place(child, x + w * start, y, w * (end - start), h)
            else:
                place(child, x, y + h * start, w, h * (end - start))

    place(tree, 0, 0, 100, 100)
    return {"v": 1, "panels": panels}


def decode_layout(encoded):
    if (not encoded or len(encoded) > MAX_ENCODED_LENGTH or len(encoded) % 4 == 1
            or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded)):
        raise ValueError("Encoded layout must be unpadded base64url JSON, at most 8192 characters.")

    def invalid_constant(_):
        raise ValueError("Invalid JSON number")

    try:
        data = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        return json.loads(data.decode("utf-8-sig"), parse_constant=invalid_constant)
    except (ValueError, binascii.Error, RecursionError):
        raise ValueError("Layout could not be decoded as UTF-8 JSON.") from None


def compile_layout(source, station_config):
    """Return an encoded layout, or empty string for the legacy automatic row."""
    source = source.strip(WHITESPACE)
    if not source:
        return ""
    if source.startswith("s1:"):
        raise ValueError("s1: layouts are not supported by the website; use a split expression or base64url: value.")
    value = decode_layout(source[len("base64url:"):]) if source.startswith("base64url:") else parse_split(source)
    layout = validate_layout(value, station_ids(station_config))
    data = json.dumps(layout, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8", errors="backslashreplace")
    encoded = base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
    if len(encoded) > MAX_ENCODED_LENGTH:
        raise ValueError("Layout is too large for a URL (maximum 8192 encoded characters).")
    return encoded


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: ad_layout.py ID < layout.txt")
        # Read a bounded value on stdin; no shell interpretation or credentials.
        source = sys.stdin.read(MAX_ENCODED_LENGTH + len("base64url:") + 2)
        if len(source) > MAX_ENCODED_LENGTH + len("base64url:"):
            raise ValueError("LAYOUT is too long (maximum 8192 encoded characters).")
        print(compile_layout(source, sys.argv[1]))
    except (ValueError, OverflowError) as error:
        print(f"ERROR: invalid LAYOUT in config.env: {error}", file=sys.stderr)
        sys.exit(1)

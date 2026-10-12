#!/usr/bin/env python3
"""Apply explicit kernel video= resolutions inside the authenticated X session."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Optional


class DisplayModeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequestedMode:
    connector: str
    width: int
    height: int
    rate: Optional[int]
    cvt: bool
    reduced: bool
    flags: str

    @property
    def size(self):
        return f"{self.width}x{self.height}"


def requested_modes(cmdline):
    # Never source/eval the kernel command line; unrelated fields may be private.
    entries = {}
    for token in shlex.split(cmdline):
        if not token.startswith("video="):
            continue
        value = token[6:]
        connector, sep, mode = value.partition(":")
        if not sep:
            connector, mode = "", connector
        entries[connector] = mode

    result = []
    for connector, mode in entries.items():
        # Rotation/margins are console options; website rotation remains separate.
        mode = mode.split(",", 1)[0]
        if not re.match(r"\d+x", mode):
            continue  # Enable/disable-only and named TV modes specify no dimensions.
        match = re.fullmatch(
            r"([1-9]\d*)x([1-9]\d*)(M?)(R?)(?:-\d+)?(?:@([1-9]\d*))?([imeDd]*)", mode
        )
        if not match or not re.fullmatch(r"[A-Za-z0-9_-]*", connector):
            raise DisplayModeError("Invalid numeric video= resolution in /proc/cmdline")
        width, height, cvt, reduced, rate, flags = match.groups()
        if "d" in flags:
            continue
        result.append(RequestedMode(connector, int(width), int(height),
                                    int(rate) if rate else None, bool(cvt), bool(reduced), flags))
    return result


def run(*args):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=5, env={**os.environ, "LC_ALL": "C"}).stdout
    except FileNotFoundError as exc:
        raise DisplayModeError(f"Required display tool is missing: {args[0]}") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DisplayModeError(f"Display command failed: {' '.join(args)}") from exc


def x_outputs(text):
    outputs = {}
    current = None
    for line in text.splitlines():
        header = re.match(r"^(\S+) (connected|disconnected)\b(.*)", line)
        if header:
            name, status, rest = header.groups()
            geometry = re.search(r"\b(\d+)x(\d+)[+-]\d+[+-]\d+", rest)
            current = {"connected": status == "connected", "size": None, "modes": {}}
            if geometry:
                current["size"] = f"{geometry[1]}x{geometry[2]}"
            outputs[name] = current
        elif current is not None and line.startswith(" "):
            fields = line.split()
            if len(fields) < 2:
                continue
            rates = []
            for value in fields[1:]:
                match = re.fullmatch(r"(\d+(?:\.\d+)?)([*+]*)", value)
                if match:
                    rates.append((float(match[1]), "*" in match[2]))
            if rates:
                current["modes"][fields[0]] = rates
    return outputs


def output_for(mode, outputs):
    if not mode.connector:
        names = [name for name, output in outputs.items() if output["connected"]]
        if len(names) != 1:
            raise DisplayModeError("A global video= resolution requires exactly one connected X output")
        return names[0]
    # DRM uses HDMI-A-1; the X modesetting driver commonly calls it HDMI-1.
    for name in dict.fromkeys((mode.connector, mode.connector.replace("HDMI-A-", "HDMI-"))):
        if name in outputs:
            if not outputs[name]["connected"]:
                return None
            return name
    raise DisplayModeError(f"Cannot map configured connector {mode.connector} to an X output")


def existing_mode(mode, output):
    candidates = []
    for name, rates in output["modes"].items():
        if not re.fullmatch(re.escape(mode.size) + r"(?:i|_.*)?", name) and name != generated_mode_name(mode):
            continue
        # Interlaced modes must not be substituted for progressive ones.
        if bool(re.search(r"(?:i$|_.*i)", name, re.I)) != ("i" in mode.flags):
            continue
        distance = min(abs(rate - mode.rate) for rate, _ in rates) if mode.rate else 0
        if distance <= 0.5:
            candidates.append((distance, name != mode.size, name))
    return min(candidates)[2] if candidates else None


def generated_mode_name(mode):
    return f"venditt-{mode.size}-{mode.rate or 60}" + ("-rb" if mode.reduced else "")


def add_cvt_mode(mode, output):
    if not mode.cvt or "i" in mode.flags or "m" in mode.flags:
        raise DisplayModeError(f"Configured mode {mode.size} is unavailable on {output}")
    args = ["cvt"]
    if mode.reduced:
        args.append("-r")
    args += [str(mode.width), str(mode.height), str(mode.rate or 60)]
    generated = run(*args)
    for line in generated.splitlines():
        fields = shlex.split(line)
        if fields and fields[0] == "Modeline":
            timing = fields[2:]
            if (len(timing) < 9 or timing[1] != str(mode.width)
                    or timing[5] != str(mode.height)):
                raise DisplayModeError("Generated CVT timing does not match the configured resolution")
            name = generated_mode_name(mode)
            run("xrandr", "--newmode", name, *timing)
            run("xrandr", "--addmode", output, name)
            return name
    raise DisplayModeError("cvt did not return a display modeline")


def apply_modes(cmdline):
    modes = requested_modes(cmdline)
    if not modes:
        return
    outputs = x_outputs(run("xrandr", "--current"))
    for mode in modes:
        output = output_for(mode, outputs)
        if output is None:
            print(f"Configured display {mode.connector} is disconnected; skipping", flush=True)
            continue
        name = existing_mode(mode, outputs[output])
        if name is None:
            # X modes are global: another configured output may have created it.
            name = generated_mode_name(mode)
            if any(name in item["modes"] for item in outputs.values()):
                run("xrandr", "--addmode", output, name)
            else:
                name = add_cvt_mode(mode, output)
        args = ["xrandr", "--output", output, "--mode", name]
        if mode.rate:
            args += ["--rate", str(mode.rate)]
        run(*args)
        outputs = x_outputs(run("xrandr", "--current"))
        active = outputs.get(output, {})
        rates = [rate for rate, current in active.get("modes", {}).get(name, []) if current]
        if (active.get("size") != mode.size or not rates
                or (mode.rate and not any(abs(rate - mode.rate) <= 0.5 for rate in rates))):
            raise DisplayModeError(f"X did not activate the configured mode {mode.size} on {output}")
        suffix = f" at {mode.rate} Hz" if mode.rate else ""
        print(f"Applied boot display mode: {output} {mode.size}{suffix}", flush=True)


def main():
    try:
        apply_modes(Path("/proc/cmdline").read_text())
    except (OSError, ValueError, DisplayModeError) as exc:
        print(f"ERROR: cannot apply configured display mode: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

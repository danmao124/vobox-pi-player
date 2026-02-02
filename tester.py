import serial, time, re

PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"
BAUD = 115200
SNIFF_SECONDS = 15

POLL_HEX = {"12"}  # drop spammy poll byte(s)

def parse_line(line: bytes):
    """
    Expected examples:
      b'x,00,0002599672,12,'
      b'x,80,0002632404,0301f4'
      b'x,ACK'
      b'v,4.0.2.0,...'
    """
    s = line.decode(errors="replace").strip()
    if not s:
        return None
    parts = s.split(",")
    return s, parts

def hex_to_bytes(hexstr: str) -> bytes:
    hexstr = hexstr.strip().lower().replace("0x", "")
    hexstr = re.sub(r"[^0-9a-f]", "", hexstr)
    if len(hexstr) == 0:
        return b""
    if len(hexstr) % 2 == 1:
        # if odd, left-pad (rare, but avoids crash)
        hexstr = "0" + hexstr
    return bytes.fromhex(hexstr)

with serial.Serial(PORT, baudrate=BAUD, timeout=0.3, write_timeout=0.3) as s:
    print("opened", PORT, "baud", BAUD, flush=True)

    # Make the CDC-ACM line behave after previous abrupt exits
    try:
        s.dtr = False
        s.rts = False
        time.sleep(0.05)
        s.reset_input_buffer()
        s.reset_output_buffer()
        s.dtr = True
        time.sleep(0.05)
    except Exception:
        pass

    def send(cmd: str):
        s.write((cmd + "\r").encode())
        s.flush()

    def drain(seconds=0.6):
        end = time.time() + seconds
        while time.time() < end:
            s.readline()

    # 1) hard-reset sniff state
    send("X,0")
    drain(0.6)

    # 2) get version cleanly
    send("V")
    vline = b""
    t_end = time.time() + 1.2
    while time.time() < t_end:
        line = s.readline()
        if line.startswith(b"v,"):
            vline = line
            break
    print("V ->", vline.decode(errors="replace").strip() if vline else "(no version line)", flush=True)

    # 3) start sniff
    send("X,1")
    drain(0.3)

    print(f"=== sniffing ({SNIFF_SECONDS}s) ===", flush=True)

    # Frame builder: collect data bytes until we see ACK, then emit a frame
    cur = bytearray()
    last_ts = None

    end = time.time() + SNIFF_SECONDS
    while time.time() < end:
        line = s.readline()
        if not line:
            continue

        raw, parts = parse_line(line)
        if raw.startswith("v,"):
            # ignore stray version lines
            continue

        # ACK line might be just "x,ACK" or embedded
        if "ACK" in raw:
            if cur:
                print("FRAME:", cur.hex(), flush=True)
                cur.clear()
            continue

        # Typical sniff line: x,00,<ts>,<hex...>
        # Grab last field that looks hex-ish
        payload = parts[-1].strip()
        payload = payload.strip()  # may be '12' or '0301f4' etc
        payload = payload.replace("\r", "").replace("\n", "")
        payload = payload.strip(",")

        # Drop common poll spam
        if payload.lower() in POLL_HEX:
            continue

        # Some lines like: x,00,....,13,0000190101  (two payload-ish fields)
        # If we see that, append both bytes chunks
        if len(parts) >= 5:
            maybe1 = parts[-2].strip().strip(",")
            maybe2 = parts[-1].strip().strip(",")
            # only treat as hex if it looks like it
            if re.fullmatch(r"[0-9A-Fa-f]+", maybe1 or ""):
                cur += hex_to_bytes(maybe1)
            if re.fullmatch(r"[0-9A-Fa-f]+", maybe2 or ""):
                cur += hex_to_bytes(maybe2)
        else:
            if re.fullmatch(r"[0-9A-Fa-f]+", payload or ""):
                cur += hex_to_bytes(payload)
            else:
                # If it’s not hex, still print it so we don’t lose info
                print("RAW:", raw, flush=True)

    # Optional: stop sniff on exit so next run starts clean
    send("X,0")
    drain(0.2)

print("done.", flush=True)

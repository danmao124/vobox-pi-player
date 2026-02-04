import serial, time, re

PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"
BAUD = 115200
SNIFF_SECONDS = 15

POLL_BYTE = 0x12  # MDB poll byte

def parse_line(line: bytes):
    """
    Expected examples:
      b'x,00,0002599672,12,'
      b'x,80,0002632404,0301f4'
      b'x,00,0002671896,13,0000190101'
      b'x,ACK'
      b'v,4.0.2.0,...'
    """
    s = line.decode(errors="replace").strip()
    if not s:
        return None
    return s, s.split(",")

def hex_to_bytes(hexstr: str) -> bytes:
    hexstr = (hexstr or "").strip().lower().replace("0x", "")
    hexstr = re.sub(r"[^0-9a-f]", "", hexstr)
    if not hexstr:
        return b""
    if len(hexstr) % 2 == 1:
        hexstr = "0" + hexstr
    return bytes.fromhex(hexstr)

def strip_leading_polls(b: bytes) -> bytes:
    i = 0
    while i < len(b) and b[i] == POLL_BYTE:
        i += 1
    return b[i:]

def is_all_polls(b: bytes) -> bool:
    return len(b) > 0 and all(x == POLL_BYTE for x in b)

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

    # ---- helper: append hex tokens, never poll-only tokens ----
    def maybe_add_hex(tok: str):
        tok = (tok or "").strip().strip(",")
        if not tok:
            return
        if not re.fullmatch(r"[0-9A-Fa-f]+", tok):
            return
        # drop pure poll tokens
        if tok.lower() == "12":
            return
        cur.extend(hex_to_bytes(tok))

    # 1) reset sniff state
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

    # Frame builder: collect data bytes; on ACK emit a frame
    cur = bytearray()

    end = time.time() + SNIFF_SECONDS
    while time.time() < end:
        line = s.readline()
        if not line:
            continue

        parsed = parse_line(line)
        if not parsed:
            continue
        raw, parts = parsed

        if raw.startswith("v,"):
            continue  # ignore stray version lines

        # ACK terminates a frame
        if "ACK" in raw:
            if cur:
                frame = bytes(cur)
                cur.clear()

                # remove leading poll spam
                frame2 = strip_leading_polls(frame)

                # ignore poll-only frames
                if not frame2 or is_all_polls(frame2):
                    continue

                print("FRAME:", frame2.hex(), flush=True)
            continue

        # Typical sniff lines are comma-separated; payload is usually last field,
        # but sometimes there are two payload fields (e.g. "... ,13,0000190101")
        # We attempt to append both last-2 and last fields if they look hex.
        if len(parts) >= 5:
            maybe_add_hex(parts[-2])
            maybe_add_hex(parts[-1])
        else:
            # last field as payload
            payload = (parts[-1] if parts else "").strip().strip(",")
            maybe_add_hex(payload)

    # Stop sniff on exit so next run starts clean
    send("X,0")
    drain(0.2)

print("done.", flush=True)

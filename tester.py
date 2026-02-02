import serial, time, binascii

PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"

def hexdump(b: bytes, maxlen=256):
    b = b[:maxlen]
    return binascii.hexlify(b).decode()

def run(baud: int):
    print(f"\n=== baud {baud} ===", flush=True)

    s = serial.Serial(
        PORT,
        baudrate=baud,
        timeout=0.05,          # short poll
        write_timeout=0.2,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )

    # Prevent “open toggles reset” vibes on some boards
    try:
        s.setDTR(True)
        s.setRTS(True)
    except Exception:
        pass

    print("opened", flush=True)

    s.reset_input_buffer()
    s.reset_output_buffer()

    def cmd(c: str, read_window=0.4):
        payload = (c + "\r").encode()     # IMPORTANT: CR only
        s.write(payload)
        s.flush()

        end = time.time() + read_window
        out = b""
        while time.time() < end:
            chunk = s.read(4096)
            if chunk:
                out += chunk
                # small “extend window” if data is still flowing
                end = time.time() + 0.08

        # Pretty-print line-ish replies, but keep raw bytes too
        printable = out.replace(b"\r", b"\\r").replace(b"\n", b"\\n")
        print(f"cmd {c!r} -> {printable!r}", flush=True)
        return out

    # Ping + enable sniff
    cmd("V")
    cmd("X,1")

    # Stream read
    t_end = time.time() + 4.0
    total = 0
    sample = b""

    while time.time() < t_end:
        n = s.in_waiting
        b = s.read(n if n else 4096)
        if b:
            total += len(b)
            if len(sample) < 400:
                sample += b

    print("stream_bytes:", total, flush=True)
    print("sample_ascii:", sample[:200].replace(b"\r", b"\\r").replace(b"\n", b"\\n"), flush=True)
    print("sample_hex:", hexdump(sample, 256), flush=True)

    s.close()

for b in (115200, 57600, 38400, 19200, 9600):
    run(b)

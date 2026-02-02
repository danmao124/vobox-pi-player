import serial, time

PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"
SNIFF_SECONDS = 15

def run(baud: int):
    print(f"\n=== baud {baud} ===", flush=True)

    s = serial.Serial(PORT, baudrate=baud, timeout=0.3, write_timeout=0.3)
    print("opened", flush=True)

    # Optional: helps recover after previous abrupt exits on some CDC-ACM devices
    try:
        s.dtr = False
        s.rts = False
        time.sleep(0.05)
    except Exception:
        pass

    s.reset_input_buffer()
    s.reset_output_buffer()

    try:
        s.dtr = True
        time.sleep(0.05)
    except Exception:
        pass

    def cmd(c: str, wait: float = 0.6) -> bytes:
        payload = (c + "\r").encode()   # CR ONLY
        s.write(payload)
        s.flush()

        end = time.time() + wait
        out = b""
        while time.time() < end:
            line = s.readline()
            if line:
                out += line
        return out

    v = cmd("V")
    print("V ->", v.decode(errors="replace").strip(), flush=True)

    x = cmd("X,1")
    if b"ACK" not in x.lower() and b"ack" not in x:
        # some firmwares reply lowercase "x,ACK" etc; we just print whatever we got
        print("X,1 ->", x.decode(errors="replace").strip(), flush=True)
        if not x:
            print("NO x,ACK from X,1 (port/device state). Try power-cycle / close conflicts.", flush=True)
            s.close()
            return
    else:
        print("X,1 ->", x.decode(errors="replace").strip(), flush=True)

    print(f"=== sniffing ({SNIFF_SECONDS}s) ===", flush=True)
    end = time.time() + SNIFF_SECONDS
    while time.time() < end:
        line = s.readline()
        if line:
            print(line.decode(errors="replace").rstrip("\r\n"), flush=True)

    # (Optional) if firmware supports stopping sniff, this makes next run more reliable
    # cmd("X,0", wait=0.3)

    s.close()
    print("done.", flush=True)

if __name__ == "__main__":
    for b in (115200, 57600, 38400, 19200, 9600):
        run(b)

import serial, time, sys

PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"

def run(baud: int):
    print(f"\n=== baud {baud} ===", flush=True)

    # write_timeout prevents blocking forever on write()
    s = serial.Serial(PORT, baudrate=baud, timeout=0.2, write_timeout=0.2)
    print("opened", flush=True)

    s.reset_input_buffer()
    s.reset_output_buffer()

    def cmd(c: str):
        # CRLF is safest for many embedded serial parsers
        payload = (c + "\r\n").encode()
        s.write(payload)
        s.flush()
        time.sleep(0.2)
        out = s.read(4096)  # returns quickly because timeout=0.2
        print(f"cmd {c!r} -> {out!r}", flush=True)

    # basic ping + enable sniff
    cmd("V")
    cmd("X,1")

    # read stream for a few seconds
    t = time.time()
    total = 0
    sample = b""
    while time.time() - t < 4:
        b = s.read(4096)
        if b:
            total += len(b)
            if len(sample) < 400:
                sample += b

    print("stream_bytes:", total, flush=True)
    print("sample:", sample, flush=True)
    s.close()

for b in (115200, 57600, 38400, 19200, 9600):
    run(b)

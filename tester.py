sudo python3 - <<'PY'
import serial, time

PORT="/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"

def run(baud):
    print("\n=== baud", baud, "===")
    s = serial.Serial(PORT, baudrate=baud, timeout=0.2)
    s.reset_input_buffer()
    s.reset_output_buffer()

    def cmd(c):
        s.write((c+"\n").encode())
        time.sleep(0.15)
        out = s.read(4096)
        print(f"cmd {c!r} -> {out!r}")

    cmd("V")      # version / identity
    cmd("X,1")    # enable sniff stream

    t = time.time()
    total = 0
    sample = b""
    while time.time()-t < 4:
        b = s.read(4096)
        if b:
            total += len(b)
            if len(sample) < 600:
                sample += b
    print("stream_bytes:", total)
    print("sample:", sample[:600])
    s.close()

for b in (115200, 57600, 38400, 19200, 9600):
    run(b)
PY

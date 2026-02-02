import serial, time

PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"
BAUD = 115200

def send(s: serial.Serial, cmd: str):
    s.write((cmd + "\r").encode())
    s.flush()

with serial.Serial(PORT, BAUD, timeout=1.0, write_timeout=1.0) as s:
    s.reset_input_buffer()
    s.reset_output_buffer()

    # Version
    send(s, "V")
    print("V ->", s.readline().decode(errors="replace").rstrip())

    # Enable sniff (should answer x,ACK)
    send(s, "X,1")
    t0 = time.time()
    got_ack = False
    while time.time() - t0 < 2.0:
        line = s.readline()
        if not line:
            continue
        text = line.decode(errors="replace").rstrip()
        print(text)
        if text.startswith("x,ACK"):
            got_ack = True
            break

    if not got_ack:
        print("NO x,ACK from X,1 (likely line ending / port conflict / device not accepting cmd)")
        raise SystemExit(1)

    print("=== sniffing (10s) ===")
    end = time.time() + 10
    while time.time() < end:
        line = s.readline()
        if line:
            print(line.decode(errors="replace").rstrip())

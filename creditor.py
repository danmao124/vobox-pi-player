#!/usr/bin/env python3
import serial, time, re, argparse
from decimal import Decimal

DEFAULT_PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"
BAUD = 115200

# Qibixx Cashless Slave (to VMC) lines we care about
VEND_REQ_RE = re.compile(r"^c,STATUS,VEND,([^,]+),([^,]+)\s*$")

def clean_line(b: bytes) -> str:
    return b.decode(errors="replace").strip()

def open_serial(port: str):
    s = serial.Serial(port=port, baudrate=BAUD, timeout=0.3, write_timeout=0.3)
    # Make CDC-ACM behave nicely after abrupt exits
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
    return s

def send(s: serial.Serial, cmd: str):
    # Use CRLF (Qibixx examples often require \r\n)
    s.write((cmd + "\r\n").encode("ascii"))
    s.flush()

def drain(s: serial.Serial, seconds=0.6):
    end = time.time() + seconds
    while time.time() < end:
        s.readline()

def get_version(s: serial.Serial, wait_s=1.2):
    send(s, "V")
    t_end = time.time() + wait_s
    while time.time() < t_end:
        line = s.readline()
        if line.startswith(b"v,"):
            return clean_line(line)
    return None

def parse_amount(s: str) -> Decimal:
    return Decimal(s.strip())

def fmt_money(x: Decimal) -> str:
    return f"{x:.2f}"

def main():
    ap = argparse.ArgumentParser(description="Qibixx split-mode: Cashless Slave credit/approve VMC vend requests")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--auto", action="store_true", help="Auto-approve vend requests")
    ap.add_argument("--max-amount", default=None, help="Deny if amount > this (e.g. 2.00)")
    ap.add_argument("--allow-products", default=None, help="Comma list of allowed product_ids (e.g. 2,10)")
    ap.add_argument("--duration", type=int, default=0, help="Seconds to run (0=forever)")
    ap.add_argument("--debug-raw", action="store_true", help="Print all c,/x,/d, lines")
    args = ap.parse_args()

    max_amount = Decimal(args.max_amount) if args.max_amount else None
    allow_products = set(p.strip() for p in args.allow_products.split(",")) if args.allow_products else None

    with open_serial(args.port) as s:
        print("opened", args.port, "baud", BAUD, flush=True)

        # Hard reset state: stop sniff, disable slave, clear buffers
        send(s, "X,0")
        send(s, "C,0")
        time.sleep(0.2)
        drain(s, 0.6)

        v = get_version(s)
        print("V ->", v if v else "(no version line)", flush=True)

        # Enable cashless slave (device presented to VMC)
        send(s, "C,1")
        print("sent: C,1 (enable cashless slave)", flush=True)

        # Wait for VMC to enable us: some firmwares print ENABLED, some go straight to IDLE
        enabled = False
        t0 = time.time()
        while time.time() - t0 < 30:
            line = s.readline()
            if not line:
                continue
            txt = clean_line(line)
            if args.debug_raw and txt:
                if txt.startswith(("c,", "x,", "d,")):
                    print(txt, flush=True)

            if txt in ("c,STATUS,ENABLED", "c,STATUS,IDLE"):
                enabled = True
                print("✅ cashless slave active:", txt, flush=True)
                break

            # Some units briefly say INACTIVE then later IDLE; keep waiting.

        if not enabled:
            print("🛑 did not reach ENABLED/IDLE within 30s. If you see INACTIVE/DISABLED only, it's wiring/mode.", flush=True)

        print("=== waiting for vend requests from VMC ===", flush=True)

        end_time = time.time() + args.duration if args.duration > 0 else None

        while True:
            if end_time and time.time() > end_time:
                break

            line = s.readline()
            if not line:
                continue
            txt = clean_line(line)
            if not txt:
                continue

            if args.debug_raw and txt.startswith(("c,", "x,", "d,")):
                print(txt, flush=True)

            # Vend request from VMC (always-idle style)
            m = VEND_REQ_RE.match(txt)
            if m:
                amt_s, product_id = m.group(1), m.group(2)

                try:
                    amt = parse_amount(amt_s)
                except Exception:
                    print("🛑 amount parse failed:", txt, flush=True)
                    continue

                decision = "APPROVE"
                reason = ""

                if max_amount is not None and amt > max_amount:
                    decision = "DENY"
                    reason = f"amount {amt} > max {max_amount}"

                if allow_products is not None and product_id not in allow_products:
                    decision = "DENY"
                    reason = f"product {product_id} not allowed"

                print(f"🧠 VEND REQ amount=${fmt_money(amt)} product_id={product_id} -> {decision}"
                      + (f" ({reason})" if reason else ""), flush=True)

                if args.auto and decision == "APPROVE":
                    cmd = f"C,VEND,{fmt_money(amt)}"
                    send(s, cmd)
                    print(f"😈 sent: {cmd}", flush=True)
                else:
                    send(s, "C,STOP")
                    print("🛑 sent: C,STOP", flush=True)
                continue

            # Completion
            if txt == "c,VEND,SUCCESS":
                print("🧾 VEND SUCCESS", flush=True)
                continue
            if txt.startswith("c,ERR,VEND"):
                print("🛑 VEND ERROR:", txt, flush=True)
                continue

        # Cleanup
        send(s, "X,0")
        send(s, "C,0")
        drain(s, 0.2)
        print("done.", flush=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import serial, time, re, sys, argparse
from decimal import Decimal

DEFAULT_PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"
BAUD = 115200

# Matches: c,STATUS,VEND,<amount>,<product_id>
VEND_REQ_RE = re.compile(r"^c,STATUS,VEND,([^,]+),([^,]+)\s*$")

def now_ms():
    return int(time.time() * 1000)

def clean_line(b: bytes) -> str:
    return b.decode(errors="replace").strip()

def open_serial(port: str):
    s = serial.Serial(port=port, baudrate=BAUD, timeout=0.3, write_timeout=0.3)
    # Make CDC-ACM behave nicely
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
    # Qibixx accepts CR (and often CRLF). Your sniffer uses "\r" so keep that.
    s.write((cmd + "\r").encode("ascii"))
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

def decimal_str(x: Decimal) -> str:
    # Qibixx examples use 2 decimals, but allow general
    # Keep minimal trailing zeros (1.00 stays 1.00; 0.75 stays 0.75)
    return f"{x:.2f}"

def parse_amount(s: str) -> Decimal:
    # supports "1", "1.0", "1.00", etc
    return Decimal(s.strip())

def main():
    ap = argparse.ArgumentParser(description="Qibixx Cashless Slave: auto-approve vend requests (give credit to VMC)")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--sniff", action="store_true", help="Also enable sniffer X,1 (telemetry)")
    ap.add_argument("--auto", action="store_true", help="Auto-approve every vend request")
    ap.add_argument("--max-amount", default=None, help="Optional: deny if amount > this (e.g. 2.00)")
    ap.add_argument("--allow-products", default=None, help="Optional: comma list of allowed product_ids (e.g. 2,5,10)")
    ap.add_argument("--duration", type=int, default=0, help="Seconds to run (0 = forever)")
    args = ap.parse_args()

    max_amount = Decimal(args.max_amount) if args.max_amount else None
    allow_products = set(p.strip() for p in args.allow_products.split(",")) if args.allow_products else None

    with open_serial(args.port) as s:
        print("opened", args.port, "baud", BAUD, flush=True)

        # Stop sniff (clean slate)
        send(s, "X,0")
        drain(s, 0.4)

        v = get_version(s)
        print("V ->", v if v else "(no version line)", flush=True)

        # Enable cashless slave
        # (If slave was previously enabled, you can optionally send C,0 first)
        send(s, "C,0")
        drain(s, 0.2)
        send(s, "C,1")
        print("sent: C,1 (enable cashless slave)", flush=True)

        if args.sniff:
            send(s, "X,1")
            print("sent: X,1 (enable sniffer)", flush=True)

        # Wait until VMC enables the cashless device
        enabled = False
        t0 = time.time()
        while time.time() - t0 < 10:
            line = s.readline()
            if not line:
                continue
            txt = clean_line(line)
            if txt == "c,STATUS,ENABLED":
                enabled = True
                break
            # print chatter (optional)
            if txt.startswith("c,") or txt.startswith("x,"):
                print(txt, flush=True)

        if not enabled:
            print("WARN: did not see c,STATUS,ENABLED within 10s; continuing anyway.", flush=True)

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

            # Vend request from VMC (always-idle flow)
            m = VEND_REQ_RE.match(txt)
            if m:
                amt_s, product_id = m.group(1), m.group(2)
                try:
                    amt = parse_amount(amt_s)
                except Exception:
                    print(f"[{now_ms()}] got vend req but amount parse failed: {txt}", flush=True)
                    continue

                decision = "APPROVE"
                reason = ""

                if max_amount is not None and amt > max_amount:
                    decision = "DENY"
                    reason = f"amount {amt} > max {max_amount}"

                if allow_products is not None and product_id not in allow_products:
                    decision = "DENY"
                    reason = f"product {product_id} not allowed"

                print(f"[{now_ms()}] VEND REQ amount={amt} product_id={product_id} -> {decision}"
                      + (f" ({reason})" if reason else ""), flush=True)

                if args.auto and decision == "APPROVE":
                    cmd = f"C,VEND,{decimal_str(amt)}"
                    send(s, cmd)
                    print(f"sent: {cmd}", flush=True)
                else:
                    # default behavior if not auto: deny to avoid accidental free vends
                    send(s, "C,STOP")
                    print("sent: C,STOP (deny/cancel)", flush=True)

                continue

            # Completion / errors
            if txt == "c,VEND,SUCCESS":
                print(f"[{now_ms()}] VEND SUCCESS", flush=True)
                continue
            if txt.startswith("c,ERR,VEND"):
                print(f"[{now_ms()}] VEND ERROR: {txt}", flush=True)
                continue

            # Print relevant chatter
            if txt.startswith(("c,", "x,", "r,")):
                print(txt, flush=True)

        # Cleanup
        send(s, "X,0")
        send(s, "C,0")
        drain(s, 0.2)
        print("done.", flush=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import serial, time, re, argparse
from decimal import Decimal

PORT = "/dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00"
BAUD = 115200

# VMC-side (cashless slave) patterns
VEND_REQ_RE = re.compile(r"^c,STATUS,VEND,([^,]+),([^,]+)\s*$")
IDLE_CREDIT_RE = re.compile(r"^c,STATUS,IDLE,([^,]+)\s*$")

# Nayax-side (cashless master) patterns (loose; tune after you see real lines)
NAYAX_RESULT_RE = re.compile(r"^d,STATUS,RESULT,([-\d]+)")
NAYAX_ACTIVITY_RE = re.compile(r"^d,STATUS,.*(BEGIN|SESSION|VEND|INIT|IDLE)")

def clean_line(b: bytes) -> str:
    return b.decode(errors="replace").strip()

def send(s: serial.Serial, cmd: str):
    s.write((cmd + "\r\n").encode("ascii"))
    s.flush()

def fmt_money(x: Decimal) -> str:
    return f"{x:.2f}"

def parse_money(s: str) -> Decimal:
    return Decimal(s.strip())

def main():
    ap = argparse.ArgumentParser("Split-mode Nayax<->VMC translator (MVP)")
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--max-credit", default="2.00", help="Credit armed on VMC side after card present")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    max_credit = Decimal(args.max_credit)

    with serial.Serial(args.port, BAUD, timeout=0.3, write_timeout=0.3) as s:
        print("opened", args.port, flush=True)

        # Clean reset
        send(s, "X,0")
        send(s, "C,0")
        send(s, "D,STOP")
        send(s, "D,0")
        time.sleep(0.3)

        # Enable VMC-side cashless slave
        send(s, "C,1")
        print("sent C,1", flush=True)

        # Init Nayax-side cashless master (auto polls)
        send(s, "D,2")
        send(s, "D,READER,1")
        print("sent D,2 + D,READER,1", flush=True)

        enabled = False
        vmc_has_credit = False
        card_present = False

        pending_vend = None   # (price, product_id)
        awaiting_nayax = False
        nayax_deadline = 0.0

        while True:
            line = s.readline()
            if not line:
                # timeout watchdog: if we are waiting for nayax, and it times out -> cancel VMC
                if awaiting_nayax and time.time() > nayax_deadline:
                    print("🛑 Nayax timeout -> C,STOP", flush=True)
                    send(s, "C,STOP")
                    send(s, "D,END")
                    awaiting_nayax = False
                    pending_vend = None
                    card_present = False
                continue

            txt = clean_line(line)
            if not txt:
                continue
            if args.debug and txt[0] in ("c","d","x"):
                print(txt, flush=True)

            # Track VMC enabled
            if txt in ("c,STATUS,ENABLED",):
                enabled = True

            # Track whether we have credit armed (IDLE,<amount>)
            m_idle = IDLE_CREDIT_RE.match(txt)
            if m_idle:
                vmc_has_credit = True

            # Vend request from VMC
            m_v = VEND_REQ_RE.match(txt)
            if m_v:
                price = parse_money(m_v.group(1))
                product_id = m_v.group(2)
                pending_vend = (price, product_id)

                # Request auth from Nayax
                cmd = f"D,REQ,{fmt_money(price)},{product_id}"
                print(f"🧠 VMC wants ${fmt_money(price)} prod={product_id} -> {cmd}", flush=True)
                send(s, cmd)
                awaiting_nayax = True
                nayax_deadline = time.time() + 12.0  # give it ~12s
                continue

            # Nayax activity heuristic: if we see status lines, assume user/card interaction
            if NAYAX_ACTIVITY_RE.match(txt):
                card_present = True

            # Nayax result
            m_r = NAYAX_RESULT_RE.match(txt)
            if m_r and awaiting_nayax and pending_vend:
                code = int(m_r.group(1))
                price, product_id = pending_vend

                if code == 1:
                    print(f"😈 Nayax approved -> C,VEND,{fmt_money(price)}", flush=True)
                    send(s, f"C,VEND,{fmt_money(price)}")
                else:
                    print(f"🛑 Nayax denied(code={code}) -> C,STOP", flush=True)
                    send(s, "C,STOP")

                send(s, "D,END")
                awaiting_nayax = False
                pending_vend = None
                card_present = False
                continue

            # Arm VMC credit only when:
            # - enabled,
            # - card present,
            # - and no credit already armed
            if enabled and card_present and not vmc_has_credit and not awaiting_nayax:
                cmd = f"C,START,{fmt_money(max_credit)}"
                print(f"⚡ arm VMC credit: {cmd}", flush=True)
                send(s, cmd)
                # after START you’ll see c,STATUS,IDLE,<credit>
                vmc_has_credit = True

            # When vend completes, VMC will typically go IDLE then ENABLED; clear credit flag
            if txt == "c,VEND,SUCCESS" or txt.startswith("c,ERR"):
                # let next IDLE/ENABLED lines re-establish state
                vmc_has_credit = False

if __name__ == "__main__":
    main()

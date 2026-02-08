#!/usr/bin/env python3
import serial, time, re, argparse
from decimal import Decimal

BAUD = 115200

# VMC-side patterns
VEND_REQ_RE = re.compile(r"^c,STATUS,VEND,([^,]+),([^,]+)\s*$")
VMC_ENABLED_RE = re.compile(r"^c,STATUS,ENABLED\b")
VMC_IDLE_CREDIT_RE = re.compile(r"^c,STATUS,IDLE,([^,]+)\s*$")
VMC_IDLE_RE = re.compile(r"^c,STATUS,IDLE\b")

# Nayax-side patterns
N_OFF_RE   = re.compile(r"^d,STATUS,OFF\b")
N_INIT_RE  = re.compile(r"^d,STATUS,INIT,(\d+)\b")
N_IDLE_RE  = re.compile(r"^d,STATUS,IDLE\b")
N_RESULT_RE = re.compile(r"^d,STATUS,RESULT,([-\d]+)")
N_ERR_RE   = re.compile(r"^d,ERR,\"([-\d]+)\"")

def clean(b: bytes) -> str:
    return b.decode(errors="replace").strip()

def send(s: serial.Serial, cmd: str):
    s.write((cmd + "\r\n").encode("ascii"))
    s.flush()

def wait_for(s: serial.Serial, regex, timeout_s=5, debug=False):
    end = time.time() + timeout_s
    while time.time() < end:
        line = s.readline()
        if not line:
            continue
        txt = clean(line)
        if debug and txt and txt[0] in ("c","d","x"):
            print(txt, flush=True)
        if regex.match(txt):
            return txt
    return None

def fmt_money(x: Decimal) -> str:
    return f"{x:.2f}"

def parse_money(s: str) -> Decimal:
    return Decimal(s.strip())

def init_nayax_master(s: serial.Serial, debug=False) -> bool:
    # Clean stop/clear
    send(s, "D,STOP")
    send(s, "D,0")
    ok = wait_for(s, N_OFF_RE, timeout_s=8, debug=debug)
    if not ok:
        print("🛑 Nayax master: did not reach STATUS,OFF", flush=True)
        return False

    # Set master
    send(s, "D,2")
    ok = wait_for(s, N_INIT_RE, timeout_s=8, debug=debug)
    if not ok:
        print("🛑 Nayax master: did not reach STATUS,INIT,*", flush=True)
        return False

    # Enable reader
    send(s, "D,READER,1")
    ok = wait_for(s, N_IDLE_RE, timeout_s=10, debug=debug)
    if not ok:
        print("🛑 Nayax master: did not reach STATUS,IDLE", flush=True)
        return False

    print("✅ Nayax master ready (d,STATUS,IDLE)", flush=True)
    return True

def main():
    ap = argparse.ArgumentParser("Split-mode translator: VMC cashless slave + Nayax cashless master")
    ap.add_argument("--port", required=True, help="e.g. /dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00")
    ap.add_argument("--max-credit", default="2.00")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--nayax-timeout", type=int, default=15)
    args = ap.parse_args()

    max_credit = Decimal(args.max_credit)

    with serial.Serial(args.port, BAUD, timeout=0.3, write_timeout=0.3) as s:
        print("opened", args.port, flush=True)

        # Reset slave + sniff off
        send(s, "X,0")
        send(s, "C,0")
        time.sleep(0.2)

        # Enable VMC-side cashless slave
        send(s, "C,1")
        print("sent: C,1", flush=True)

        # Wait for VMC enable
        ok = wait_for(s, VMC_ENABLED_RE, timeout_s=30, debug=args.debug)
        if not ok:
            print("🛑 VMC never enabled cashless slave (still INACTIVE). Wiring wrong.", flush=True)
            return
        print("✅ VMC enabled cashless slave", flush=True)

        # Init Nayax master side
        if not init_nayax_master(s, debug=args.debug):
            print("🛑 Fix Nayax wiring/power/state first. (Left port should be Nayax; right port should be VMC.)", flush=True)
            return

        # State
        vmc_has_credit = False

        # IMPORTANT: only arm credit when YOU decide (e.g. after Nayax indicates user present).
        # For MVP, arm immediately once Nayax is ready (later you can gate on a real "card present" signal).
        def arm_vmc_credit():
            nonlocal vmc_has_credit
            if vmc_has_credit:
                return
            cmd = f"C,START,{fmt_money(max_credit)}"
            send(s, cmd)
            vmc_has_credit = True
            print(f"⚡ armed VMC credit: {cmd}", flush=True)

        arm_vmc_credit()

        pending = None
        awaiting = False
        nayax_deadline = 0.0

        while True:
            line = s.readline()
            if not line:
                # Nayax timeout watchdog
                if awaiting and time.time() > nayax_deadline:
                    print("🛑 Nayax auth timeout -> C,STOP + D,END", flush=True)
                    send(s, "C,STOP")
                    send(s, "D,END")
                    awaiting = False
                    pending = None
                    vmc_has_credit = False
                continue

            txt = clean(line)
            if not txt:
                continue
            if args.debug and txt[0] in ("c","d","x"):
                print(txt, flush=True)

            # Track credit state
            if VMC_IDLE_CREDIT_RE.match(txt):
                vmc_has_credit = True
            if txt == "c,STATUS,IDLE":
                vmc_has_credit = False

            # VMC vend request
            m = VEND_REQ_RE.match(txt)
            if m:
                price = parse_money(m.group(1))
                product_raw = m.group(2)

                # Sanitize product id for Nayax (common expectation: 0..255)
                try:
                    prod_int = int(product_raw)
                except Exception:
                    prod_int = 0
                prod_for_nayax = prod_int & 0xFF  # 257 -> 1

                pending = (price, product_raw, prod_for_nayax)

                cmd = f"D,REQ,{fmt_money(price)},{prod_for_nayax}"
                print(f"🧠 VMC wants ${fmt_money(price)} prod_raw={product_raw} -> {cmd}", flush=True)
                send(s, cmd)
                awaiting = True
                nayax_deadline = time.time() + args.nayax_timeout
                continue

            # Nayax errors
            em = N_ERR_RE.match(txt)
            if em and awaiting and pending:
                code = em.group(1)
                price, product_raw, prod_for_nayax = pending
                print(f"🛑 Nayax ERR {code} on D,REQ (prod_raw={product_raw} sent={prod_for_nayax}) -> C,STOP", flush=True)
                send(s, "C,STOP")
                send(s, "D,END")
                awaiting = False
                pending = None
                vmc_has_credit = False
                continue

            # Nayax result
            rm = N_RESULT_RE.match(txt)
            if rm and awaiting and pending:
                res = int(rm.group(1))
                price, product_raw, prod_for_nayax = pending

                if res == 1:
                    print(f"😈 Nayax approved -> C,VEND,{fmt_money(price)}", flush=True)
                    send(s, f"C,VEND,{fmt_money(price)}")
                else:
                    print(f"🛑 Nayax denied(res={res}) -> C,STOP", flush=True)
                    send(s, "C,STOP")

                send(s, "D,END")
                awaiting = False
                pending = None
                vmc_has_credit = False
                arm_vmc_credit()
                continue

            # After a vend completes, re-arm credit if desired
            if txt == "c,VEND,SUCCESS":
                print("🧾 VEND SUCCESS", flush=True)
                vmc_has_credit = False
                arm_vmc_credit()

if __name__ == "__main__":
    main()

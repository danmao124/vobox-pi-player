#!/usr/bin/env python3
"""
translator_final.py

Split-mode Qibixx Pi Hat translator:
- VMC side: act as CASHLESS SLAVE (C,...) to the vending machine VMC
- Nayax side: act as CASHLESS MASTER (D,...) to the Nayax reader
- Flow:
    1) Wait for VMC ENABLED
    2) Init Nayax master until IDLE
    3) Arm VMC credit with C,START (retry-safe; fixes START -3)
    4) On c,STATUS,VEND,price,product -> D,REQ,price,product_sanitized
    5) If Nayax approves -> C,VEND,price else C,STOP
    6) D,END then re-arm

Run:
  python3 translator_final.py --port /dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00 --max-credit 2.00 --debug
"""

import serial, time, re, argparse
from decimal import Decimal

BAUD = 115200

# ---------- regex ----------
VMC_ENABLED_RE = re.compile(r"^c,STATUS,ENABLED\b")
VMC_IDLE_CREDIT_RE = re.compile(r"^c,STATUS,IDLE,([^,]+)\s*$")
VMC_IDLE_RE = re.compile(r"^c,STATUS,IDLE\s*$")
VMC_VEND_RE = re.compile(r"^c,STATUS,VEND,([^,]+),([^,]+)\s*$")
VMC_START_ERR_RE = re.compile(r'^c,ERR,"START\s+(-?\d+)"\s*$')

N_OFF_RE = re.compile(r"^d,STATUS,OFF\b")
N_INIT_RE = re.compile(r"^d,STATUS,INIT,(\d+)\b")
N_IDLE_RE = re.compile(r"^d,STATUS,IDLE\b")
N_RESULT_RE = re.compile(r"^d,STATUS,RESULT,([-\d]+)\b")
N_ERR_RE = re.compile(r'^d,ERR,"([-\d]+)"\s*$')

# ---------- helpers ----------
def clean(b: bytes) -> str:
    return b.decode(errors="replace").strip()

def send(s: serial.Serial, cmd: str):
    # Qibixx is happiest with CRLF
    s.write((cmd + "\r\n").encode("ascii"))
    s.flush()

def fmt_money(x: Decimal) -> str:
    return f"{x:.2f}"

def parse_money(s: str) -> Decimal:
    return Decimal(s.strip())

def debug_print(enabled: bool, txt: str):
    if enabled and txt and txt[0] in ("c", "d", "x"):
        print(txt, flush=True)

def wait_for(s: serial.Serial, regex, timeout_s: float, debug=False):
    end = time.time() + timeout_s
    while time.time() < end:
        line = s.readline()
        if not line:
            continue
        txt = clean(line)
        debug_print(debug, txt)
        if regex.match(txt):
            return txt
    return None

# ---------- init routines ----------
def init_vmc_slave(s: serial.Serial, debug=False) -> bool:
    send(s, "X,0")    # no sniff for now
    send(s, "C,0")    # disable slave
    time.sleep(0.2)

    send(s, "C,1")    # enable slave
    print("sent: C,1", flush=True)

    ok = wait_for(s, VMC_ENABLED_RE, timeout_s=30, debug=debug)
    if not ok:
        print("🛑 VMC never enabled cashless slave (check wiring: VMC -> RIGHT/Peripheral).", flush=True)
        return False

    print("✅ VMC enabled cashless slave", flush=True)
    return True

def init_nayax_master(s: serial.Serial, debug=False) -> bool:
    # hard reset
    send(s, "D,STOP")
    send(s, "D,0")
    ok = wait_for(s, N_OFF_RE, timeout_s=10, debug=debug)
    if not ok:
        print("🛑 Nayax master did not reach d,STATUS,OFF (power/wiring: Nayax -> LEFT/VMC).", flush=True)
        return False

    send(s, "D,2")
    ok = wait_for(s, N_INIT_RE, timeout_s=10, debug=debug)
    if not ok:
        print("🛑 Nayax master did not reach d,STATUS,INIT,*", flush=True)
        return False

    send(s, "D,READER,1")
    ok = wait_for(s, N_IDLE_RE, timeout_s=15, debug=debug)
    if not ok:
        print("🛑 Nayax master did not reach d,STATUS,IDLE", flush=True)
        return False

    print("✅ Nayax master ready (d,STATUS,IDLE)", flush=True)
    return True

# ---------- VMC credit arming (fixes START -3) ----------
def arm_credit_safe(s: serial.Serial, credit: Decimal, debug=False) -> bool:
    """
    Some VMCs will reject START briefly after ENABLED (START -3).
    We retry until we see c,STATUS,IDLE,<credit> or timeout.
    """
    credit_s = fmt_money(credit)
    for _ in range(12):  # ~6–10 seconds depending on timing
        send(s, f"C,START,{credit_s}")
        t_end = time.time() + 0.9
        while time.time() < t_end:
            line = s.readline()
            if not line:
                continue
            txt = clean(line)
            debug_print(debug, txt)

            if VMC_IDLE_CREDIT_RE.match(txt):
                print(f"⚡ armed VMC credit: {txt}", flush=True)
                return True

            m_err = VMC_START_ERR_RE.match(txt)
            if m_err:
                # START -3 is common if timing is early; just retry
                break
        time.sleep(0.4)

    print("🛑 failed to arm VMC credit (kept getting START errors / no IDLE,<credit>)", flush=True)
    return False

# ---------- main loop ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--max-credit", default="2.00")
    ap.add_argument("--nayax-timeout", type=int, default=15)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    max_credit = Decimal(args.max_credit)

    with serial.Serial(args.port, BAUD, timeout=0.3, write_timeout=0.3) as s:
        print("opened", args.port, flush=True)

        if not init_vmc_slave(s, debug=args.debug):
            return
        if not init_nayax_master(s, debug=args.debug):
            return

        vmc_has_credit = False
        awaiting_nayax = False
        pending = None  # (price, vmc_product_raw, nayax_product)
        nayax_deadline = 0.0

        # Arm credit once at start
        vmc_has_credit = arm_credit_safe(s, max_credit, debug=args.debug)

        while True:
            line = s.readline()
            if not line:
                # Nayax timeout watchdog
                if awaiting_nayax and time.time() > nayax_deadline:
                    print("🛑 Nayax timeout -> C,STOP + D,END", flush=True)
                    send(s, "C,STOP")
                    send(s, "D,END")
                    awaiting_nayax = False
                    pending = None
                    vmc_has_credit = False
                # If idle with no credit, re-arm
                if not awaiting_nayax and not vmc_has_credit:
                    vmc_has_credit = arm_credit_safe(s, max_credit, debug=args.debug)
                continue

            txt = clean(line)
            if not txt:
                continue
            debug_print(args.debug, txt)

            # Track VMC credit state
            if VMC_IDLE_CREDIT_RE.match(txt):
                vmc_has_credit = True
            elif VMC_IDLE_RE.match(txt):
                vmc_has_credit = False

            # VMC vend request
            m_v = VMC_VEND_RE.match(txt)
            if m_v:
                price = parse_money(m_v.group(1))
                vmc_prod_raw = m_v.group(2)

                # Sanitize product for Nayax: many expect 0..255
                try:
                    prod_int = int(vmc_prod_raw)
                except Exception:
                    prod_int = 0
                nayax_prod = prod_int & 0xFF  # 257 -> 1

                pending = (price, vmc_prod_raw, nayax_prod)

                cmd = f"D,REQ,{fmt_money(price)},{nayax_prod}"
                print(f"🧠 VMC wants ${fmt_money(price)} prod_raw={vmc_prod_raw} -> {cmd}", flush=True)
                send(s, cmd)

                awaiting_nayax = True
                nayax_deadline = time.time() + args.nayax_timeout
                continue

            # Nayax immediate ERR
            m_ne = N_ERR_RE.match(txt)
            if m_ne and awaiting_nayax and pending:
                code = m_ne.group(1)
                price, vmc_prod_raw, nayax_prod = pending
                print(f"🛑 Nayax ERR {code} on D,REQ (sent prod={nayax_prod}, raw={vmc_prod_raw}) -> C,STOP", flush=True)
                send(s, "C,STOP")
                send(s, "D,END")
                awaiting_nayax = False
                pending = None
                vmc_has_credit = False
                continue

            # Nayax RESULT
            m_nr = N_RESULT_RE.match(txt)
            if m_nr and awaiting_nayax and pending:
                res = int(m_nr.group(1))
                price, vmc_prod_raw, nayax_prod = pending

                if res == 1:
                    print(f"😈 Nayax approved -> C,VEND,{fmt_money(price)}", flush=True)
                    send(s, f"C,VEND,{fmt_money(price)}")
                else:
                    print(f"🛑 Nayax denied(res={res}) -> C,STOP", flush=True)
                    send(s, "C,STOP")

                send(s, "D,END")
                awaiting_nayax = False
                pending = None
                vmc_has_credit = False
                continue

            # Vend complete
            if txt == "c,VEND,SUCCESS":
                print("🧾 VEND SUCCESS", flush=True)
                vmc_has_credit = False
                continue

if __name__ == "__main__":
    main()

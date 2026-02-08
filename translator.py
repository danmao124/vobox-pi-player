#!/usr/bin/env python3
"""
translator_final.py (+ comp credit mode)

New feature: COMP MODE
- --comp-credit 5.00
    Arms the VMC with that much credit *without* requiring Nayax CREDIT
    and auto-approves vends up to that amount by sending C,VEND,<price>.
    (Skips Nayax for that vend.)

- --comp-oneshot
    After the first successful vend, comp mode turns off automatically.

Why:
- Lets you "give yourself $5" (free vend credit) on demand.

Run examples:
  # Normal Nayax-gated operation:
  python3 translator_final.py --port /dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00 --max-credit 2.00 --debug

  # Give yourself $5 credit (free vends up to $5) while script runs:
  python3 translator_final.py --port /dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00 --comp-credit 5.00 --debug

  # One-shot comp (one free vend) then revert to normal Nayax gating:
  python3 translator_final.py --port /dev/serial/by-id/usb-Qibixx_MDB-HAT_0-if00 --comp-credit 5.00 --comp-oneshot --max-credit 2.00 --debug
"""

import serial, time, re, argparse
from decimal import Decimal

BAUD = 115200

# ---------- regex ----------
VMC_ENABLED_RE      = re.compile(r"^c,STATUS,ENABLED\b")
VMC_IDLE_CREDIT_RE  = re.compile(r"^c,STATUS,IDLE,([^,]+)\s*$")
VMC_IDLE_RE         = re.compile(r"^c,STATUS,IDLE\s*$")
VMC_VEND_RE         = re.compile(r"^c,STATUS,VEND,([^,]+),([^,]+)\s*$")
VMC_START_ERR_RE    = re.compile(r'^c,ERR,"START\s+(-?\d+)"\s*$')

N_OFF_RE            = re.compile(r"^d,STATUS,OFF\b")
N_INIT_RE           = re.compile(r"^d,STATUS,INIT,(\d+)\b")
N_IDLE_RE           = re.compile(r"^d,STATUS,IDLE\b")
N_CREDIT_RE         = re.compile(r"^d,STATUS,CREDIT,([^,]+),(.+)$")
N_RESULT_RE         = re.compile(r"^d,STATUS,RESULT,([-\d]+),([^,]+)")
N_ERR_RE            = re.compile(r'^d,ERR,"([-\d]+)"\s*$')

# ---------- helpers ----------
def clean(b: bytes) -> str:
    return b.decode(errors="replace").strip()

def send(s: serial.Serial, cmd: str):
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
    send(s, "X,0")
    send(s, "C,0")
    time.sleep(0.2)

    send(s, "C,1")
    print("sent: C,1", flush=True)

    ok = wait_for(s, VMC_ENABLED_RE, timeout_s=30, debug=debug)
    if not ok:
        print("🛑 VMC never enabled cashless slave. Wiring must be: VMC -> RIGHT/Peripheral.", flush=True)
        return False

    print("✅ VMC enabled cashless slave", flush=True)
    return True

def init_nayax_master(s: serial.Serial, debug=False) -> bool:
    send(s, "D,STOP")
    send(s, "D,0")
    ok = wait_for(s, N_OFF_RE, timeout_s=10, debug=debug)
    if not ok:
        print("🛑 Nayax master did not reach d,STATUS,OFF. Wiring must be: Nayax -> LEFT/VMC.", flush=True)
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

# ---------- VMC credit arming ----------
def arm_credit_safe(s: serial.Serial, credit: Decimal, debug=False) -> bool:
    credit_s = fmt_money(credit)
    for _ in range(12):
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

            if VMC_START_ERR_RE.match(txt):
                break
        time.sleep(0.4)
    print("🛑 failed to arm VMC credit (no IDLE,<credit>)", flush=True)
    return False

# ---------- main loop ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--max-credit", default="2.00", help="Normal (Nayax-gated) credit to arm on VMC.")
    ap.add_argument("--nayax-timeout", type=int, default=15)
    ap.add_argument("--credit-wait", type=float, default=6.0, help="Seconds to wait for Nayax CREDIT after VMC VEND")
    ap.add_argument("--debug", action="store_true")

    # NEW: comp mode
    ap.add_argument("--comp-credit", default=None,
                    help="If set (e.g. 5.00), arm VMC with this credit and approve vends WITHOUT Nayax.")
    ap.add_argument("--comp-oneshot", action="store_true",
                    help="If set with --comp-credit, disable comp mode after first successful vend.")

    args = ap.parse_args()

    max_credit = Decimal(args.max_credit)
    comp_credit = Decimal(args.comp_credit) if args.comp_credit is not None else None
    comp_active = comp_credit is not None

    with serial.Serial(args.port, BAUD, timeout=0.3, write_timeout=0.3) as s:
        print("opened", args.port, flush=True)

        if not init_vmc_slave(s, debug=args.debug):
            return
        if not init_nayax_master(s, debug=args.debug):
            return

        # State
        vmc_has_credit = False
        vmc_busy = False
        awaiting_nayax = False
        pending = None
        nayax_deadline = 0.0

        nayax_credit_ready = False

        def current_arm_amount() -> Decimal:
            return comp_credit if comp_active else max_credit

        def maybe_arm():
            nonlocal vmc_has_credit
            if vmc_busy or vmc_has_credit:
                return
            if not comp_active and not nayax_credit_ready:
                return
            vmc_has_credit = arm_credit_safe(s, current_arm_amount(), debug=args.debug)

        if comp_active:
            print(f"😈 COMP MODE ON: free credit=${fmt_money(comp_credit)} "
                  f"{'(one-shot)' if args.comp_oneshot else '(persistent)'}", flush=True)

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
                    vmc_busy = False
                maybe_arm()
                continue

            txt = clean(line)
            if not txt:
                continue
            debug_print(args.debug, txt)

            # Nayax state
            if N_IDLE_RE.match(txt):
                nayax_credit_ready = False
            if N_CREDIT_RE.match(txt):
                nayax_credit_ready = True

            # VMC credit state
            if VMC_IDLE_CREDIT_RE.match(txt):
                vmc_has_credit = True
            elif VMC_IDLE_RE.match(txt):
                vmc_has_credit = False
            elif VMC_ENABLED_RE.match(txt):
                if not vmc_busy:
                    vmc_has_credit = False

            # Vend complete
            if txt == "c,VEND,SUCCESS":
                print("🧾 VEND SUCCESS", flush=True)
                vmc_busy = False
                vmc_has_credit = False
                if comp_active and args.comp_oneshot:
                    comp_active = False
                    print("😈 COMP MODE OFF (one-shot consumed)", flush=True)
                continue

            # VMC vend request
            m_v = VMC_VEND_RE.match(txt)
            if m_v:
                vmc_busy = True
                price = parse_money(m_v.group(1))
                vmc_prod_raw = m_v.group(2)

                # Hard guard: never vend above current armed credit
                if price > current_arm_amount():
                    print(f"🛑 price ${fmt_money(price)} > armed ${fmt_money(current_arm_amount())} -> C,STOP", flush=True)
                    send(s, "C,STOP")
                    vmc_busy = False
                    vmc_has_credit = False
                    continue

                # If comp mode, skip Nayax and approve immediately
                if comp_active:
                    print(f"😈 COMP APPROVE -> C,VEND,{fmt_money(price)} (prod_raw={vmc_prod_raw})", flush=True)
                    send(s, f"C,VEND,{fmt_money(price)}")
                    # stay busy until c,VEND,SUCCESS arrives
                    continue

                # Normal mode: need Nayax CREDIT; wait briefly if needed
                if not nayax_credit_ready:
                    wait_until = time.time() + args.credit_wait
                    while time.time() < wait_until and not nayax_credit_ready:
                        l2 = s.readline()
                        if not l2:
                            continue
                        t2 = clean(l2)
                        debug_print(args.debug, t2)
                        if N_CREDIT_RE.match(t2):
                            nayax_credit_ready = True
                            break
                        if N_IDLE_RE.match(t2):
                            nayax_credit_ready = False

                if not nayax_credit_ready:
                    print("🛑 No Nayax CREDIT session -> C,STOP", flush=True)
                    send(s, "C,STOP")
                    vmc_busy = False
                    vmc_has_credit = False
                    continue

                # Sanitize product for Nayax
                try:
                    prod_int = int(vmc_prod_raw)
                except Exception:
                    prod_int = 0
                nayax_prod = prod_int & 0xFF

                pending = (price, vmc_prod_raw, nayax_prod)

                cmd = f"D,REQ,{fmt_money(price)},{nayax_prod}"
                print(f"🧠 VMC wants ${fmt_money(price)} prod_raw={vmc_prod_raw} -> {cmd}", flush=True)
                send(s, cmd)

                awaiting_nayax = True
                nayax_deadline = time.time() + args.nayax_timeout
                continue

            # Nayax ERR
            m_ne = N_ERR_RE.match(txt)
            if m_ne and awaiting_nayax and pending:
                code = m_ne.group(1)
                price, vmc_prod_raw, nayax_prod = pending
                print(f"🛑 Nayax ERR {code} on D,REQ (prod={nayax_prod}, raw={vmc_prod_raw}) -> C,STOP", flush=True)
                send(s, "C,STOP")
                send(s, "D,END")
                awaiting_nayax = False
                pending = None
                vmc_has_credit = False
                vmc_busy = False
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
                # keep vmc_busy True until c,VEND,SUCCESS
                continue

            maybe_arm()

if __name__ == "__main__":
    main()

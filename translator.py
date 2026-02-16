#!/usr/bin/env python3
"""
  python3 translator.py --comp-credit .25 --comp-oneshot --debug
"""

import serial, time, re, argparse, uuid, signal, atexit
from decimal import Decimal
from pathlib import Path
from api_client import api_post, get_device_credentials

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

def send(s: serial.Serial, cmd: str, retries: int = 3, debug: bool = False) -> bool:
    payload = (cmd + "\r\n").encode("ascii", errors="strict")
    for attempt in range(1, retries + 1):
        try:
            s.write(payload)
            s.flush()
            return True
        except serial.SerialTimeoutException:
            if debug:
                print(f"⚠️ write timeout on '{cmd}' attempt {attempt}/{retries}", flush=True)
            try:
                s.reset_output_buffer()
            except Exception:
                pass
            time.sleep(0.15)
        except serial.SerialException as e:
            if debug:
                print(f"⚠️ serial exception on '{cmd}': {e}", flush=True)
            break
    return False

def fmt_money(x: Decimal) -> str:
    return f"{x:.2f}"

def parse_money(s: str) -> Decimal:
    return Decimal(s.strip())

def debug_print(enabled: bool, txt: str):
    if enabled and txt and txt[0] in ("c", "d", "x"):
        print(txt, flush=True)

def load_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

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

# ---------- event logging ----------
def log_vend_event(api_base: str, event_type: str, price: Decimal,
                   nayax_prod: int = None, reason: str = None, comp_mode: bool = False):
    try:
        url = f"{api_base}/device/logdeviceevent"
        idempotency_key = str(uuid.uuid4())

        data = {"price": str(price), "comp_mode": comp_mode}
        if nayax_prod is not None:
            data["nayax_prod"] = int(nayax_prod)
        if reason:
            data["reason"] = reason

        payload = {"type": event_type, "idempotency_key": idempotency_key, "data": data}
        device_id, secret = get_device_credentials()
        r = api_post(url, payload, device_id=device_id, secret=secret, timeout=5.0)
        print(f"📡 Logged {event_type}: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"⚠️ Failed to log vend event: {e}", flush=True)

# ---------- init routines ----------
def init_vmc_slave(s: serial.Serial, debug=False) -> bool:
    if not send(s, "X,0", debug=debug): return False
    if not send(s, "C,0", debug=debug): return False
    time.sleep(0.2)

    if not send(s, "C,1", debug=debug): return False
    print("sent: C,1", flush=True)

    ok = wait_for(s, VMC_ENABLED_RE, timeout_s=30, debug=debug)
    if not ok:
        print("🛑 VMC never enabled cashless slave. Wiring must be: VMC -> RIGHT/Peripheral.", flush=True)
        return False

    print("✅ VMC enabled cashless slave", flush=True)
    return True

def init_nayax_master(s: serial.Serial, debug=False) -> bool:
    send(s, "D,STOP", debug=debug)
    send(s, "D,0", debug=debug)
    ok = wait_for(s, N_OFF_RE, timeout_s=10, debug=debug)
    if not ok:
        print("🛑 Nayax master did not reach d,STATUS,OFF. Wiring must be: Nayax -> LEFT/VMC.", flush=True)
        return False

    send(s, "D,2", debug=debug)
    ok = wait_for(s, N_INIT_RE, timeout_s=10, debug=debug)
    if not ok:
        print("🛑 Nayax master did not reach d,STATUS,INIT,*", flush=True)
        return False

    send(s, "D,READER,1", debug=debug)
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
        send(s, f"C,START,{credit_s}", debug=debug)
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

# ---------- main ----------
def main():
    here = Path(__file__).resolve().parent
    cfg = load_env_file(here / "config.env")

    port = cfg.get("PORT", "")
    if not port:
        raise ValueError("PORT missing in config.env")

    api_base = cfg.get("API_BASE", "")
    if not api_base:
        raise ValueError("API_BASE missing in config.env")

    max_credit_str = cfg.get("MAX_CREDIT", "10.00")
    try:
        max_credit = Decimal(max_credit_str)
    except Exception:
        raise ValueError(f"Invalid MAX_CREDIT value in config.env: {max_credit_str}")

    ap = argparse.ArgumentParser()
    ap.add_argument("--nayax-timeout", type=int, default=90)
    ap.add_argument("--credit-wait", type=float, default=6.0,
                    help="Seconds to wait for Nayax CREDIT after VMC VEND")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--comp-credit", default=None,
                    help="If set (e.g. 0.25), arm VMC with this credit and approve vends WITHOUT Nayax.")
    ap.add_argument("--comp-oneshot", action="store_true",
                    help="If set with --comp-credit, disable comp mode after first successful vend.")
    ap.add_argument("--vmc-vend-timeout", type=float, default=25.0,
                    help="Seconds to wait for c,VEND,SUCCESS before resetting cashless.")
                    
    args = ap.parse_args()

    comp_credit = Decimal(args.comp_credit) if args.comp_credit is not None else None
    comp_active = comp_credit is not None

    with serial.Serial(
        port, BAUD,
        timeout=0.3,
        write_timeout=1.5,
        rtscts=False, dsrdtr=False, xonxoff=False
    ) as s:
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
        except Exception:
            pass

        print("opened", port, flush=True)

        stop_flag = {"stop": False}

        def hard_cleanup():
            try:
                send(s, "C,STOP", debug=args.debug)
                send(s, "D,END", debug=args.debug)
                send(s, "D,STOP", debug=args.debug)
                time.sleep(0.2)
            except Exception:
                pass

        def handle_sig(sig, frame):
            stop_flag["stop"] = True

        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)
        atexit.register(hard_cleanup)

        hard_cleanup()

        if not init_vmc_slave(s, debug=args.debug):
            return
        if not init_nayax_master(s, debug=args.debug):
            return

        vmc_has_credit = False
        vmc_credit_ts = 0.0
        vmc_busy = False
        vmc_vend_deadline = 0.0

        nayax_credit_ready = False
        nayax_deadline = 0.0  # 0 means "not waiting"

        # vend context for logging
        current_vend_price = None
        current_vend_nayax_prod = None  # selection byte (0-255)
        current_vend_comp_mode = False

        def reset_vend_state():
            nonlocal vmc_has_credit, vmc_credit_ts, vmc_busy, vmc_vend_deadline
            nonlocal current_vend_price, current_vend_nayax_prod, current_vend_comp_mode
            nonlocal nayax_deadline
            nayax_deadline = 0.0
            vmc_busy = False
            vmc_has_credit = False
            vmc_credit_ts = 0.0
            vmc_vend_deadline = 0.0
            current_vend_price = None
            current_vend_nayax_prod = None
            current_vend_comp_mode = False

        def current_arm_amount() -> Decimal:
            return comp_credit if comp_active else max_credit

        def maybe_arm():
            nonlocal vmc_has_credit, vmc_credit_ts
            if vmc_busy or vmc_has_credit:
                return
            if not comp_active and not nayax_credit_ready:
                return
            ok = arm_credit_safe(s, current_arm_amount(), debug=args.debug)
            vmc_has_credit = ok
            if ok:
                vmc_credit_ts = time.time()

        if comp_active:
            print(f"😈 COMP MODE ON: free credit=${fmt_money(comp_credit)} "
                  f"{'(one-shot)' if args.comp_oneshot else '(persistent)'}", flush=True)

        while not stop_flag["stop"]:
            line = s.readline()
            now = time.time()

            # VMC watchdog timeout
            if vmc_busy and vmc_vend_deadline and now > vmc_vend_deadline:
                print("🛑 VMC vend watchdog timeout -> C,STOP (reset busy)", flush=True)
                send(s, "C,STOP", debug=args.debug)
                send(s, "D,END", debug=args.debug)
                reset_vend_state()

            # Nayax timeout (only meaningful if we're in a normal vend)
            if (nayax_deadline and now > nayax_deadline and vmc_busy
                and (current_vend_price is not None) and (not current_vend_comp_mode)):
                price = current_vend_price
                sel = current_vend_nayax_prod
                print("🛑 Nayax timeout -> C,STOP + D,END", flush=True)
                log_vend_event(
                    api_base,
                    "nayax_payment.denied",
                    price,
                    nayax_prod=sel,
                    reason="nayax_timeout",
                    comp_mode=False,
                )
                send(s, "C,STOP", debug=args.debug)
                send(s, "D,END", debug=args.debug)
                reset_vend_state()

            if not line:
                maybe_arm()
                continue

            txt = clean(line)
            if not txt:
                continue
            debug_print(args.debug, txt)

            # Nayax session state
            if N_IDLE_RE.match(txt):
                nayax_credit_ready = False
            if N_CREDIT_RE.match(txt):
                nayax_credit_ready = True

            # VMC credit state
            if VMC_IDLE_CREDIT_RE.match(txt):
                vmc_has_credit = True
                vmc_credit_ts = time.time()
            elif VMC_IDLE_RE.match(txt):
                vmc_has_credit = False
            elif VMC_ENABLED_RE.match(txt):
                if not vmc_busy:
                    vmc_has_credit = False

            # Vend complete (this is where we log "approved" final truth)
            if txt == "c,VEND,SUCCESS":
                print("🧾 VEND SUCCESS", flush=True)
                if current_vend_price is not None:
                    log_vend_event(
                        api_base,
                        "nayax_payment.approved",
                        current_vend_price,
                        nayax_prod=current_vend_nayax_prod,
                        comp_mode=current_vend_comp_mode,
                    )

                reset_vend_state()

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

                # Stable selection byte (0-255)
                try:
                    prod_int = int(vmc_prod_raw)
                except Exception:
                    prod_int = 0
                selection_byte = prod_int & 0xFF

                current_vend_price = price
                current_vend_nayax_prod = selection_byte
                current_vend_comp_mode = bool(comp_active)

                if price > current_arm_amount():
                    print(f"🛑 price ${fmt_money(price)} > armed ${fmt_money(current_arm_amount())} -> C,STOP", flush=True)
                    send(s, "C,STOP", debug=args.debug)
                    reset_vend_state()
                    continue

                if comp_active:
                    print(f"😈 COMP APPROVE -> C,VEND,{fmt_money(price)} (sel={selection_byte})", flush=True)
                    send(s, f"C,VEND,{fmt_money(price)}", debug=args.debug)
                    vmc_vend_deadline = time.time() + args.vmc_vend_timeout
                    continue

                # Normal mode: wait briefly for CREDIT
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
                    log_vend_event(
                        api_base,
                        "nayax_payment.denied",
                        price,
                        nayax_prod=selection_byte,
                        reason="no_nayax_credit_session",
                        comp_mode=False,
                    )
                    send(s, "C,STOP", debug=args.debug)
                    reset_vend_state()
                    continue

                cmd = f"D,REQ,{fmt_money(price)},{selection_byte}"
                print(f"🧠 VMC wants ${fmt_money(price)} -> {cmd}", flush=True)
                send(s, cmd, debug=args.debug)

                nayax_deadline = time.time() + args.nayax_timeout
                vmc_vend_deadline = time.time() + args.vmc_vend_timeout
                continue

            # Nayax ERR (no in-flight state; only act if we're in an active normal vend)
            m_ne = N_ERR_RE.match(txt)
            print(f"RAW txt repr={txt!r}", flush=True)
            print(f"m_ne: {m_ne}", flush=True)
            print(f"vmc_busy: {vmc_busy}", flush=True)
            print(f"current_vend_price: {current_vend_price}", flush=True)
            print(f"current_vend_comp_mode: {current_vend_comp_mode}", flush=True)
            if (m_ne and vmc_busy and (current_vend_price is not None) and (not current_vend_comp_mode)):
                code = m_ne.group(1)
                price = current_vend_price
                sel = current_vend_nayax_prod

                log_vend_event(
                    api_base,
                    "nayax_payment.denied",
                    price,
                    nayax_prod=sel,
                    reason=f"nayax_err_{code}",
                    comp_mode=False,
                )
                print(f"🛑 Nayax ERR {code} on D,REQ (sel={sel}) -> C,STOP", flush=True)

                send(s, "C,STOP", debug=args.debug)
                send(s, "D,END", debug=args.debug)
                reset_vend_state()
                continue

            # Nayax RESULT (no in-flight state; only act if we're in an active normal vend)
            m_nr = N_RESULT_RE.match(txt)
            if (m_nr and vmc_busy and (current_vend_price is not None) and (not current_vend_comp_mode)):
                res = int(m_nr.group(1))
                price = current_vend_price
                sel = current_vend_nayax_prod

                nayax_deadline = 0.0  # Nayax responded

                if res == 1:
                    print(f"😈 Nayax approved -> C,VEND,{fmt_money(price)}", flush=True)
                    send(s, f"C,VEND,{fmt_money(price)}", debug=args.debug)
                    vmc_vend_deadline = time.time() + args.vmc_vend_timeout
                    # do NOT reset here; wait for c,VEND,SUCCESS to log approved + clean up
                else:
                    print(f"🛑 Nayax denied(res={res}) -> C,STOP", flush=True)
                    log_vend_event(
                        api_base,
                        "nayax_payment.denied",
                        price,
                        nayax_prod=sel,
                        reason=f"nayax_denied_res_{res}",
                        comp_mode=False,
                    )
                    send(s, "C,STOP", debug=args.debug)
                    reset_vend_state()

                send(s, "D,END", debug=args.debug)
                continue

            maybe_arm()

        print("👋 stopping (signal received)", flush=True)
        hard_cleanup()

if __name__ == "__main__":
    main()

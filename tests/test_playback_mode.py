import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import playback_mode as mode
import playback_report as report


def config_block():
    source = (ROOT / "tvads.sh").read_text()
    # Exercise startup config without touching auth, devices, or renderers.
    return source.split("# ---------- load config ----------", 1)[1].split("# Device auth", 1)[0]


def functions(*names):
    source = (ROOT / "tvads.sh").read_text()
    return "\n".join(
        name + "() {" + source.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        for name in names
    )


def response(**fields):
    return {"response": {"success": True, "data": [], **fields}}


class PlaybackModeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mode.json"

    def update(self, at, **fields):
        mode.update_state(self.path, " STATION ", at, response(**fields))

    def test_explicit_false_switches_back_and_stale_prefetch_cannot_undo_it(self):
        self.update(10, hasYoutube=True)
        self.assertEqual(mode.browser_content(self.path, "station"), "ads")
        self.update(30, hasYoutube=False)
        self.update(20, hasYoutube=True)
        self.assertEqual(mode.browser_content(self.path, "STATION"), "")

    def test_invalid_or_missing_flags_do_not_clear_confirmed_mode(self):
        self.update(1, hasYoutube=True)
        for value in [None, "false", "true", 0, 1, [], {}]:
            self.update(2, hasYoutube=value)
        self.update(3)
        mode.update_state(self.path, "STATION", 4, response(success=False, hasYoutube=False))
        self.assertEqual(mode.browser_content(self.path, "STATION"), "ads")

    def test_requires_browser_selects_layout_and_explicit_false_clears_it(self):
        self.update(10, requiresBrowser=True, hasYoutube=False, webContent="bay101poker")
        self.assertEqual(mode.browser_content(self.path, "station"), "ads")
        self.update(30, requiresBrowser=False, hasYoutube=True)
        self.assertEqual(mode.browser_content(self.path, "station"), "bay101poker")
        self.update(20, requiresBrowser=True)
        self.assertEqual(mode.browser_content(self.path, "station"), "bay101poker")
        self.update(40, webContent=None)
        self.assertEqual(mode.browser_content(self.path, "station"), "")

    def test_confirmed_requires_browser_survives_missing_invalid_and_failed_responses(self):
        self.update(1, requiresBrowser=True, hasYoutube=False)
        for value in (None, "false", "true", 0, 1, [], {}):
            with self.subTest(value=value):
                self.update(2, requiresBrowser=value, hasYoutube=False)
                self.assertEqual(mode.browser_content(self.path, "STATION"), "ads")
        self.update(3, hasYoutube=False)
        mode.update_state(self.path, "STATION", 4, response(success=False, requiresBrowser=False))
        self.assertEqual(mode.browser_content(self.path, "STATION"), "ads")
        self.assertEqual(mode.browser_content(self.path, "OTHER"), "")

    def test_legacy_state_remains_usable_until_new_signal_is_confirmed(self):
        self.update(10, hasYoutube=True)
        self.update(20, requiresBrowser="false")
        self.assertEqual(mode.browser_content(self.path, "STATION"), "ads")
        self.update(30, requiresBrowser=False)
        self.assertEqual(mode.browser_content(self.path, "STATION"), "")
        self.update(40, hasYoutube=True)
        self.assertEqual(mode.browser_content(self.path, "STATION"), "")

    def test_web_schedule_is_preserved_by_heartbeat_but_cleared_explicitly(self):
        self.update(1, webContent="bay101poker", hasYoutube=False)
        self.update(3, hasYoutube=True)
        self.assertEqual(mode.browser_content(self.path, "STATION"), "ads")
        self.update(4, hasYoutube=False)
        self.assertEqual(mode.browser_content(self.path, "STATION"), "bay101poker")
        # A delayed billboard may still update an independent web schedule field.
        self.update(2, webContent=None, hasYoutube=True)
        self.assertEqual(mode.browser_content(self.path, "STATION"), "")

    def test_state_from_another_station_is_not_reused(self):
        self.update(30, hasYoutube=True)
        self.assertEqual(mode.browser_content(self.path, "OTHER"), "")
        mode.update_state(self.path, "OTHER", 40, response(hasYoutube=False))
        self.assertEqual(mode.read_state(self.path, "STATION"), {})

    def test_unreadable_state_defaults_to_native_playback(self):
        for contents in ["invalid", "[]", "null"]:
            self.path.write_text(contents)
            self.assertEqual(mode.browser_content(self.path, "STATION"), "")

    def test_station_config_is_normalized_deduplicated_and_bounded(self):
        self.assertEqual(mode.station_ids(" bay101, MCB, BAY101, ,"), ["BAY101", "MCB"])
        self.assertEqual(len(mode.station_ids(",".join(str(i) for i in range(16)))), 16)
        for value in ("", " , ", ",".join(str(i) for i in range(17))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mode.station_ids(value)
        result = subprocess.run([sys.executable, str(ROOT / "playback_mode.py"), "stations", "bay101, MCB,bay101"],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "BAY101,MCB\n")

    def test_multiple_panels_require_browser_without_youtube_or_saved_state(self):
        self.assertEqual(mode.browser_content(self.path, "BAY101,MCB"), "ads")
        mode.update_state(self.path, "bay101, MCB", 1, response(hasYoutube=False, webContent="other"))
        self.assertEqual(mode.browser_content(self.path, "BAY101,MCB"), "ads")
        self.assertEqual(mode.read_state(self.path, "BAY101"), {})
        self.assertEqual(mode.read_state(self.path, "BAY101,OTHER"), {})

    def test_browser_signal_interrupts_image_without_waiting_for_duration(self):
        self.update(1, hasYoutube=False)
        with patch.object(report.time, "monotonic_ns", return_value=100_000_000_000), \
                patch.object(report.time, "sleep", side_effect=lambda _: self.update(2, hasYoutube=True)) as sleeper:
            rc = report.wait_image("100000000000", 15, str(self.path) + ".wizard",
                                   str(self.path) + ".at", str(self.path) + ".list",
                                   str(self.path), "STATION")
        self.assertEqual(rc, 3)
        sleeper.assert_called_once()

    def test_layout_signal_interrupts_native_image_without_youtube(self):
        self.update(1, requiresBrowser=False, hasYoutube=False)
        with patch.object(report.time, "monotonic_ns", return_value=100_000_000_000), \
                patch.object(report.time, "sleep", side_effect=lambda _: self.update(2, requiresBrowser=True)) as sleeper:
            rc = report.wait_image("100000000000", 15, str(self.path) + ".wizard",
                                   str(self.path) + ".at", str(self.path) + ".list",
                                   str(self.path), "STATION")
        self.assertEqual(rc, 3)
        sleeper.assert_called_once()


class PlaybackModeShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        names = ["MAIN_LIST", "PENDING_LIST", "INDEX_FILE", "NEXT_FILE", "NEXT_BLAST_FILE",
                 "PLAYBACK_MODE_FILE", "ACTIVE_MODE_FILE", "PLAYBACK_HISTORY", "SYNC_LIST",
                 "SYNC_AT_FILE", "SYNC_NEXT_FILE", "SYNC_BLAST_FILE"]
        self.env = "\n".join(f"{key}={shlex.quote(str(self.root / key))}" for key in names)
        self.env += f"\nSTATE_DIR={shlex.quote(str(self.root))}\nSCRIPT_DIR={shlex.quote(str(ROOT))}\n"
        self.env += r'''
ID=STATION WEB_STATION="" DEVICE_ID=device-test DEVICE_SECRET=secret-test
PLAYBACK_SESSION=11111111-1111-4111-8111-111111111111 PLAYER_VERSION=test
API_BASE=https://example.test/api VIEW_PATH=view/billboard ASK_FOR_EVENT_PATH=device/askforevent
CURL_API_OPTS=(--fail) EVENT_SLOT=0 LAST_SYNC_COMMAND="" blast_idx=0
KIOSK_BOOTSTRAPPED="" ACTIVE_KIOSK_CONTENT="" MPV_HAS_DISPLAY="" CHROMIUM_PID=""
JQ_URLS='.response.data[]?.url // empty'
JQ_INDEX='.response.index // .response.message // empty'
JQ_BLAST='.response.blastIndex // empty'
log() { echo "$*"; }
build_curl_auth_headers() { curl_headers=(-H test-auth); }
note_fetch_reach_ok() { :; }
internet_ping_ok() { return 0; }
wizard_active() { return 1; }
with_cache_lock() { "$@"; }
restart_player() { echo "RESTART: $1"; exit 70; }
'''

    def run_shell(self, script, *, names=(), expected=0):
        result = subprocess.run(["bash", "-euo", "pipefail", "-c",
                                 self.env + functions(*names) + "\n" + script],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def set_mode(self, enabled):
        (self.root / "PLAYBACK_MODE_FILE").unlink(missing_ok=True)
        mode.update_state(self.root / "PLAYBACK_MODE_FILE", "STATION", 1, response(hasYoutube=enabled))

    def test_requires_browser_from_either_endpoint_launches_layout_without_config_flag(self):
        station = "APNIMANDI-41081-94538-MAIN"
        (self.root / "layout-response.json").write_text(json.dumps(response(
            requiresBrowser=True, hasYoutube=False, index=0, blastIndex=0)))
        for command in ('fetch_batch_to 0 0 "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"', 'ask_for_event'):
            with self.subTest(command=command):
                (self.root / "PLAYBACK_MODE_FILE").unlink(missing_ok=True)
                self.run_shell(f"ID={station}\n" + r'''
build_event_body() { echo '{}'; }
curl() { cat "$STATE_DIR/layout-response.json"; }
''' + command, names=("ask_for_event", "fetch_batch_to", "update_playback_mode"))
                parts = self.launch_kiosk(station, unmuted_id=" abc ", supervised=True)
                self.assert_ads_url(parts, station, "90", unmute="ABC")
                self.assertEqual((self.root / "ACTIVE_MODE_FILE").read_text(), "chromium\n")

    def test_shell_and_python_agree_on_browser_signal_precedence(self):
        updates = [
            {"hasYoutube": True},
            {"requiresBrowser": False},
            {"requiresBrowser": True, "hasYoutube": False},
            {"requiresBrowser": None, "hasYoutube": False},
            {"hasYoutube": False},
            {"requiresBrowser": False, "webContent": "bay101poker"},
            {"webContent": None},
        ]
        for at, (fields, expected) in enumerate(zip(updates, ("ads", "", "ads", "ads", "ads", "bay101poker", ""))):
            with self.subTest(fields=fields):
                mode.update_state(self.root / "PLAYBACK_MODE_FILE", "STATION", at, response(**fields))
                self.assertEqual(mode.browser_content(self.root / "PLAYBACK_MODE_FILE", "STATION"), expected)
                self.assertEqual(self.run_shell('requested_kiosk_content', names=("requested_kiosk_content",)).strip(), expected)

    def test_heartbeat_sets_true_and_empty_billboard_explicitly_clears_it(self):
        (self.root / "heartbeat.json").write_text(json.dumps(response(hasYoutube=True)))
        (self.root / "billboard.json").write_text(json.dumps(response(hasYoutube=False, index="0", blastIndex="0")))
        output = self.run_shell(r'''
build_event_body() { echo '{}'; }
curl() { cat "$STATE_DIR/$RESPONSE.json"; }
RESPONSE=heartbeat
ask_for_event
echo "content=$(requested_kiosk_content)"
echo old-youtube-url > "$PENDING_LIST"
RESPONSE=billboard
fetch_batch_to 0 0 "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"
echo "content=$(requested_kiosk_content)"
[[ ! -s "$PENDING_LIST" ]]
''', names=("ask_for_event", "fetch_batch_to", "update_playback_mode", "requested_kiosk_content"))
        self.assertIn("content=ads", output)
        self.assertTrue(output.rstrip().endswith("content="))
        self.assertFalse(mode.read_state(self.root / "PLAYBACK_MODE_FILE", "STATION")["hasYoutube"])

    def test_fetch_failure_or_invalid_payload_preserves_current_mode_and_list(self):
        self.set_mode(True)
        (self.root / "PENDING_LIST").write_text("current\n")
        self.run_shell(r'''
curl() { echo '{"response":{"success":false,"hasYoutube":false,"data":[]}}'; }
if fetch_batch_to 0 0 "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; then exit 99; fi
curl() { return 22; }
if fetch_batch_to 0 0 "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; then exit 99; fi
''', names=("fetch_batch_to", "update_playback_mode"))
        self.assertEqual((self.root / "PENDING_LIST").read_text(), "current\n")
        self.assertEqual(mode.browser_content(self.root / "PLAYBACK_MODE_FILE", "STATION"), "ads")

    def test_chromium_heartbeat_omits_mpv_history_and_identifies_session(self):
        (self.root / "ACTIVE_MODE_FILE").write_text("chromium\n")
        output = self.run_shell('build_event_body', names=("build_event_body",))
        body = json.loads(output)
        self.assertEqual(body["playbackMode"], "chromium")
        self.assertEqual(body["playbackSession"], "11111111-1111-4111-8111-111111111111")
        self.assertNotIn("playback", body)

    def test_native_sync_accepts_only_primary_station_or_legacy_commands(self):
        commands = [{"type": "sync", "data": {"index": i, "timestamp": 100 + i, **tag}}
                    for i, tag in enumerate(({}, {"adStationId": " station "}, {"adStationId": "OTHER"},
                                             {"adStationId": ["STATION"]}), 1)]
        (self.root / "commands.json").write_text(json.dumps(response(data=commands)))
        output = self.run_shell(r'''
build_event_body() { echo '{}'; }
update_playback_mode() { :; }
curl() { cat "$STATE_DIR/commands.json"; }
arm_sync_command() { echo "ARM $1"; }
ask_for_event
''', names=("ask_for_event",))
        self.assertEqual([line for line in output.splitlines() if line.startswith("ARM")], ["ARM 1", "ARM 2"])

    def test_multi_station_boot_and_false_signal_keep_browser_panels_running(self):
        output = self.run_shell(r'''
ID=BAY101,MCB
ensure_dirs() { :; }
start_wifi_hotkey() { :; }
clear_sync_pending() { :; }
event_poll_loop() { echo HEARTBEATS; }
start_mpv_if_needed() { echo WRONG-MPV; exit 99; }
start_background_fetch_pending() { echo WRONG-PREFETCH; exit 99; }
wait_while_wizard() { :; }
chromium_running() { [[ -f "$STATE_DIR/chrome" ]]; }
kiosk_startx_alive() { chromium_running; }
x_display_running() { return 1; }
launch_web_kiosk() { echo "CHROME $1"; touch "$STATE_DIR/chrome"; }
kill_web_kiosk() { echo WRONG-KILL; exit 99; }
fetch_batch_to() {
  [[ -f "$STATE_DIR/chrome" ]] || { echo WRONG-STARTUP-FETCH; exit 99; }
  printf '{"response":{"success":true,"hasYoutube":false}}' |
    python3 "$SCRIPT_DIR/playback_mode.py" update "$PLAYBACK_MODE_FILE" "$ID" 2
}
sleep() { sync_web_kiosk; echo "STILL $(requested_kiosk_content)"; exit 0; }
main
''', names=("main", "requested_kiosk_content", "sync_web_kiosk", "ensure_web_kiosk_healthy"))
        self.assertEqual(output.count("CHROME ads"), 1)
        self.assertIn("HEARTBEATS", output)
        self.assertIn("STILL ads", output)
        self.assertNotIn("WRONG", output)
        self.assertEqual((self.root / "ACTIVE_MODE_FILE").read_text(), "chromium\n")

    def test_video_wait_and_mpv_start_abort_immediately_for_browser(self):
        self.set_mode(True)
        output = self.run_shell(r'''
mpv() { echo WRONG; exit 99; }
mpv_get_prop() { echo WRONG; exit 99; }
rc=0
mpv_wait_until_eof_with_timeout 300 || rc=$?
echo "wait=$rc"
rc=0
start_mpv_if_needed || rc=$?
echo "start=$rc"
''', names=("requested_kiosk_content", "browser_requested", "mpv_wait_until_eof_with_timeout", "start_mpv_if_needed"))
        self.assertEqual(output, "wait=3\nstart=3\n")

    def test_browser_sync_is_left_to_website_without_native_asset_downloads(self):
        self.set_mode(True)
        output = self.run_shell(r'''
fetch_batch_to() { echo WRONG; exit 99; }
start_asset_download_bg() { echo WRONG; exit 99; }
arm_sync_command 0 100 0
echo delegated-to-browser
''', names=("requested_kiosk_content", "browser_requested", "arm_sync_command"))
        self.assertEqual(output, "delegated-to-browser\n")

    def test_session_registration_precedes_the_first_scheduled_poll(self):
        output = self.run_shell(r'''
ask_for_event() { echo REGISTER; }
sleep() { echo "SLEEP $1"; exit 0; }
seconds_until_event_slot() { echo 29; }
event_poll_loop
''', names=("event_poll_loop",))
        self.assertTrue(output.endswith("REGISTER\nSLEEP 1\n"))

    def main_harness(self, initial, next_mode, *, daily_refresh=False):
        self.set_mode(initial)
        clock = r'''
date() {
  if [[ -f "$STATE_DIR/clock-started" ]]; then echo 86500;
  else touch "$STATE_DIR/clock-started"; echo 100; fi
}
''' if daily_refresh else ""
        return self.run_shell(clock + r'''
ensure_dirs() { :; }
start_wifi_hotkey() { :; }
clear_sync_pending() { :; }
event_poll_loop() { echo HEARTBEATS; }
fetches=0
fetch_batch_to() {
  fetches=$((fetches + 1))
  echo https://example.test/ad.png > "$3"
  echo 0 > "$4"
  echo 0 > "$5"
  if (( fetches > 1 )); then
    printf '{"response":{"success":true,"hasYoutube":%s}}' "$NEXT_MODE" |
      python3 "$SCRIPT_DIR/playback_mode.py" update "$PLAYBACK_MODE_FILE" "$ID" 2
  fi
}
promote_main_list() { mv "$1" "$MAIN_LIST"; }
start_background_fetch_pending() { :; }
start_mpv_if_needed() { echo MPV; MPV_HAS_DISPLAY=1; }
wait_while_wizard() { :; }
chromium_running() { [[ -f "$STATE_DIR/chrome" ]]; }
kiosk_startx_alive() { chromium_running; }
x_display_running() { return 1; }
launch_web_kiosk() { echo "CHROME $1"; touch "$STATE_DIR/chrome"; }
kill_web_kiosk() { echo KILL-CHROME; rm -f "$STATE_DIR/chrome"; }
sync_should_interrupt_playback() { return 1; }
apply_sync_if_due() { return 1; }
sleep() { :; }
play_url() {
  echo PLAY
  if [[ "$NEXT_MODE" == "true" ]]; then
    printf '{"response":{"success":true,"hasYoutube":true}}' |
      python3 "$SCRIPT_DIR/playback_mode.py" update "$PLAYBACK_MODE_FILE" "$ID" 2
    return 3
  fi
  exit 0
}
main
'''.replace("fetches=0", "NEXT_MODE=" + str(next_mode).lower() + "\nfetches=0"),
            names=("main", "normalize_url", "read_next_blast_index", "requested_kiosk_content",
                   "browser_requested", "sync_web_kiosk", "ensure_web_kiosk_healthy"),
            expected=70 if initial != next_mode or daily_refresh else 0)

    def test_chromium_keeps_polling_and_restarts_on_false_without_starting_mpv(self):
        output = self.main_harness(True, False)
        self.assertIn("CHROME ads", output)
        self.assertIn("HEARTBEATS", output)
        self.assertIn("RESTART: browser content changed", output)
        self.assertNotIn("MPV", output)
        self.assertNotIn("PLAY\n", output)

    def test_native_playback_restarts_on_true_and_false_boot_resumes_mpv(self):
        output = self.main_harness(False, True)
        self.assertIn("MPV", output)
        self.assertIn("RESTART: web content enabled", output)
        self.assertNotIn("CHROME", output)
        output = self.main_harness(False, False)
        self.assertIn("MPV", output)
        self.assertIn("PLAY", output)
        self.assertNotIn("CHROME", output)

    def test_daily_browser_refresh_restarts_with_fresh_session_credentials(self):
        output = self.main_harness(True, True, daily_refresh=True)
        self.assertIn("CHROME ads", output)
        self.assertIn("RESTART: daily browser refresh with a new playback session", output)
        self.assertNotIn("MPV", output)

    def test_chromium_detection_uses_process_name_not_launcher_path(self):
        for comm, args, running in (
            ("chromium", "/usr/lib/chromium/chromium --kiosk", True),
            ("chromium", "/opt/chromium/chromium --kiosk", True),
            ("sh", "/bin/sh /usr/bin/chromium --kiosk", False),
            ("xinit", "/usr/bin/xinit /usr/bin/chromium --kiosk", False),
            ("bash", "/bin/bash /data/player/tvads.sh --chromium-session", False),
        ):
            with self.subTest(comm=comm, args=args):
                output = self.run_shell(
                    f"PROCESS_COMM={shlex.quote(comm)}\nPROCESS_ARGS={shlex.quote(args)}\n" + r'''
pgrep() {
  case "$1" in
    -x) [[ "$PROCESS_COMM" == "$2" ]];;
    -f) [[ "$PROCESS_ARGS" =~ $2 ]];;
    *) exit 99;;
  esac
}
if chromium_running; then echo running; else echo stopped; fi
''', names=("chromium_running",))
                self.assertEqual(output.strip(), "running" if running else "stopped")

    def launch_kiosk(self, stations="STATION", orientation="90", *, unmuted_id=None,
                     content="ads", secret="secret-test", supervised=False,
                     kms_output="", kms_status=0, expected_size="1920,1080",
                     x_output="", x_status=0, kms_before_output="", main_startup=False):
        for name in ("launched", "args", "rotation", "xset-args", "x-ready",
                     "startx-args", "xrandr-calls", "kms-calls"):
            (self.root / name).unlink(missing_ok=True)
        (self.root / "kms-output").write_text(kms_output)
        (self.root / "kms-before-output").write_text(kms_before_output)
        (self.root / "x-output").write_text(x_output)
        settings = {"ID": stations, "ORIENTATION": orientation, "UNMUTED_ID": unmuted_id,
                    "WEB_STATION": "WEB-STATION", "DEVICE_SECRET": secret,
                    "KMS_STATUS": str(kms_status), "XRANDR_STATUS": str(x_status)}
        config_file = self.root / "config.env"
        config_file.write_text("\n".join(f"unset {key}" if value is None else f"{key}={shlex.quote(value)}"
                                         for key, value in settings.items()) + "\n")
        config = f"CONFIG={shlex.quote(str(config_file))}\n" + config_block()
        command = "sync_web_kiosk" if supervised else "launch_web_kiosk " + shlex.quote(content)
        if main_startup:
            command = r'''
ensure_dirs() { :; }
start_wifi_hotkey() { :; }
clear_sync_pending() { :; }
fetch_batch_to() { : > "$3"; echo 0 > "$4"; echo 0 > "$5"; }
promote_main_list() { mv "$1" "$MAIN_LIST"; }
read_next_blast_index() { :; }
start_mpv_if_needed() { exit 99; }
start_background_fetch_pending() { exit 99; }
wait_while_wizard() { wait; exit 0; }
# Exercise the real immediate heartbeat and request builder, then end the worker.
sleep() {
  [[ -f "$STATE_DIR/heartbeat.json" ]] && exit 0
  command sleep "$@"
}
curl() {
  while (( $# )); do
    if [[ "$1" == -d ]]; then
      printf '%s' "$2" > "$STATE_DIR/heartbeat.json"
      break
    fi
    shift
  done
  echo '{"response":{"success":true,"data":[]}}'
}
main
'''
        output = self.run_shell(config + r'''
rm() { :; }
kmsprint() {
  if [[ -f "$STATE_DIR/x-ready" ]]; then
    echo after-X >> "$STATE_DIR/kms-calls"
    cat "$STATE_DIR/kms-output"
  else
    echo before-X >> "$STATE_DIR/kms-calls"
    cat "$STATE_DIR/kms-before-output"
  fi
  return "$KMS_STATUS"
}
pgrep() {
  [[ -f "$STATE_DIR/launched" ]] || return 1
  # Debian's /usr/bin launcher execs the browser at a different path.
  case "$1" in
    -x) [[ chromium == "$2" ]];;
    -f) [[ '/usr/lib/chromium/chromium --kiosk' =~ $2 ]];;
    *) exit 99;;
  esac
}
kiosk_startx_alive() { [[ -n "$CHROMIUM_PID" ]]; }
x_display_running() { return 1; }
kill_web_kiosk() { exit 99; }
startx() {
  printf '%s\n' "$@" > "$STATE_DIR/startx-args"
  touch "$STATE_DIR/x-ready"
  DISPLAY=:77 XAUTHORITY="$STATE_DIR/session-auth" "$1" "$2" "$3" "$4"
}
xset() {
  [[ "$DISPLAY" == :77 && "$XAUTHORITY" == "$STATE_DIR/session-auth" ]] || return 1
  printf '%s\n' "$*" >> "$STATE_DIR/xset-args"
}
xrandr() {
  [[ -f "$STATE_DIR/x-ready" && "$DISPLAY" == :77 && "$XAUTHORITY" == "$STATE_DIR/session-auth" ]] || return 1
  printf '%s\n' "$*" >> "$STATE_DIR/xrandr-calls"
  [[ "$*" == --current ]] || { touch "$STATE_DIR/rotation"; return 1; }
  cat "$STATE_DIR/x-output"
  return "$XRANDR_STATUS"
}
exec() {
  printf '%s\n' "$@" > "$STATE_DIR/args"
  touch "$STATE_DIR/launched"
}
export STATE_DIR KMS_STATUS XRANDR_STATUS
export -f kmsprint xset xrandr exec
# Use real sleep while the harmless startx stub writes its marker.
''' + command + "\nwait\n", names=("kms_window_size", "launch_web_kiosk", "sync_web_kiosk",
                                          "ensure_web_kiosk_healthy", "requested_kiosk_content",
                                          "chromium_running", "main", "event_poll_loop",
                                          "ask_for_event", "build_event_body", "update_playback_mode"))
        args = (self.root / "args").read_text().splitlines()
        url = next(arg for arg in args if arg.startswith("https://"))
        parts = urlsplit(url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "example.test")
        self.assertEqual(parts.fragment, "")
        self.assertEqual(args[0], "/usr/bin/chromium")
        for flag in ("--kiosk", "--start-fullscreen", "--window-position=0,0",
                     f"--window-size={expected_size}", "--force-device-scale-factor=1",
                     "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"):
            self.assertIn(flag, args)
        startx_args = (self.root / "startx-args").read_text().splitlines()
        self.assertEqual(startx_args[:4], ["/bin/bash", str(ROOT / "tvads.sh"), "--chromium-session", url])
        self.assertEqual(startx_args[4:], ["--", ":0", "-nocursor", "-s", "off"])
        self.assertEqual((self.root / "xrandr-calls").read_text(), "--current\n")
        if (self.root / "kms-calls").exists():
            self.assertEqual((self.root / "kms-calls").read_text(), "after-X\n")
        self.assertEqual((self.root / "xset-args").read_text().splitlines(),
                         ["q", "s off", "s noblank", "-dpms", "dpms 0 0 0"])
        self.assertFalse((self.root / "rotation").exists(), "Website rotation must not also rotate X")
        self.assertNotIn(secret, output)
        self.assertNotIn("https://", output)
        if x_status == 0 and x_output.startswith("Screen") and expected_size != "1920,1080":
            self.assertIn("Chromium window size from X: " + expected_size.replace(",", "x"), output)
        elif kms_status == 0 and kms_output and expected_size != "1920,1080":
            self.assertIn("Chromium window size from kmsprint (after X startup): " + expected_size.replace(",", "x"), output)
        elif kms_status != 0 or not kms_output:
            self.assertIn("WARN: no active resolution from X or kmsprint; using Chromium window size 1920x1080", output)
        return parts

    def test_layout_startup_registers_the_same_session_as_the_4k_browser(self):
        station = "APNIMANDI-41081-94538-MAIN"
        mode.update_state(self.root / "PLAYBACK_MODE_FILE", station, 1,
                          response(requiresBrowser=True))
        parts = self.launch_kiosk(station, main_startup=True,
                                  x_output="Screen 0: current 3840 x 2160, maximum 16384 x 16384\n",
                                  expected_size="3840,2160")
        body = json.loads((self.root / "heartbeat.json").read_text())
        self.assertEqual(body["playbackSession"], parse_qs(parts.query)["playbackSession"][0])
        self.assertEqual(body["adStationIds"], [station])
        self.assertEqual(body["adStationId"], station)
        self.assertEqual(body["playbackMode"], "chromium")

    def test_kiosk_sizes_after_x_changes_console_mode_to_4k(self):
        console = "Connector 0 (33) HDMI-A-1 (connected)\n    Crtc 2 (92) 1536x768@60.00\n"
        x_state = "Screen 0: minimum 320 x 200, current 3840 x 2160, maximum 16384 x 16384\n"
        # The X screen is authoritative even if KMS reports an old console mode.
        self.launch_kiosk(x_output=x_state, kms_before_output=console, kms_output=console,
                          expected_size="3840,2160")
        self.assertFalse((self.root / "kms-calls").exists())

    def test_kiosk_kms_fallback_also_runs_after_x_changes_the_mode(self):
        console = "Connector 0 (33) HDMI-A-1 (connected)\n    Crtc 2 (92) 1536x768@60.00\n"
        active = "Connector 0 (33) HDMI-A-1 (connected)\n    Crtc 2 (92) 3840x2160@60.00\n"
        self.launch_kiosk(x_status=127, kms_before_output=console, kms_output=active,
                          expected_size="3840,2160")

    def test_kiosk_uses_x_current_size_not_supported_modes_or_rotated_dimensions(self):
        x_state = """Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 32767 x 32767
HDMI-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis)
   3840x2160     60.00 +
   2560x1440     60.00*
"""
        for angle in ("0", "90", "180", "270"):
            with self.subTest(angle=angle):
                self.launch_kiosk(orientation=angle, x_output=x_state, expected_size="2560,1440")

    def test_x_resolution_detection_rejects_errors_and_bad_dimensions(self):
        for output, status in (("", 0), ("Screen 0: current 0 x 2160,", 0),
                               ("Screen 0: current 3840 x invalid,", 0),
                               ("Screen 0: current 3840 x 2160,", 1)):
            with self.subTest(output=output, status=status):
                result = self.run_shell(
                    f"xrandr() {{ printf '%s' {shlex.quote(output)}; return {status}; }}\n"
                    "if x_window_size; then exit 99; fi\necho NO-X\n", names=("x_window_size",))
                self.assertEqual(result, "NO-X\n")

    def test_x_resolution_detection_handles_missing_command(self):
        output = self.run_shell(r'''
command() {
  if [[ "$*" == "-v xrandr" ]]; then return 1; fi
  builtin command "$@"
}
if x_window_size; then exit 99; fi
echo NO-X
''', names=("x_window_size",))
        self.assertEqual(output, "NO-X\n")

    def test_session_still_launches_when_blanking_setup_fails_and_returns_browser_status(self):
        output = self.run_shell(r'''
xrandr() { echo 'Screen 0: minimum 320 x 200, current 3840 x 2160, maximum 16384 x 16384'; }
xset() { return 1; }
exec() {
  [[ "$1" == /usr/bin/chromium ]] || return 99
  echo BROWSER-STARTED
  return 37
}
export -f xrandr xset exec
/bin/bash "$SCRIPT_DIR/tvads.sh" --chromium-session 'https://example.test/ads/STATION?secret=test-only'
''', expected=37)
        self.assertIn("Chromium window size from X: 3840x2160", output)
        self.assertIn("WARN: could not disable display blanking", output)
        self.assertIn("BROWSER-STARTED", output)
        self.assertNotIn("secret", output)

    def test_kiosk_uses_active_4k_mode_instead_of_scaled_framebuffer(self):
        kms_output = """Connector 0 (32) HDMI-A-1 (connected)
  Encoder 0 (31) TMDS
    Crtc 3 (96) 3840x2160@60.00 594.000 3840/176/88/296/+ 2160/8/10/72/+ 60 (60.00)
      Plane 3 (86) fb-id: 340 (crtcs: 3) 0,0 1920x1080 -> 0,0 3840x2160
        FB 340 1920x1080 RG16
"""
        for content, orientation in (("ads", "0"), ("ads", "90"), ("bay101poker", "270")):
            with self.subTest(content=content, orientation=orientation):
                self.launch_kiosk(content=content, orientation=orientation, kms_output=kms_output,
                                  expected_size="3840,2160")

    def test_kiosk_skips_disconnected_and_inactive_outputs_and_keeps_first_active(self):
        self.launch_kiosk(kms_output="""Connector 0 (32) HDMI-A-1 (disconnected)
  Encoder 0 (31) TMDS
    Crtc 0 (96) 3840x2160@60.00
Connector 1 (42) HDMI-A-2 (connected)
  Encoder 1 (41) TMDS
    Crtc 1 (97)
      Plane 1 (87) fb-id: 341 (crtcs: 1) 0,0 3840x2160 -> 0,0 3840x2160
        FB 341 3840x2160 RG16
Connector 2 (52) DSI-1 (connected)
  Encoder 2 (51) DSI
    Crtc 2 (98) 1280x720 74.250 1280/110/40/220/+ 720/5/5/20/+ 60 (60.00)
Connector 3 (62) DSI-2 (connected)
  Encoder 3 (61) DSI
    Crtc 3 (99) 1920x1080@60.00
""", expected_size="1280,720")

    def test_kiosk_resolution_fallback_is_always_1080p(self):
        for kms_output, status in (
            ("", 0),
            ("unrecognized output", 0),
            ("Connector 0 (32) HDMI-A-1 (connected)\n    Crtc 0 (96) 0x0@60.00\n", 0),
            ("Connector 0 (32) HDMI-A-1 (connected)\n    0 3840x2160@60.00\n", 0),
            ("Connector 0 (32) HDMI-A-1 (connected)\n    Crtc 0 (96) 3840x2160@60.00\n", 1),
            ("", 127),
        ):
            with self.subTest(kms_output=kms_output, status=status):
                self.launch_kiosk(kms_output=kms_output, kms_status=status)

    def test_kms_resolution_detection_handles_missing_command(self):
        output = self.run_shell(r'''
command() {
  if [[ "$*" == "-v kmsprint" ]]; then return 1; fi
  builtin command "$@"
}
if kms_window_size; then exit 99; fi
echo NO-KMS
''', names=("kms_window_size",))
        self.assertEqual(output, "NO-KMS\n")

    def assert_ads_url(self, parts, stations, orientation, *, unmute=None, secret="secret-test"):
        self.assertEqual(unquote(parts.path), "/ads/" + stations)
        expected = {
            "kiosk": ["1"], "deviceId": ["device-test"], "secret": [secret],
            "playbackSession": ["11111111-1111-4111-8111-111111111111"],
            "orientation": [orientation], "index": ["-2"],
        }
        if unmute is not None:
            expected["unmute"] = [unmute]
        self.assertEqual(parse_qs(parts.query, keep_blank_values=True), expected)

    def test_multi_station_preserves_path_order_index_audio_rotation_and_escaped_credentials(self):
        secret = "secret&+=?# /%"
        parts = self.launch_kiosk(" c,a,b,a ", "270", unmuted_id=" b ", secret=secret, supervised=True)
        self.assert_ads_url(parts, "C,A,B", "270", unmute="B", secret=secret)

    def test_ads_orientation_preserves_stations_unmute_and_kiosk_credentials(self):
        for stations, unmuted_id, unmute in (("STATION", " station ", "STATION"),
                                            ("BABY,PEPE", " baby ", "BABY"),
                                            ("STATION", " child ", "CHILD"),
                                            ("BABY,PEPE", " child ", "CHILD")):
            for angle in ("0", "90", "180", "270"):
                with self.subTest(stations=stations, angle=angle):
                    parts = self.launch_kiosk(stations, angle, unmuted_id=unmuted_id)
                    self.assert_ads_url(parts, stations, angle, unmute=unmute)

    def test_nested_unmute_is_encoded_as_one_query_value(self):
        parts = self.launch_kiosk(unmuted_id=" child&index=99# /% ")
        self.assert_ads_url(parts, "STATION", "90", unmute="CHILD&INDEX=99# /%")

    def test_missing_blank_or_invalid_browser_orientation_defaults_to_zero(self):
        for orientation in (None, "", "45", "-90", "360", "90.0", "invalid", "90&unmute=PEPE"):
            with self.subTest(orientation=orientation):
                parts = self.launch_kiosk("BABY,PEPE", orientation, unmuted_id="baby")
                self.assert_ads_url(parts, "BABY,PEPE", "0", unmute="BABY")

    def test_orientation_preserves_muted_defaults_and_query_encoding(self):
        secret = "secret&+=?# /%"
        for unmuted_id in (None, "", "  ", "BABY,PEPE"):
            with self.subTest(unmuted_id=unmuted_id):
                parts = self.launch_kiosk("BABY,PEPE", "270", unmuted_id=unmuted_id, secret=secret)
                self.assert_ads_url(parts, "BABY,PEPE", "270", secret=secret)

    def test_supervisor_uses_current_orientation_for_single_and_multiple_stations(self):
        self.set_mode(True)
        for stations in ("STATION", "BABY,PEPE"):
            for angle in ("90", "270"):
                with self.subTest(stations=stations, angle=angle):
                    # Each process re-reads config on startup/recovery/reconfiguration.
                    parts = self.launch_kiosk(stations, angle, supervised=True)
                    self.assert_ads_url(parts, stations, angle)
                    self.assertEqual((self.root / "ACTIVE_MODE_FILE").read_text(), "chromium\n")

    def test_scheduled_web_pages_keep_orientation_path_and_rotate_only_in_website(self):
        for angle in ("0", "90", "180", "270"):
            with self.subTest(angle=angle):
                parts = self.launch_kiosk(orientation=angle, content="bay101poker", unmuted_id="CHILD")
                self.assertEqual(parts.path, f"/player/{angle}/bay101poker")
                self.assertEqual(parse_qs(parts.query), {"id": ["WEB-STATION"], "secret": ["secret-test"]})


if __name__ == "__main__":
    unittest.main()

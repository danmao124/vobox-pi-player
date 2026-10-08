import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import playback_mode as mode
import playback_report as report


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

    def test_browser_signal_interrupts_image_without_waiting_for_duration(self):
        self.update(1, hasYoutube=False)
        with patch.object(report.time, "monotonic_ns", return_value=100_000_000_000), \
                patch.object(report.time, "sleep", side_effect=lambda _: self.update(2, hasYoutube=True)) as sleeper:
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

    def test_kiosk_url_credentials_autoplay_and_rotation(self):
        output = self.run_shell(r'''
ORIENTATION=90
rm() { :; }
chromium_running() { [[ -f "$STATE_DIR/launched" ]]; }
kiosk_startx_alive() { [[ -n "$CHROMIUM_PID" ]]; }
x_display_running() { return 1; }
kill_web_kiosk() { exit 99; }
startx() { printf '%s\n' "$@" > "$STATE_DIR/args"; touch "$STATE_DIR/launched"; }
xset() { :; }
xrandr() {
  if [[ "$1" == "--query" ]]; then echo 'HDMI-1 connected primary';
  else printf '%s\n' "$*" > "$STATE_DIR/rotation"; fi
}
# Use real sleep while the harmless startx stub writes its marker.
launch_web_kiosk ads
wait
''', names=("launch_web_kiosk",))
        args = (self.root / "args").read_text().splitlines()
        url = next(arg for arg in args if arg.startswith("https://"))
        parts = urlsplit(url)
        self.assertEqual(parts.path, "/ads/STATION")
        self.assertEqual(parse_qs(parts.query), {
            "kiosk": ["1"], "deviceId": ["device-test"], "secret": ["secret-test"],
            "playbackSession": ["11111111-1111-4111-8111-111111111111"],
        })
        self.assertIn("--autoplay-policy=no-user-gesture-required", args)
        self.assertEqual((self.root / "rotation").read_text().strip(), "--output HDMI-1 --rotate right")
        self.assertNotIn("secret-test", output)
        self.assertNotIn("https://", output)


if __name__ == "__main__":
    unittest.main()

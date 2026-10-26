from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def shell_functions(*names):
    source = (ROOT / "tvads.sh").read_text()
    return "\n".join(
        name + "() {" + source.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        for name in names
    )


class BackgroundDownloadTests(unittest.TestCase):
    def download_script(self, root):
        return shell_functions(
            "cache_path_for_url", "asset_lock_path", "asset_download_in_progress",
            "claim_asset_download", "start_asset_download_bg", "cache_asset",
            "abort_in_flight_asset_downloads", "asset_downloads_in_progress",
        ) + f"\nASSET_DIR={shlex.quote(str(root))}\n" + r'''
CURL_ASSET_OPTS=(stub)
log() { echo "$*" >&2; }
build_curl_auth_headers() { curl_headers=(stub); }
note_fetch_reach_ok() { :; }
internet_ping_ok() { return 0; }
wait_for() {
  for _ in {1..500}; do [[ -f "$1" ]] && return 0; sleep 0.01; done
  return 99
}
wait_finished() {
  for _ in {1..500}; do
    [[ ! -f "$ASSET_DIR/video.mp4.lock" ]] && return 0
    sleep 0.01
  done
  return 98
}
'''

    def test_parallel_encounters_reuse_download_during_startup_and_after_batch_change(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = self.download_script(root) + r'''
# Force overlapping callers while authentication is still being prepared.
build_curl_auth_headers() { wait_for "$ASSET_DIR/auth-release"; curl_headers=(stub); }
curl() {
  echo attempt >> "$ASSET_DIR/attempts"
  printf partial > "$tmp"
  touch "$ASSET_DIR/download-started"
  wait_for "$ASSET_DIR/release"
  printf complete > "$tmp"
}
for _ in {1..12}; do start_asset_download_bg https://cdn.test/video.mp4 & done
wait
touch "$ASSET_DIR/queued"
wait_for "$ASSET_DIR/download-started"
echo https://cdn.test/another-batch.png > "$ASSET_DIR/main.txt"
asset_downloads_in_progress
cache_asset 'https://cdn.test/video.mp4?new-signature=1' || true
touch "$ASSET_DIR/repeated"
wait_for "$ASSET_DIR/release"
wait_finished
'''
            with (root / "download.log").open("w") as log, subprocess.Popen(
                ["bash", "-euo", "pipefail", "-c", script], stdout=log, stderr=log,
            ) as process:
                try:
                    self.wait_for_file(root / "queued")
                    self.wait_for_file(root / "video.mp4.lock")
                    worker = (root / "video.mp4.lock").read_text()
                    self.assertFalse((root / "attempts").exists())
                    (root / "auth-release").touch()
                    self.wait_for_file(root / "repeated")
                    self.assertEqual((root / "video.mp4.lock").read_text(), worker)
                    self.assertEqual((root / "video.mp4.tmp").read_text(), "partial")
                finally:
                    (root / "auth-release").touch()
                    (root / "release").touch()
                    process.wait(timeout=7)
                self.assertEqual(process.returncode, 0, (root / "download.log").read_text())
            self.assertEqual((root / "attempts").read_text().splitlines(), ["attempt"])
            self.assertEqual((root / "video.mp4").read_text(), "complete")
            self.assertFalse((root / "video.mp4.tmp").exists())

    def test_failed_download_can_retry_and_completed_file_is_reused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "video.mp4.lock").write_text("999999999\n")
            (root / "video.mp4.tmp").write_text("stale partial")
            script = self.download_script(root) + r'''
curl() {
  [[ ! -f "$tmp" ]] || return 99
  if [[ ! -f "$ASSET_DIR/first-attempt" ]]; then
    touch "$ASSET_DIR/first-attempt"
    printf failed > "$tmp"
    return 22
  fi
  echo retry >> "$ASSET_DIR/attempts"
  printf complete > "$tmp"
}
cache_asset https://cdn.test/video.mp4 || true
wait_for "$ASSET_DIR/first-attempt"
wait_finished
[[ ! -f "$ASSET_DIR/video.mp4.tmp" ]]
cache_asset https://cdn.test/video.mp4 || true
wait_for "$ASSET_DIR/video.mp4"
wait_finished
cache_asset https://cdn.test/video.mp4
'''
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "attempts").read_text().splitlines(), ["retry"])
            self.assertEqual(result.stdout.strip(), str(root / "video.mp4"))

    def test_network_abort_stops_worker_curl_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = self.download_script(root) + r'''
curl() {
  trap 'touch "$ASSET_DIR/curl-stopped"; exit 143' TERM
  printf partial > "$tmp"
  touch "$ASSET_DIR/download-started"
  wait_for "$ASSET_DIR/never-release"
}
cache_asset https://cdn.test/video.mp4 || true
wait_for "$ASSET_DIR/download-started"
abort_in_flight_asset_downloads
wait_finished
[[ ! -f "$ASSET_DIR/video.mp4.tmp" ]]
! asset_downloads_in_progress
'''
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "curl-stopped").exists())
            self.assertFalse((root / "video.mp4").exists())

    def wait_for_file(self, path):
        deadline = time.monotonic() + 3
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail(f"Player did not reach {path.name} while the download was waiting")
            time.sleep(0.01)

    def test_cached_assets_remain_available_while_new_download_is_waiting(self):
        for download_result in (0, 22):
            with self.subTest(download_result=download_result), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                cached = root / "old.jpg"
                cached.write_text("cached ad")
                script = shell_functions(
                    "cache_path_for_url", "asset_lock_path", "asset_download_in_progress",
                    "claim_asset_download", "start_asset_download_bg", "cache_asset",
                )
                script += f"\nASSET_DIR={shlex.quote(d)}\nDOWNLOAD_RESULT={download_result}\n"
                script += r'''
CURL_ASSET_OPTS=(stub)
build_curl_auth_headers() { curl_headers=(stub); }
log() { echo "$*"; }
note_fetch_reach_ok() { :; }
internet_ping_ok() { return 0; }
curl() {
  touch "$ASSET_DIR/download-started"
  for _ in {1..500}; do
    if [[ -f "$ASSET_DIR/release" ]]; then
      printf 'downloaded ad' > "$tmp"
      return "$DOWNLOAD_RESULT"
    fi
    sleep 0.01
  done
  return 99
}
rc=0
src="$(cache_asset https://example.com/new.jpg)" || rc=$?
printf '%s' "$src" > "$ASSET_DIR/missing-result"
echo "$rc" > "$ASSET_DIR/missing-status"
cache_asset https://example.com/old.jpg > "$ASSET_DIR/cached-result"
touch "$ASSET_DIR/playback-continued"
# Keep the harness alive until the download and its lock cleanup finish.
for _ in {1..500}; do
  if [[ -f "$ASSET_DIR/release" && ! -f "$ASSET_DIR/new.jpg.lock" ]]; then
    exit 0
  fi
  sleep 0.01
done
exit 98
'''
                # stderr goes to a file so the background logger cannot keep
                # communicate() waiting after the foreground shell exits.
                with (root / "download.log").open("w") as log, subprocess.Popen(
                    ["bash", "-euo", "pipefail", "-c", script],
                    stdout=subprocess.PIPE, stderr=log, text=True,
                ) as process:
                    try:
                        self.wait_for_file(root / "download-started")
                        self.wait_for_file(root / "new.jpg.lock")
                        self.wait_for_file(root / "playback-continued")
                        self.assertFalse((root / "new.jpg").exists())
                        self.assertEqual((root / "missing-status").read_text().strip(), "1")
                        self.assertEqual((root / "missing-result").read_text(), "")
                        self.assertEqual((root / "cached-result").read_text().strip(), str(cached))
                    finally:
                        (root / "release").touch()
                        process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, (root / "download.log").read_text())
                self.assertEqual((root / "new.jpg").exists(), download_result == 0)
                self.assertFalse((root / "new.jpg.tmp").exists())
                self.assertFalse((root / "new.jpg.lock").exists())


class PlaybackLoopSyncTests(unittest.TestCase):
    def run_sync(self, *, between_assets=False, ready=True):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            names = (
                "MAIN_LIST", "PENDING_LIST", "INDEX_FILE", "NEXT_FILE", "NEXT_BLAST_FILE",
                "SYNC_LIST", "SYNC_AT_FILE", "SYNC_NEXT_FILE", "SYNC_BLAST_FILE", "PLAYBACK_HISTORY",
            )
            script = "\n".join(f"{name}={shlex.quote(str(root / name))}" for name in names)
            script += f"\nASSET_DIR={shlex.quote(d)}\nBETWEEN_ASSETS={int(between_assets)}\n"
            script += shell_functions(
                "main", "apply_sync_if_due", "promote_main_list", "clear_sync_pending",
                "normalize_url", "cache_path_for_url", "read_next_blast_index",
            )
            if ready:
                (root / "new.jpg").write_text("cached ad")
            script += r'''
PLAYER_VERSION=test EVENT_SLOT=0 blast_idx=0 SCRIPT_DIR=unused ID=STATION
KIOSK_BOOTSTRAPPED=""
browser_requested() { return 1; }
checks=0 plays=0
ensure_dirs() { :; }
start_wifi_hotkey() { :; }
event_poll_loop() { :; }
fetch_batch_to() {
  echo https://example.com/old.jpg > "$3"
  echo 1 > "$4"
  echo 0 > "$5"
}
sync_web_kiosk() { echo kiosk-synced; }
start_background_fetch_pending() { :; }
start_mpv_if_needed() { :; }
wait_while_wizard() { :; }
stage_sync() {
  echo https://example.com/new.jpg > "$SYNC_LIST"
  echo 100 > "$SYNC_AT_FILE"
  echo 2 > "$SYNC_NEXT_FILE"
  echo 0 > "$SYNC_BLAST_FILE"
}
sync_should_interrupt_playback() {
  checks=$((checks + 1))
  # Simulate a sync becoming due between the precheck and apply_sync_if_due.
  if (( BETWEEN_ASSETS && checks == 2 )); then stage_sync; fi
  return 1
}
date() { echo 100; }
bump_fetch_gen() { :; }
asset_downloads_in_progress() { return 1; }
swap_pending_if_any() { :; }
python3() { :; }
sleep() { echo "SLEEP $1"; }
log() { echo "$*"; }
play_url() {
  plays=$((plays + 1))
  echo "PLAY $1 position=$item_position"
  if [[ "$1" == */new.jpg ]] || (( plays == 2 )); then exit 0; fi
  stage_sync
  return 2
}
main
'''
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                capture_output=True, text=True, check=True, timeout=5,
            )
            return result.stdout

    def assert_immediate_sync(self, output):
        self.assertIn("PLAY https://example.com/new.jpg position=1", output)
        self.assertNotIn("SLEEP", output)
        applied = output.index("Applying sync batch")
        refreshed = output.index("kiosk-synced", applied)
        played = output.index("PLAY https://example.com/new.jpg")
        self.assertLess(refreshed, played)

    def test_sync_during_playback_starts_new_batch_without_retry_delay(self):
        self.assert_immediate_sync(self.run_sync())

    def test_sync_between_assets_starts_new_batch_without_retry_delay(self):
        output = self.run_sync(between_assets=True)
        self.assert_immediate_sync(output)
        self.assertNotIn("PLAY https://example.com/old.jpg", output)

    def test_sync_with_missing_assets_keeps_current_batch(self):
        output = self.run_sync(ready=False)
        self.assertIn("Sync missed: assets not ready", output)
        self.assertNotIn("PLAY https://example.com/new.jpg", output)
        self.assertEqual(output.count("PLAY https://example.com/old.jpg position=1"), 2)


class BatchAdvanceTests(unittest.TestCase):
    def test_next_batch_plays_while_downloads_continue_even_if_nothing_was_playable(self):
        for playable in (0, 1):
            with self.subTest(playable=playable), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                names = ("MAIN_LIST", "PENDING_LIST", "INDEX_FILE", "NEXT_FILE",
                         "NEXT_BLAST_FILE", "PLAYBACK_HISTORY")
                script = "\n".join(f"{name}={shlex.quote(str(root / name))}" for name in names)
                script += f"\nPLAYABLE={playable}\n" + shell_functions(
                    "main", "promote_main_list", "swap_pending_if_any", "read_next_blast_index",
                ) + r'''
PLAYER_VERSION=test EVENT_SLOT=0 blast_idx=0 SCRIPT_DIR=unused ID=TEST
KIOSK_BOOTSTRAPPED="" plays=0
ensure_dirs() { :; }
start_wifi_hotkey() { :; }
clear_sync_pending() { :; }
sync_web_kiosk() { :; }
event_poll_loop() { :; }
start_mpv_if_needed() { :; }
wait_while_wizard() { :; }
cleanup_cache() { :; }
browser_requested() { return 1; }
sync_should_interrupt_playback() { return 1; }
apply_sync_if_due() { return 1; }
normalize_url() { echo "$1"; }
asset_downloads_in_progress() { return 0; }
internet_ping_ok() { return 0; }
abort_in_flight_asset_downloads() { echo WRONG-ABORT; exit 99; }
python3() { :; }
log() { echo "$*"; }
sleep() { echo "SLEEP $1"; }
fetch_batch_to() {
  printf 'https://cdn.test/cached.png\nhttps://cdn.test/downloading.mp4\n' > "$3"
  echo 1 > "$4"
  echo 0 > "$5"
}
start_background_fetch_pending() { echo https://cdn.test/next.png > "$PENDING_LIST"; }
play_url() {
  plays=$((plays + 1))
  (( plays <= 3 )) || exit 99
  echo "PLAY $1"
  [[ "$1" != */next.png ]] || exit 0
  [[ "$1" == */cached.png ]] && (( PLAYABLE ))
}
main
'''
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PLAY https://cdn.test/next.png", result.stdout)
                self.assertEqual("SLEEP 5" in result.stdout, not playable)
                self.assertNotIn("WRONG-ABORT", result.stdout)

    def test_waiting_for_next_batch_does_not_replace_active_api_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = f"STATE_DIR={shlex.quote(d)}\n" + shell_functions(
                "read_fetch_gen", "bump_fetch_gen", "start_background_fetch_pending",
                "pending_fetch_in_progress", "swap_pending_if_any",
            ) + r'''
FETCH_GEN_FILE="$STATE_DIR/gen" PENDING_LIST="$STATE_DIR/pending"
INDEX_FILE="$STATE_DIR/index" blast_idx=0
echo 0 > "$FETCH_GEN_FILE"
echo 1 > "$INDEX_FILE"
cleanup_cache() { :; }
read_next_blast_index() { :; }
background_fetch_pending() {
  echo "$3" >> "$STATE_DIR/requests"
  for _ in {1..500}; do
    [[ ! -f "$STATE_DIR/release" ]] || return 0
    sleep 0.01
  done
  return 99
}
start_background_fetch_pending 1 0
original_pid="$PENDING_FETCH_PID"
swap_pending_if_any
swap_pending_if_any
[[ "$PENDING_FETCH_PID" == "$original_pid" ]]
[[ "$(read_fetch_gen)" == 1 ]]
touch "$STATE_DIR/release"
for _ in {1..500}; do pending_fetch_in_progress || break; sleep 0.01; done
! pending_fetch_in_progress
# A completed/failed request must still be retried if it published no batch.
swap_pending_if_any
for _ in {1..500}; do pending_fetch_in_progress || break; sleep 0.01; done
! pending_fetch_in_progress
[[ "$(read_fetch_gen)" == 2 ]]
'''
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "requests").read_text().splitlines(), ["1", "2"])


class PlaybackFailureTests(unittest.TestCase):
    def run_play(self, *, cached, confirmed, url):
        script = shell_functions("play_url") + f"\nCACHED={int(cached)} CONFIRMED={int(confirmed)}\n"
        script += f"EXPECTED_KIND={'video' if url.endswith('.mp4') else 'image'}\n"
        script += r'''
normalize_url() { echo "$1"; }
browser_requested() { return 1; }
cache_asset() { (( CACHED )) || return 1; echo /cache/asset; }
wait_while_wizard() { :; }
start_mpv_if_needed() { echo START-MPV; }
is_video() { [[ "$1" == *.mp4 ]]; }
mpv_send() { echo "MPV $1"; }
mpv_get_prop_data() { echo /cache/asset; }
mpv_get_duration_secs() { echo 22; }
mpv_wait_until_eof_with_timeout() { echo VIDEO-WAIT; }
log() { echo "$*"; }
python3() {
  case "$2" in
    load)
      [[ "$8" == "$EXPECTED_KIND" ]] || return 97
      (( CONFIRMED )) || return 1; echo 123 ;;
    wait-image) echo IMAGE-WAIT ;;
  esac
}
SCRIPT_DIR=unused MPV_SOCK=unused MAIN_LIST=unused item_position=1 PLAYBACK_HISTORY=unused
IMAGE_SECONDS=15 WIZARD_LOCK=unused SYNC_AT_FILE=unused SYNC_LIST=unused
PLAYBACK_MODE_FILE=unused ID=TEST ASSET_DIR=unused
rc=0
'''
        script += f"play_url {shlex.quote(url)} || rc=$?\necho result=$rc\n"
        return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                              capture_output=True, text=True, check=True, timeout=5).stdout

    def test_downloading_video_does_not_send_any_mpv_commands_or_wait(self):
        output = self.run_play(cached=False, confirmed=False, url="https://cdn.test/video.mp4")
        self.assertIn("skip (not cached yet)", output)
        self.assertIn("result=1", output)
        self.assertNotIn("MPV", output)
        self.assertNotIn("WAIT", output)

    def test_failed_load_never_reloads_or_waits_out_image_or_video_slot(self):
        for ext in ("png", "mp4"):
            with self.subTest(ext=ext):
                output = self.run_play(cached=True, confirmed=False, url=f"https://cdn.test/asset.{ext}")
                self.assertIn("skip (mpv playback not confirmed)", output)
                self.assertIn("result=1", output)
                self.assertNotIn("loadfile", output)
                self.assertNotIn("WAIT", output)

    def test_confirmed_image_and_video_keep_their_normal_wait(self):
        for ext, wait in (("png", "IMAGE-WAIT"), ("mp4", "VIDEO-WAIT")):
            with self.subTest(ext=ext):
                output = self.run_play(cached=True, confirmed=True, url=f"https://cdn.test/asset.{ext}")
                self.assertIn(wait, output)
                self.assertIn("Playback confirmed", output)
                self.assertIn("result=0", output)

    def test_sync_cannot_interrupt_video_or_asset_boundary_until_files_are_cached(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sync.txt").write_text("https://cdn.test/video.mp4\n")
            (root / "sync_at.txt").write_text("100")
            (root / "video.mp4.tmp").write_bytes(b"partial")
            script = shell_functions("sync_should_interrupt_playback")
            script += f"\nSCRIPT_DIR={shlex.quote(str(ROOT))} ASSET_DIR={shlex.quote(d)}\n"
            script += r'''
SYNC_LIST="$ASSET_DIR/sync.txt" SYNC_AT_FILE="$ASSET_DIR/sync_at.txt"
date() { echo 100; }
if sync_should_interrupt_playback; then echo INTERRUPT; else echo KEEP-PLAYING; fi
mv "$ASSET_DIR/video.mp4.tmp" "$ASSET_DIR/video.mp4"
if sync_should_interrupt_playback; then echo READY; else echo WRONG; fi
'''
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                    capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(result.stdout, "KEEP-PLAYING\nREADY\n")


if __name__ == "__main__":
    unittest.main()

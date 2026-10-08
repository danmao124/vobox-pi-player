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
                    "start_asset_download_bg", "cache_asset",
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
batch_has_downloads_in_progress() { return 1; }
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


if __name__ == "__main__":
    unittest.main()

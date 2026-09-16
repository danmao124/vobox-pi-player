import hashlib
import io
import json
import sys
import tempfile
import subprocess
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playback_report as report

class PlaybackReportTests(unittest.TestCase):
    def test_confirmed_start_snapshot_and_finish(self):
        with tempfile.TemporaryDirectory() as d:
            history = Path(d) / "history.json"
            playlist = Path(d) / "main.txt"
            playlist.write_text("https://example.com/a.png\n")
            batch_hash = hashlib.sha256(playlist.read_bytes()).hexdigest()
            Path(str(playlist) + ".cursor").write_text(json.dumps({
                "playlistHash": batch_hash, "nextIndex": 20, "nextBlastIndex": 4}))
            sock = MagicMock()
            sock.__enter__.return_value = sock
            sock.makefile.return_value = io.StringIO(
                '{"error":"success"}\n{"event":"playback-restart"}\n'
                '{"event":"file-loaded"}\n{"event":"playback-restart"}\n')
            with patch.object(report.socket, "socket", return_value=sock), \
                    patch.object(report, "now_ms", return_value=100_000), \
                    patch.object(report.time, "monotonic_ns", return_value=123_456_789):
                started = report.load_and_record("socket", "asset.png", playlist, "1", history)
            self.assertEqual(started, 123_456_789)
            recorded = report.read_history(history)
            self.assertEqual(recorded[0]["startMs"], 100_000)
            self.assertTrue(recorded[0]["key"].endswith(":1"))
            self.assertEqual(recorded[0]["nextIndex"], 20)
            self.assertEqual(recorded[0]["nextBlastIndex"], 4)
            with patch.object(report, "now_ms", return_value=110_000):
                snapshot = report.snapshot(history)["playback"]
                self.assertEqual(snapshot["segments"][0]["endMs"], 110_000)
                self.assertNotIn("clockSynced", snapshot)
                self.assertNotIn("endMs", report.read_history(history)[0])
                report.finish(history)
                self.assertEqual(report.read_history(history)[0]["endMs"], 110_000)
            self.assertEqual(json.loads(sock.sendall.call_args.args[0])["command"], ["loadfile", "asset.png", "replace"])

    def test_cursor_is_bound_to_playlist_not_prefetch_index(self):
        with tempfile.TemporaryDirectory() as d:
            playlist = Path(d) / "main.txt"
            cursor_path = Path(str(playlist) + ".cursor")
            Path(d, "index.txt").write_text("90")
            cursor_path.write_text(json.dumps({"playlistHash": "correct", "nextIndex": 20, "nextBlastIndex": 4}))
            self.assertEqual(report.next_cursor(playlist, "correct"), {"nextIndex": 20, "nextBlastIndex": 4})
            self.assertEqual(report.next_cursor(playlist, "different"), {})
            cursor_path.write_text(json.dumps({"playlistHash": "correct", "nextIndex": -1, "nextBlastIndex": 4}))
            self.assertEqual(report.next_cursor(playlist, "correct"), {})
            cursor_path.unlink()
            self.assertEqual(report.next_cursor(playlist, "correct"), {})

    def test_failed_load_does_not_create_playback(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.makefile.return_value = io.StringIO('{"error":"failure"}\n')
        with patch.object(report.socket, "socket", return_value=sock):
            with self.assertRaises(RuntimeError):
                report.load_and_record("socket", "bad", "unused", 1, "unused")

    def test_history_and_snapshot_are_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            with patch.object(report, "now_ms", return_value=1_000_000):
                report.save_history(path, [{"key": "old", "startMs": 1, "endMs": 2}] +
                    [{"key": "recent", "startMs": 999_000, "endMs": 999_500}] * 300)
                self.assertEqual(len(report.read_history(path)), 256)
                self.assertEqual(len(report.snapshot(path)["playback"]["segments"]), 256)

class ImageDeadlineTests(unittest.TestCase):
    def test_elapsed_setup_and_polling_overhead_do_not_extend_duration(self):
        with tempfile.TemporaryDirectory() as d:
            # Playback started at 100s; setup has already consumed one second.
            clock = [101_000_000_000]
            sleeps = []

            def monotonic_ns():
                clock[0] += 15_000_000  # Simulate work between sleeps.
                return clock[0]

            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += round(seconds * 1_000_000_000) + 20_000_000

            with patch.object(report.time, "monotonic_ns", side_effect=monotonic_ns), \
                    patch.object(report.time, "sleep", side_effect=sleep), \
                    patch.object(report.time, "time_ns", side_effect=AssertionError("wall clock used for duration")):
                rc = report.wait_image("100000000000", "15", d + "/wizard", d + "/at", d + "/list")
            self.assertEqual(rc, 0)
            self.assertGreaterEqual(clock[0], 115_000_000_000)
            self.assertLessEqual(clock[0], 115_035_000_000)
            self.assertTrue(all(0 < seconds <= 0.2 for seconds in sleeps))
            self.assertLess(sleeps[-1], 0.2)

    def test_expired_deadline_does_not_start_another_full_wait(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(report.time, "monotonic_ns", return_value=120_000_000_000), \
                patch.object(report.time, "sleep") as sleep:
            self.assertEqual(report.wait_image("100000000000", "15", d + "/wizard", d + "/at", d + "/list"), 0)
            sleep.assert_not_called()

    def test_sync_cutover_requires_valid_timestamp_and_nonempty_playlist(self):
        cases = [("501", "asset.png", 2), ("499", "asset.png", 2),
                 ("501", "", 0), ("999", "asset.png", 0), ("bad", "asset.png", 0)]
        for at, playlist, expected in cases:
            with self.subTest(at=at, playlist=playlist), tempfile.TemporaryDirectory() as d:
                Path(d, "at").write_text(at)
                Path(d, "list").write_text(playlist)
                with patch.object(report.time, "time_ns", return_value=500_000_000_000), \
                        patch.object(report.time, "monotonic_ns", return_value=120_000_000_000), \
                        patch.object(report.time, "sleep") as sleep:
                    rc = report.wait_image("100000000000", "15", d + "/wizard", d + "/at", d + "/list")
                self.assertEqual(rc, expected)
                sleep.assert_not_called()

    def test_wifi_wizard_interrupts_a_running_image_wait(self):
        with tempfile.TemporaryDirectory() as d:
            def sleep(seconds):
                Path(d, "wizard").mkdir()

            with patch.object(report.time, "monotonic_ns", return_value=100_000_000_000), \
                    patch.object(report.time, "sleep", side_effect=sleep) as sleeper:
                rc = report.wait_image("100000000000", "15", d + "/wizard", d + "/at", d + "/list")
            self.assertEqual(rc, 0)
            sleeper.assert_called_once()


class ShellSyncTests(unittest.TestCase):
    def test_image_sync_interrupt_waits_for_scheduled_cutover(self):
        source = (Path(__file__).resolve().parents[1] / "tvads.sh").read_text()
        function = "play_url() {" + source.split("play_url() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        script = function + r"""
normalize_url() { echo "$1"; }
cache_asset() { echo asset.png; }
wait_while_wizard() { :; }
start_mpv_if_needed() { :; }
is_video() { return 1; }
mpv_send() { :; }
mpv_get_prop_data() { :; }
log() { :; }
python3() {
  case "$2" in
    load) echo 123456789 ;;
    wait-image)
      [[ "$3" == 123456789 && "$4" == 15 ]] || exit 99
      return 2 ;;
  esac
}
wait_out_sync_deadline() { echo waited-for-sync; }
SCRIPT_DIR=unused MPV_SOCK=unused MAIN_LIST=unused item_position=1 PLAYBACK_HISTORY=unused
IMAGE_SECONDS=15 WIZARD_LOCK=unused SYNC_AT_FILE=unused SYNC_LIST=unused
rc=0
play_url https://example.com/image.png || rc=$?
echo "result=$rc"
"""
        result = subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "waited-for-sync\nresult=2\n")

    def test_sync_requires_cached_assets_and_promotes_ready_batch(self):
        source = (Path(__file__).resolve().parents[1] / "tvads.sh").read_text()
        def function(name):
            return name + "() {" + source.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        functions = function("apply_sync_if_due") + function("clear_sync_pending") + function("promote_main_list")
        for ready in (False, True):
            with self.subTest(ready=ready), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                files = {name: root / name for name in (
                    "SYNC_AT_FILE", "SYNC_LIST", "SYNC_NEXT_FILE", "SYNC_BLAST_FILE",
                    "MAIN_LIST", "INDEX_FILE", "NEXT_BLAST_FILE", "PENDING_LIST")}
                files["SYNC_AT_FILE"].write_text("100")
                files["SYNC_LIST"].write_text("new.png\n")
                Path(str(files["SYNC_LIST"]) + ".cursor").write_text("new metadata")
                Path(str(files["MAIN_LIST"]) + ".cursor").write_text("old metadata")
                files["MAIN_LIST"].write_text("old.png\n")
                if ready:
                    (root / "new.png").write_text("cached")
                env = "\n".join(f"{name}={shlex.quote(str(path))}" for name, path in files.items())
                script = env + "\n" + functions + "\n" + r"""
log() { :; }
date() { echo 100; }
normalize_url() { echo "$1"; }
cache_path_for_url() { echo "ASSET_ROOT/$1"; }
bump_fetch_gen() { :; }
read_next_blast_index() { :; }
start_background_fetch_pending() { :; }
blast_idx=0
if apply_sync_if_due; then echo applied; else echo skipped; fi
""".replace("ASSET_ROOT", d)
                result = subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "applied" if ready else "skipped")
                self.assertEqual(files["MAIN_LIST"].read_text(), "new.png\n" if ready else "old.png\n")
                self.assertEqual(Path(str(files["MAIN_LIST"]) + ".cursor").read_text(),
                                 "new metadata" if ready else "old metadata")

if __name__ == "__main__":
    unittest.main()

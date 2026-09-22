import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def shell_function(name):
    source = (ROOT / "tvads.sh").read_text()
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"


class CacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets with spaces"
        self.assets.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.main = self.assets / "main.txt"
        self.pending = self.state / "pending.txt"
        self.sync = self.state / "sync.txt"
        self.main.write_text("")

    def asset(self, name, mib=1, age=100):
        path = self.assets / name
        # Allocate real blocks: sparse files should not count towards the limit.
        path.write_bytes(b"x" * int(mib * MIB))
        os.utime(path, (age, age))
        return path

    def cleanup_script(self, limit=10):
        values = {
            "ASSET_DIR": self.assets, "STATE_DIR": self.state, "MAX_CACHE_MB": limit,
            "MAIN_LIST": self.main, "PENDING_LIST": self.pending, "SYNC_LIST": self.sync,
        }
        script = "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())
        return script + "\n" + shell_function("cleanup_cache") + '\nlog() { echo "$*"; }\ncleanup_cache\necho player-continues\n'

    def cleanup(self, limit=10):
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", self.cleanup_script(limit)],
                                capture_output=True, text=True, check=True)
        self.assertIn("player-continues", result.stdout)
        return result

    def test_does_not_trim_between_target_and_limit(self):
        cached = self.asset("cached.jpg", mib=9.5)
        result = self.cleanup()
        self.assertTrue(cached.exists())
        self.assertNotIn("trimming", result.stdout)

    def test_trims_oldest_first_to_ninety_percent_then_stops(self):
        files = [self.asset(f"{index}.jpg", mib=2, age=100 + index) for index in range(6)]
        result = self.cleanup()
        self.assertEqual([file.exists() for file in files], [False, False, True, True, True, True])
        self.assertIn("trimming to 9MB", result.stdout)
        self.assertIn("removed 2 file(s)", result.stdout)
        self.assertTrue(self.main.exists())
        self.assertNotIn("trimming", self.cleanup().stdout)

    def test_protects_all_three_playlists_and_url_normalization(self):
        current = self.asset("current image.jpg", mib=2, age=1)
        pending = self.asset("pending%20image.png", mib=2, age=2)
        synced = self.asset("sync.mp4", mib=2, age=3)
        self.main.write_bytes(b"https://cdn.example/current image.jpg?token=123,  \r\n")
        self.pending.write_text("https://cdn.example/pending%20image.png?version=2\n")
        self.sync.write_text("https://cdn.example/sync.mp4")
        orphan = self.asset("old.jpg", mib=2, age=4)
        newer = self.asset("new.jpg", mib=2.5, age=5)
        self.cleanup()
        for path in (current, pending, synced, newer):
            self.assertTrue(path.exists(), str(path))
        self.assertFalse(orphan.exists())

    def test_preserves_metadata_partials_locks_and_marked_downloads(self):
        protected = [self.asset(name, age=index) for index, name in enumerate((
            "main.txt.cursor", "extra.txt", "history.json", "partial.jpg.tmp", "partial.jpg.lock",
            "locked.jpg", "locked.jpg.lock", "writing.jpg", "writing.jpg.tmp",
        ))]
        orphan = self.asset("orphan.jpg", mib=2)
        result = self.cleanup(limit=5)
        self.assertFalse(orphan.exists())
        self.assertTrue(all(path.exists() for path in protected))
        self.assertTrue(self.main.exists())
        self.assertIn("target not reached", result.stdout)

    def test_leaves_protected_assets_even_when_they_exceed_limit(self):
        current = self.asset("current.jpg", mib=11)
        self.main.write_text("https://cdn.example/current.jpg\n")
        result = self.cleanup()
        self.assertTrue(current.exists())
        self.assertIn("removed 0 file(s)", result.stdout)
        self.assertIn("target not reached", result.stdout)

    def test_missing_or_unreadable_main_playlist_stops_deletion(self):
        cached = self.asset("keep.jpg", mib=11)
        self.main.unlink()
        result = self.cleanup()
        self.assertTrue(cached.exists())
        self.assertIn("cache cleanup stopped", result.stdout)
        self.main.mkdir()  # Reading a directory fails even when tests run as root.
        result = self.cleanup()
        self.assertTrue(cached.exists())
        self.assertIn("cache cleanup stopped", result.stdout)

    def test_unreadable_staged_playlist_stops_deletion(self):
        cached = self.asset("keep.jpg", mib=11)
        self.sync.mkdir()
        result = self.cleanup()
        self.assertTrue(cached.exists())
        self.assertIn("cache cleanup stopped", result.stdout)

    def test_cleanup_waits_for_playlist_publication_and_protects_new_sync(self):
        cached = self.asset("new-sync.jpg", mib=11)
        staged = self.state / "sync.txt.tmp"
        staged.write_text("https://cdn.example/new-sync.jpg\n")
        release = self.state / "release"
        publish = (
            "import os, pathlib, sys, time\n"
            "print('writer-locked', flush=True)\n"
            "deadline = time.monotonic() + 5\n"
            f"while not pathlib.Path({str(release)!r}).exists():\n"
            "    if time.monotonic() > deadline: sys.exit(1)\n"
            "    time.sleep(0.01)\n"
            f"os.replace({str(staged)!r}, {str(self.sync)!r})\n"
        )
        script = f"STATE_DIR={shlex.quote(str(self.state))}\n" + shell_function("with_cache_lock")
        script += "\nwith_cache_lock python3 -c " + shlex.quote(publish)
        with subprocess.Popen(["bash", "-eu", "-c", script], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as writer:
            self.assertEqual(writer.stdout.readline().strip(), "writer-locked")
            with subprocess.Popen(["bash", "-euo", "pipefail", "-c", self.cleanup_script()],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as cleaner:
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        cleaner.communicate(timeout=0.2)
                finally:
                    release.touch()
                stdout, stderr = cleaner.communicate(timeout=5)
                self.assertEqual(cleaner.returncode, 0, stderr)
                self.assertIn("player-continues", stdout)
            _, stderr = writer.communicate(timeout=5)
            self.assertEqual(writer.returncode, 0, stderr)
        self.assertTrue(cached.exists())
        self.assertTrue(self.sync.exists())

    def test_handles_unusual_filenames_without_touching_links_or_subdirectories(self):
        old = self.asset("old\nimage.jpg", mib=6)
        new = self.asset("new image.jpg", mib=6, age=200)
        outside = self.root / "outside.jpg"
        outside.write_text("outside")
        link = self.assets / "linked.jpg"
        link.symlink_to(outside)
        subdir = self.assets / "subdirectory"
        subdir.mkdir()
        nested = subdir / "metadata"
        nested.write_text("keep")
        self.cleanup()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(), "outside")
        self.assertEqual(nested.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()

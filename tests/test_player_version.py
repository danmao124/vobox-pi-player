import json
import re
import subprocess
import unittest
from pathlib import Path


class PlayerVersionTests(unittest.TestCase):
    def heartbeat(self, snapshot_command):
        source = (Path(__file__).resolve().parents[1] / "tvads.sh").read_text()
        version_line = re.search(r'^readonly PLAYER_VERSION="[^"]+"$', source, re.M).group()
        body_function = "build_event_body() {" + source.split("build_event_body() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        script = version_line + "\n" + body_function + "\n" + (
            "SCRIPT_DIR=unused PLAYBACK_HISTORY=unused\n"
            "python3() { " + snapshot_command + "; }\n"
            "build_event_body\n"
        )
        result = subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True, check=True)
        return json.loads(result.stdout), version_line.split('"')[1]

    def test_heartbeat_contains_running_version_and_playback(self):
        body, version = self.heartbeat("echo '{\"playback\":{\"sampledAtMs\":123,\"segments\":[]}}'")
        self.assertEqual(body, {"playerVersion": version, "playback": {"sampledAtMs": 123, "segments": []}})

    def test_version_is_reported_when_playback_helper_fails(self):
        body, version = self.heartbeat("return 1")
        self.assertEqual(body, {"playerVersion": version})


if __name__ == "__main__":
    unittest.main()

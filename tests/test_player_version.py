import json
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class PlayerVersionTests(unittest.TestCase):
    def heartbeat(self, snapshot_command, *, ad_station_id="OAKS-CARDCLUB", web_station=""):
        source = (Path(__file__).resolve().parents[1] / "tvads.sh").read_text()
        version_line = re.search(r'^readonly PLAYER_VERSION="[^"]+"$', source, re.M).group()
        body_function = "build_event_body() {" + source.split("build_event_body() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        script = version_line + "\n" + body_function + "\n" + (
            f"SCRIPT_DIR=unused PLAYBACK_HISTORY=unused PLAYBACK_SESSION=test-session ID={ad_station_id!r} WEB_STATION={web_station!r}\n"
            "python3() { " + snapshot_command + "; }\n"
            "build_event_body\n"
        )
        result = subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True, check=True)
        return json.loads(result.stdout), version_line.split('"')[1]

    def test_heartbeat_contains_running_version_and_playback(self):
        body, version = self.heartbeat("echo '{\"playback\":{\"sampledAtMs\":123,\"segments\":[]}}'")
        self.assertEqual(
            body,
            {
                "playerVersion": version,
                "adStationId": "OAKS-CARDCLUB",
                "webStationId": "",
                "playbackMode": "mpv",
                "playbackSession": "test-session",
                "playback": {"sampledAtMs": 123, "segments": []},
            },
        )

    def test_heartbeat_includes_web_station_when_configured(self):
        body, version = self.heartbeat(
            "echo '{}'",
            web_station="  bay101-poker-1  ",
        )
        self.assertEqual(
            body,
            {
                "playerVersion": version,
                "adStationId": "OAKS-CARDCLUB",
                "webStationId": "BAY101-POKER-1",
                "playbackMode": "mpv",
                "playbackSession": "test-session",
            },
        )

    def test_version_is_reported_when_playback_helper_fails(self):
        body, version = self.heartbeat("return 1")
        self.assertEqual(body, {
            "playerVersion": version, "adStationId": "OAKS-CARDCLUB", "webStationId": "",
            "playbackMode": "mpv",
            "playbackSession": "test-session",
        })

    def test_whitespace_only_web_station_sends_explicit_empty_value(self):
        body, _ = self.heartbeat("echo '{}'", web_station="   ")
        self.assertEqual(body["webStationId"], "")


class BillboardRequestTests(unittest.TestCase):
    def test_web_station_query_is_sent_even_when_empty(self):
        source = (Path(__file__).resolve().parents[1] / "tvads.sh").read_text()
        function = "fetch_batch_to() {" + source.split("fetch_batch_to() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        for web_station in ("", "BAY101-POKER-1"):
            with self.subTest(web_station=web_station), tempfile.TemporaryDirectory() as d:
                request = Path(d) / "request-args"
                script = function + f"\nWEB_STATION={shlex.quote(web_station)}\nREQUEST={shlex.quote(str(request))}\nSCRIPT_DIR={shlex.quote(str(Path(__file__).resolve().parents[1]))}\n"
                script += r'''
API_BASE=https://example.com/api VIEW_PATH=view/billboard ID=OAKS-CARDCLUB
CURL_API_OPTS=(--fail)
log() { :; }
build_curl_auth_headers() { curl_headers=(-H test-auth); }
internet_ping_ok() { return 0; }
curl() {
  printf '%s\n' "$@" > "$REQUEST"
  return 22
}
fetch_batch_to 3 5 unused unused unused || true
'''
                subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                               capture_output=True, text=True, check=True)
                url = request.read_text().splitlines()[-1]
                query = parse_qs(urlsplit(url).query, keep_blank_values=True)
                self.assertEqual(query["webStationId"], [web_station])
                self.assertEqual(query["id"], ["OAKS-CARDCLUB"])
                self.assertEqual(query["index"], ["3"])
                self.assertEqual(query["blastIndex"], ["5"])


if __name__ == "__main__":
    unittest.main()

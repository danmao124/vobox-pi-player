import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def shell_function(name):
    source = (ROOT / "tvads.sh").read_text()
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"


# Fake the privileged dispatcher, with a separate process/session standing in for
# systemd. Never call host sudo/systemd or inherit the receiver's output pipes.
MOCK_SUDO = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["REBOOT_TEST_ROOT"])
args = sys.argv[1:]
assert args[:2] == ["-n", "systemd-run"], args
with (root / "dispatch.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
failure = int(os.environ.get("DISPATCH_RC", "0"))
if failure:
    sys.exit(failure)
command = args[args.index("/bin/bash"):]
assert len(command) == 3 and command[:2] == ["/bin/bash", "-c"], command
(root / "worker-command.json").write_text(json.dumps(command))
with (root / "worker.log").open("a") as log:
    subprocess.Popen([sys.executable, str(root / "worker.py")],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True)
'''

MOCK_WORKER = r'''
import json
import os
from pathlib import Path
import subprocess

root = Path(os.environ["REBOOT_TEST_ROOT"])
command = json.loads((root / "worker-command.json").read_text())
result = subprocess.run(command, cwd=root, timeout=5)
(root / "worker-status").write_text(str(result.returncode))
'''

MOCK_HELPER = r'''
import os
from pathlib import Path
import signal
import sys

root = Path(os.environ["REBOOT_TEST_ROOT"])
name = Path(sys.argv[0]).name
with (root / "helpers.log").open("a") as stream:
    stream.write(name + "\n")
if name == "tvstop" and os.environ.get("KILL_RECEIVER") == "1":
    # Only the test-created receiver process group; the worker lives elsewhere.
    receiver = int((root / "receiver-pid").read_text())
    assert receiver != os.getpgrp()
    os.killpg(receiver, signal.SIGTERM)
sys.exit(int(os.environ.get("STOP_RC" if name == "tvstop" else "START_RC", "0")))
'''


class RebootPlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # A closed PATH ensures even missing-helper tests cannot find real tools.
        for name in ("python3", "jq", "cat", "sleep", "touch"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.executable("sudo", MOCK_SUDO)
        self.executable("tvstop", MOCK_HELPER)
        self.executable("tvstart", MOCK_HELPER)
        self.executable("systemd-run", "raise AssertionError('must go through fake sudo')\n")
        (self.root / "worker.py").write_text(MOCK_WORKER)
        self.env = {**os.environ, "PATH": str(self.bin), "REBOOT_TEST_ROOT": str(self.root)}

    def executable(self, name, code):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o755)

    def lines(self, name):
        path = self.root / name
        return path.read_text().splitlines() if path.exists() else []

    def dispatches(self):
        return [json.loads(line) for line in self.lines("dispatch.jsonl")]

    def poll(self, commands=None, *, success=True, raw=None, curl_rc=0, kill_receiver=False):
        response = raw if raw is not None else json.dumps({"response": {
            "success": success, "message": "Acknowledgment Received", "data": commands,
        }})
        (self.root / "response.json").write_text(response)
        script = shell_function("reboot_player") + shell_function("ask_for_event")
        script += f"\nSCRIPT_DIR={shlex.quote(str(ROOT))}\nCURL_RC={curl_rc}\n"
        script += r'''
API_BASE=https://example.test/api ASK_FOR_EVENT_PATH=device/askforevent ID=STATION
CURL_API_OPTS=(--fail) LAST_SYNC_COMMAND=""
log() { echo "$*"; }
build_event_body() { echo '{}'; }
build_curl_auth_headers() { curl_headers=(-H test-auth); }
curl() { cat "$REBOOT_TEST_ROOT/response.json"; return "$CURL_RC"; }
internet_ping_ok() { return 0; }
note_fetch_reach_ok() { :; }
update_playback_mode() { :; }
arm_sync_command() { echo SYNC >> "$REBOOT_TEST_ROOT/helpers.log"; }
set_wifi_profile() { echo WIFI >> "$REBOOT_TEST_ROOT/helpers.log"; }
printf '%s' "$$" > "$REBOOT_TEST_ROOT/receiver-pid"
ask_for_event || true
echo poll-continues
'''
        if kill_receiver:
            script += "sleep 5\n"
        result = subprocess.run(
            ["/bin/bash", "-euo", "pipefail", "-c", script], cwd=self.root,
            env={**self.env, "KILL_RECEIVER": "1" if kill_receiver else "0"},
            start_new_session=True, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, -signal.SIGTERM if kill_receiver else 0,
                         result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        if not kill_receiver:
            self.assertIn("poll-continues", result.stdout)
        return result.stdout

    def worker_status(self):
        path = self.root / "worker-status"
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if path.exists() and path.read_text():
                return int(path.read_text())
            time.sleep(0.01)
        self.fail("Mock worker did not finish: " + "\n".join(self.lines("worker.log")))

    def test_dispatches_detached_fixed_service_and_runs_stop_before_start(self):
        output = self.poll([{"type": "REBOOT_PLAYER", "data": {}}])
        self.assertEqual(self.worker_status(), 0)
        self.assertEqual(self.lines("helpers.log"), ["tvstop", "tvstart"])
        call, = self.dispatches()
        self.assertEqual(call[:5], ["-n", "systemd-run", "--unit=vobox-reboot-player",
                                    "--collect", "--property=Type=exec"])
        self.assertEqual(call[5:7], ["/bin/bash", "-c"])
        for forbidden in ("--scope", "--pipe", "--wait", "--no-block"):
            self.assertNotIn(forbidden, call)
        self.assertNotIn("ignoring type=REBOOT_PLAYER", output)

    def test_failed_stop_never_starts_and_failure_is_preserved(self):
        self.env["STOP_RC"] = "9"
        self.poll([{"type": "REBOOT_PLAYER"}])
        self.assertEqual(self.worker_status(), 9)
        self.assertEqual(self.lines("helpers.log"), ["tvstop"])

    def test_failed_start_preserves_worker_failure(self):
        self.env["START_RC"] = "12"
        self.poll([{"type": "REBOOT_PLAYER"}])
        self.assertEqual(self.worker_status(), 12)
        self.assertEqual(self.lines("helpers.log"), ["tvstop", "tvstart"])

    def test_worker_survives_receiver_process_group_termination(self):
        self.poll([{"type": "REBOOT_PLAYER"}], kill_receiver=True)
        self.assertEqual(self.worker_status(), 0)
        self.assertEqual(self.lines("helpers.log"), ["tvstop", "tvstart"])

    def test_coalesces_reboots_after_sync_wifi_and_unknown_commands(self):
        output = self.poll([
            {"type": "REBOOT_PLAYER"},
            {"type": "sync", "data": {"index": 1, "timestamp": 123, "blastIndex": 0}},
            {"type": "setWifi", "data": {"ssid": "Guest", "password": "password"}},
            {"type": "futureCommand"}, {"type": "REBOOT_PLAYER", "data": {}},
        ])
        self.assertEqual(self.worker_status(), 0)
        self.assertEqual(len(self.dispatches()), 1)
        self.assertEqual(self.lines("helpers.log"), ["SYNC", "WIFI", "tvstop", "tvstart"])
        self.assertLess(output.index("ignoring type=futureCommand"), output.index("handing tvstop"))

    def test_ignores_all_command_payload_fields_and_ids(self):
        attack = "$(touch injected); `touch injected`; tvstart"
        output = self.poll([{"_id": attack, "type": "REBOOT_PLAYER", "data": {
            "command": attack, "script": attack, "service": attack,
            "tvstop": attack, "tvstart": attack, "args": [attack],
        }}])
        self.assertEqual(self.worker_status(), 0)
        self.assertNotIn(attack, json.dumps(self.dispatches()))
        self.assertNotIn(attack, output)
        self.assertFalse((self.root / "injected").exists())
        self.assertEqual(self.lines("helpers.log"), ["tvstop", "tvstart"])

    def test_requires_exact_type_and_successful_array_response(self):
        for commands, success in (
            ([], True), (None, True), ([{"type": "reboot_player"}], True),
            ([{"type": "reboot"}], True), ([{"type": "REBOOT_PLAYER "}], True),
            ([{"data": {"type": "REBOOT_PLAYER"}}], True),
            ([{"type": "REBOOT_PLAYER"}], False),
            ([{"type": "REBOOT_PLAYER"}], "true"),
            ({"command": {"type": "REBOOT_PLAYER"}}, True),
        ):
            with self.subTest(commands=commands, success=success):
                self.poll(commands, success=success)
        for raw in ("not json", "{}", '{"response":{"success":true,"data":'):
            with self.subTest(raw=raw):
                self.poll(raw=raw)
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.lines("helpers.log"), [])

    def test_http_failure_does_not_dispatch_even_with_a_command_body(self):
        self.poll([{"type": "REBOOT_PLAYER"}], curl_rc=22)
        self.assertEqual(self.dispatches(), [])

    def test_dispatch_failure_keeps_polling_without_running_helpers(self):
        self.env["DISPATCH_RC"] = "1"
        output = self.poll([{"type": "REBOOT_PLAYER"}])
        self.assertIn("could not launch restart worker (exit 1)", output)
        self.assertEqual(len(self.dispatches()), 1)
        self.assertEqual(self.lines("helpers.log"), [])
        self.assertFalse((self.root / "worker-status").exists())

    def test_missing_systemd_run_keeps_polling_without_in_process_fallback(self):
        (self.bin / "systemd-run").unlink()
        output = self.poll([{"type": "REBOOT_PLAYER"}])
        self.assertIn("requires systemd-run; player left running", output)
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.lines("helpers.log"), [])

    def test_missing_start_helper_is_detected_before_stop(self):
        (self.bin / "tvstart").unlink()
        self.poll([{"type": "REBOOT_PLAYER"}])
        self.assertEqual(self.worker_status(), 127)
        self.assertEqual(self.lines("helpers.log"), [])
        self.assertIn("tvstop and tvstart must be installed", "\n".join(self.lines("worker.log")))

    def test_missing_stop_helper_never_starts(self):
        (self.bin / "tvstop").unlink()
        self.poll([{"type": "REBOOT_PLAYER"}])
        self.assertEqual(self.worker_status(), 127)
        self.assertEqual(self.lines("helpers.log"), [])


if __name__ == "__main__":
    unittest.main()

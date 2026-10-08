import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def shell_function(name):
    source = (ROOT / "tvads.sh").read_text()
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def wifi_command(ssid="VenueGuest", password="secret-wifi-pass"):
    return {"_id": "66f1a2b3c4d5e6f7a8b9c0d1", "type": "setWifi",
            "data": {"ssid": ssid, "password": password}}


# Stand in for sudo/nmcli so tests cannot touch the host's network configuration.
# Persist profiles between calls, reject unexpected operations, and emit sensitive
# output to verify that the player does not forward nmcli diagnostics to its log.
MOCK_SUDO = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["WIFI_TEST_ROOT"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
assert args[:5] == ["-n", "nmcli", "--wait", "10", "connection"], args
operation = args[5]
profiles_file = root / "profiles.json"
profiles = json.loads(profiles_file.read_text())
failures = json.loads((root / "failures.json").read_text())
if operation == "show":
    assert args[6] == "id" and len(args) == 8, args
    name = args[7]
    sys.exit(failures.get("show", {}).get(name, 0 if name in profiles else 10))
if operation == "add":
    assert args[6:13] == ["save", "yes", "type", "wifi", "ifname", "*", "con-name"], args
    name = args[13]
    assert name not in profiles, "duplicate profile"
    settings = args[14:]
elif operation == "modify":
    assert args[6] == "id", args
    name = args[7]
    assert name in profiles, "missing profile"
    settings = args[8:]
else:
    raise AssertionError("Unexpected network operation: " + operation)
assert len(settings) % 2 == 0, settings
properties = dict(zip(settings[::2], settings[1::2]))
password = properties["802-11-wireless-security.psk"]
print("nmcli output: " + password)
print("nmcli error: " + password, file=sys.stderr)
failure = failures.get(operation, {}).get(name, 0)
if failure:
    sys.exit(failure)
profiles.setdefault(name, {}).update(properties)
profiles_file.write_text(json.dumps(profiles))
'''


class SetWifiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        sudo = self.bin / "sudo"
        sudo.write_text(MOCK_SUDO)
        sudo.chmod(0o755)
        self.write_json("profiles.json", {})
        self.write_json("failures.json", {})

    def write_json(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def profiles(self):
        return json.loads((self.root / "profiles.json").read_text())

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def poll(self, commands, *, success=True):
        self.write_json("response.json", {"response": {
            "success": success, "message": "Acknowledgment Received", "data": commands,
        }})
        script = shell_function("set_wifi_profile") + shell_function("ask_for_event")
        script += f"\nWIFI_TEST_ROOT={shlex.quote(str(self.root))}\n"
        script += f"SCRIPT_DIR={shlex.quote(str(ROOT))}\n"
        script += r'''
API_BASE=https://example.com/api ASK_FOR_EVENT_PATH=device/askforevent
LAST_SYNC_COMMAND=""
CURL_API_OPTS=(--fail)
log() { echo "$*"; }
build_event_body() { echo '{}'; }
build_curl_auth_headers() { curl_headers=(-H test-auth); }
curl() { cat "$WIFI_TEST_ROOT/response.json"; }
note_fetch_reach_ok() { :; }
update_playback_mode() { :; }
arm_sync_command() { printf 'SYNC %s %s %s\n' "$1" "$2" "$3"; }
ask_for_event
echo poll-continues
'''
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script], cwd=self.root,
            env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                 "WIFI_TEST_ROOT": str(self.root)},
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertIn("poll-continues", result.stdout)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_saves_sample_as_persistent_autoconnect_profile_without_activating_it(self):
        output = self.poll([wifi_command()])
        self.assertEqual(self.profiles(), {"vobox-wifi-VenueGuest": {
            "connection.autoconnect": "yes",
            "connection.autoconnect-retries": "0",
            "802-11-wireless.ssid": "VenueGuest",
            "802-11-wireless.mode": "infrastructure",
            "802-11-wireless-security.key-mgmt": "wpa-psk",
            "802-11-wireless-security.psk": "secret-wifi-pass",
            "802-11-wireless-security.psk-flags": "0",
        }})
        self.assertEqual([call[5] for call in self.calls()], ["show", "add"])
        self.assertIn("saved Wi-Fi profile", output)
        self.assertNotIn("secret-wifi-pass", output)
        self.assertNotIn("ignoring type=setWifi", output)

    def test_repeated_ssid_updates_password_without_duplicate_profiles(self):
        self.poll([wifi_command(), wifi_command(password="updated-password")])
        # An older profile's disabled autoconnect/finite retries are updated too.
        profiles = self.profiles()
        profiles["vobox-wifi-VenueGuest"].update({
            "connection.autoconnect": "no", "connection.autoconnect-retries": "4",
        })
        self.write_json("profiles.json", profiles)
        # A later poll (or player restart) must find the same saved profile.
        self.poll([wifi_command(password="latest-password")])
        self.assertEqual(len(self.profiles()), 1)
        self.assertEqual(self.profiles()["vobox-wifi-VenueGuest"]["802-11-wireless-security.psk"],
                         "latest-password")
        self.assertEqual(self.profiles()["vobox-wifi-VenueGuest"]["connection.autoconnect"], "yes")
        self.assertEqual(self.profiles()["vobox-wifi-VenueGuest"]["connection.autoconnect-retries"], "0")
        self.assertEqual([call[5] for call in self.calls()],
                         ["show", "add", "show", "modify", "show", "modify"])

    def test_mixed_queue_preserves_sync_and_processes_all_wifi_commands(self):
        output = self.poll([
            wifi_command(), {"type": "sync", "data": {"index": 3, "timestamp": 123, "blastIndex": 5}},
            wifi_command("Second Network"), {"type": "futureCommand", "data": {}},
        ])
        self.assertEqual(len(self.profiles()), 2)
        self.assertIn("SYNC 3 123 5", output)
        self.assertIn("ignoring type=futureCommand", output)

    def test_credentials_are_passed_literally_including_ssid_trailing_newline(self):
        ssid = ' Cafe\t"\\$(touch injected)\n'
        password = "  p'ass\\\";$(touch injected) `id`  "
        output = self.poll([wifi_command(ssid, password)])
        profile = self.profiles()["vobox-wifi-" + ssid]
        self.assertEqual(profile["802-11-wireless.ssid"], ssid)
        self.assertEqual(profile["802-11-wireless-security.psk"], password)
        self.assertFalse((self.root / "injected").exists())
        self.assertNotIn(password, output)

    def test_accepts_32_byte_unicode_ssid_and_64_digit_hex_psk(self):
        ssid, password = "é" * 16, "aB09" * 16
        self.poll([wifi_command(ssid, password)])
        self.assertEqual(self.profiles()["vobox-wifi-" + ssid]["802-11-wireless-security.psk"], password)

    def test_invalid_credentials_are_rejected_without_blocking_later_commands(self):
        invalid_data = [
            None, [], "credentials", {}, {"ssid": "VenueGuest"}, {"password": "secret-wifi-pass"},
            {"ssid": 42, "password": "secret-wifi-pass"}, {"ssid": "VenueGuest", "password": False},
            *({"ssid": ssid, "password": "secret-wifi-pass"}
              for ssid in ("", "a" * 33, "é" * 17, "null\x00byte")),
            *({"ssid": "VenueGuest", "password": password}
              for password in ("", "short", "a" * 65, "z" * 64, "pass\x00word", "password\n")),
        ]
        commands = [{"type": "setWifi", "data": data} for data in invalid_data]
        output = self.poll([*commands, wifi_command()])
        self.assertEqual(output.count("WARN: setWifi requires"), len(invalid_data))
        self.assertEqual([call[5] for call in self.calls()], ["show", "add"])
        self.assertEqual(len(self.profiles()), 1)

    def test_lookup_failure_does_not_attempt_to_add_or_stop_the_queue(self):
        for code in (1, 8, 127):
            with self.subTest(exit_code=code):
                self.write_json("failures.json", {"show": {"vobox-wifi-Broken": code}})
                output = self.poll([wifi_command("Broken"), wifi_command()])
                self.assertIn(f"could not read Wi-Fi profiles (exit {code})", output)
                self.assertNotIn("vobox-wifi-Broken", self.profiles())
                self.assertIn("vobox-wifi-VenueGuest", self.profiles())
        self.assertFalse(any(call[5] == "add" and call[13] == "vobox-wifi-Broken"
                             for call in self.calls()))

    def test_save_failure_is_redacted_and_does_not_stop_the_queue(self):
        for operation in ("add", "modify"):
            with self.subTest(operation=operation):
                self.write_json("profiles.json", {"vobox-wifi-Broken": {}} if operation == "modify" else {})
                self.write_json("failures.json", {operation: {"vobox-wifi-Broken": 1}})
                output = self.poll([wifi_command("Broken", "do-not-log-this"), wifi_command()])
                self.assertIn("could not save Wi-Fi profile (exit 1)", output)
                self.assertNotIn("do-not-log-this", output)
                self.assertIn("vobox-wifi-VenueGuest", self.profiles())

    def test_empty_or_unsuccessful_response_does_not_touch_network_configuration(self):
        for commands, success in (([], True), (None, True), ([wifi_command()], False)):
            with self.subTest(commands=commands, success=success):
                self.poll(commands, success=success)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()

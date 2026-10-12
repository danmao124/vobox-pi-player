import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import display_mode as display


BOOT = "console=tty1 root=PARTUUID=test video=HDMI-A-1:1536x768M@60"
BEFORE = """Screen 0: minimum 320 x 200, current 3840 x 2160, maximum 16384 x 16384
HDMI-1 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis)
   3840x2160     60.00*+
   1536x768     59.95
HDMI-2 disconnected (normal left inverted right x axis y axis)
"""
AFTER = BEFORE.replace("current 3840 x 2160", "current 1536 x 768").replace(
    "primary 3840x2160+0+0", "primary 1536x768+0+0").replace("60.00*+", "60.00+").replace(
    "59.95", "59.95*")
CVT = '# 1536x768 59.95 Hz\nModeline "1536x768_60.00" 95.75 1536 1616 1768 2000 768 771 781 798 -hsync +vsync\n'


class DisplayModeTests(unittest.TestCase):
    def apply(self, cmdline=BOOT, states=(BEFORE, AFTER), cvt=CVT, fail=None):
        self.calls = []
        snapshots = iter(states)

        def run(*args):
            self.calls.append(args)
            if fail and fail(args):
                raise display.DisplayModeError("mock display command failed")
            if args == ("xrandr", "--current"):
                return next(snapshots)
            if args[0] == "cvt":
                return cvt
            return ""

        with patch.object(display, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()) as output:
            display.apply_modes(cmdline)
        return output.getvalue()

    def test_reads_exact_venue_mode_and_ignores_unrelated_boot_arguments(self):
        mode, = display.requested_modes(BOOT)
        self.assertEqual((mode.connector, mode.size, mode.rate, mode.cvt),
                         ("HDMI-A-1", "1536x768", 60, True))

    def test_no_explicit_resolution_does_not_change_x(self):
        for cmdline in ("console=tty1", "video=HDMI-A-1:D", "video=HDMI-A-1:d", "video=HDMI-A-1:1920x1080@60d"):
            with self.subTest(cmdline=cmdline):
                self.apply(cmdline)
                self.assertEqual(self.calls, [])

    def test_last_setting_for_a_connector_wins(self):
        modes = display.requested_modes(BOOT + " video=HDMI-A-1:1920x1080@30")
        self.assertEqual([(mode.size, mode.rate) for mode in modes], [("1920x1080", 30)])

    def test_supports_bpp_reduced_blanking_and_keeps_console_rotation_separate(self):
        mode, = display.requested_modes("video=HDMI-A-1:1536x768MR-24@60D,rotate=90")
        self.assertTrue(mode.cvt and mode.reduced)
        self.assertEqual((mode.width, mode.height, mode.rate), (1536, 768, 60))

    def test_bad_numeric_mode_is_not_silently_ignored(self):
        for value in ("1536x0M@60", "0x768", "1536x768M@0", "1536x768@oops", "1536x768;touch"):
            with self.subTest(value=value), self.assertRaises(display.DisplayModeError):
                self.apply("video=HDMI-A-1:" + value)

    def test_applies_boot_mode_to_matching_x_output_before_verifying(self):
        output = self.apply()
        self.assertEqual(self.calls, [
            ("xrandr", "--current"),
            ("xrandr", "--output", "HDMI-1", "--mode", "1536x768", "--rate", "60"),
            ("xrandr", "--current"),
        ])
        self.assertIn("Applied boot display mode: HDMI-1 1536x768 at 60 Hz", output)

    def test_exact_connector_name_and_second_hdmi_are_supported(self):
        for name, kernel in (("HDMI-A-1", "HDMI-A-1"), ("HDMI-2", "HDMI-A-2"), ("DP-1", "DP-1")):
            with self.subTest(name=name):
                before = BEFORE.replace("HDMI-2 disconnected", "UNUSED disconnected").replace("HDMI-1", name)
                after = AFTER.replace("HDMI-2 disconnected", "UNUSED disconnected").replace("HDMI-1", name)
                self.apply(BOOT.replace("HDMI-A-1", kernel), states=(before, after))
                self.assertEqual(self.calls[1][2], name)

    def test_disconnected_configured_output_does_not_reconfigure_another_screen(self):
        output = self.apply(BOOT.replace("HDMI-A-1", "HDMI-A-2"), states=(BEFORE,))
        self.assertEqual(self.calls, [("xrandr", "--current")])
        self.assertIn("disconnected; skipping", output)

    def test_unknown_connector_does_not_fall_back_to_first_connected_output(self):
        with self.assertRaisesRegex(display.DisplayModeError, "Cannot map configured connector"):
            self.apply(BOOT.replace("HDMI-A-1", "HDMI-A-3"))

    def test_global_mode_requires_one_unambiguous_output(self):
        self.apply("video=1536x768M@60")
        two_screens = BEFORE.replace("HDMI-2 disconnected", "HDMI-2 connected")
        with self.assertRaisesRegex(display.DisplayModeError, "exactly one connected"):
            self.apply("video=1536x768M@60", states=(two_screens,))

    def test_refresh_rate_selects_the_requested_rate_not_preferred_4k_or_30hz(self):
        before = BEFORE.replace("1536x768     59.95", "1536x768     30.00 59.95")
        self.apply(states=(before, AFTER))
        self.assertEqual(self.calls[1][-2:], ("--rate", "60"))
        self.assertNotIn("3840x2160", self.calls[1])

    def test_accepts_standard_custom_mode_names_and_nearest_fractional_refresh(self):
        self.apply(states=(BEFORE.replace("1536x768", "1536x768_60.00"),
                           AFTER.replace("   1536x768", "   1536x768_60.00")))
        self.assertEqual(self.calls[1][4], "1536x768_60.00")

    def test_missing_custom_cvt_mode_is_created_and_attached_to_configured_output(self):
        before = BEFORE.replace("   1536x768     59.95\n", "")
        after = AFTER.replace("   1536x768", "   venditt-1536x768-60")
        self.apply(states=(before, after))
        self.assertEqual(self.calls[1], ("cvt", "1536", "768", "60"))
        self.assertEqual(self.calls[2][:3], ("xrandr", "--newmode", "venditt-1536x768-60"))
        self.assertEqual(self.calls[3], ("xrandr", "--addmode", "HDMI-1", "venditt-1536x768-60"))
        self.assertEqual(self.calls[4], ("xrandr", "--output", "HDMI-1", "--mode", "venditt-1536x768-60", "--rate", "60"))

    def test_reduced_blanking_is_passed_to_cvt(self):
        before = BEFORE.replace("   1536x768     59.95\n", "")
        after = AFTER.replace("   1536x768", "   venditt-1536x768-60-rb")
        self.apply(BOOT.replace("768M", "768MR"), states=(before, after))
        self.assertEqual(self.calls[1], ("cvt", "-r", "1536", "768", "60"))

    def test_reuses_a_generated_mode_instead_of_creating_it_twice(self):
        state = AFTER.replace("   1536x768", "   venditt-1536x768-60")
        self.apply(states=(state, state))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[1][4], "venditt-1536x768-60")

    def test_multiple_connector_overrides_apply_to_their_own_outputs(self):
        other_before = "HDMI-2 connected 1920x1080+3840+0\n   1920x1080 60.00*\n   1280x720 60.00\n"
        other_after = "HDMI-2 connected 1280x720+3840+0\n   1920x1080 60.00\n   1280x720 60.00*\n"
        before = BEFORE.split("HDMI-2 disconnected")[0] + other_before
        first = AFTER.split("HDMI-2 disconnected")[0] + other_before
        second = AFTER.split("HDMI-2 disconnected")[0] + other_after
        self.apply(BOOT + " video=HDMI-A-2:1280x720@60", states=(before, first, second))
        self.assertEqual(self.calls[1][2:5], ("HDMI-1", "--mode", "1536x768"))
        self.assertEqual(self.calls[3][2:5], ("HDMI-2", "--mode", "1280x720"))

    def test_generated_mode_can_be_attached_to_second_output_without_duplicate_creation(self):
        before = BEFORE.replace("   1536x768     59.95\n", "").split("HDMI-2 disconnected")[0]
        other = "HDMI-2 connected 3840x2160+0+0\n   3840x2160 60.00*\n"
        first = AFTER.replace("   1536x768", "   venditt-1536x768-60").split("HDMI-2 disconnected")[0]
        second = first + "HDMI-2 connected 1536x768+0+0\n   venditt-1536x768-60 59.95*\n"
        self.apply(BOOT + " video=HDMI-A-2:1536x768M@60", states=(before + other, first + other, second))
        self.assertEqual(sum("--newmode" in args for args in self.calls), 1)
        self.assertIn(("xrandr", "--addmode", "HDMI-2", "venditt-1536x768-60"), self.calls)

    def test_unavailable_non_cvt_mode_does_not_guess_new_timings(self):
        with self.assertRaisesRegex(display.DisplayModeError, "unavailable"):
            self.apply(BOOT.replace("768M", "768"), states=(BEFORE.replace("59.95", "30.00"),))
        self.assertFalse(any(args[0] == "cvt" for args in self.calls))

    def test_main_reads_running_kernel_arguments_and_reports_errors(self):
        with patch.object(display.Path, "read_text", return_value=BOOT), \
                patch.object(display, "apply_modes") as apply:
            self.assertEqual(display.main(), 0)
            apply.assert_called_once_with(BOOT)
        with patch.object(display.Path, "read_text", return_value=BOOT), \
                patch.object(display, "apply_modes", side_effect=display.DisplayModeError("mode rejected")), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(display.main(), 1)
        self.assertIn("cannot apply configured display mode: mode rejected", output.getvalue())
        self.assertNotIn("PARTUUID", output.getvalue())

    def test_failed_switch_or_wrong_dimensions_or_refresh_are_errors(self):
        for state in (BEFORE, AFTER.replace("59.95*", "30.00*"), AFTER.replace("59.95*", "59.95")):
            with self.subTest(state=state), self.assertRaisesRegex(display.DisplayModeError, "did not activate"):
                self.apply(states=(BEFORE, state))
        with self.assertRaisesRegex(display.DisplayModeError, "mock display command failed"):
            self.apply(fail=lambda args: "--mode" in args)

    def test_missing_cvt_or_invalid_generated_dimensions_are_errors(self):
        before = BEFORE.replace("   1536x768     59.95\n", "")
        with self.assertRaises(display.DisplayModeError):
            self.apply(states=(before,), fail=lambda args: args[0] == "cvt")
        with self.assertRaisesRegex(display.DisplayModeError, "does not match"):
            self.apply(states=(before,), cvt=CVT.replace("1536 1616", "1530 1616"))

    def test_command_execution_preserves_x_auth_uses_no_shell_and_is_bounded(self):
        with patch.dict(display.os.environ, {"DISPLAY": ":77", "XAUTHORITY": "/tmp/test-auth"}), \
                patch.object(display.subprocess, "run") as run:
            run.return_value.stdout = "output"
            self.assertEqual(display.run("xrandr", "--current"), "output")
        args, kwargs = run.call_args
        self.assertEqual(args, (("xrandr", "--current"),))
        self.assertEqual(kwargs["env"]["DISPLAY"], ":77")
        self.assertEqual(kwargs["env"]["XAUTHORITY"], "/tmp/test-auth")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertNotIn("shell", kwargs)


class DisplaySessionTests(unittest.TestCase):
    def session(self, success):
        # Start the actual separate X-client entry point. Its helper runs with
        # fake X commands, and changes the state queried by the shell afterwards.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = path / "state"
            state.write_text(BEFORE)
            (path / "after").write_text(AFTER)
            helper = path / "apply.py"
            helper.write_text('''import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import display_mode as display
state = Path(os.environ["DISPLAY_TEST_STATE"])
def run(*args):
    assert os.environ["DISPLAY"] == ":77"
    assert os.environ["XAUTHORITY"] == "test-auth"
    if args == ("xrandr", "--current"):
        return state.read_text()
    if not ''' + repr(success) + ''':
        raise display.DisplayModeError("test switch rejected")
    assert args == ("xrandr", "--output", "HDMI-1", "--mode", "1536x768", "--rate", "60")
    state.write_text(state.with_name("after").read_text())
    return ""
display.run = run
display.apply_modes(''' + repr(BOOT) + ''')
''')
            script = r'''
python3() {
  [[ "$1" == */display_mode.py ]] || return 99
  command python3 "$DISPLAY_TEST_HELPER" "$DISPLAY_TEST_ROOT"
}
xrandr() { [[ "$*" == --current ]] || return 99; cat "$DISPLAY_TEST_STATE"; }
xset() { return 0; }
exec() { printf 'BROWSER %s\n' "$*"; }
export -f python3 xrandr xset exec
/bin/bash "$DISPLAY_TEST_ROOT/tvads.sh" --chromium-session 'https://example.test/ads/STATION'
'''
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                    env={**display.os.environ, "DISPLAY": ":77", "XAUTHORITY": "test-auth",
                                         "DISPLAY_TEST_STATE": str(state), "DISPLAY_TEST_HELPER": str(helper),
                                         "DISPLAY_TEST_ROOT": str(ROOT)},
                                    capture_output=True, text=True, timeout=10)
            return result

    def test_session_applies_override_before_measuring_and_launching_chromium(self):
        result = self.session(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Applied boot display mode: HDMI-1 1536x768 at 60 Hz", result.stdout)
        self.assertIn("Chromium window size from X: 1536x768", result.stdout)
        self.assertIn("--window-size=1536,768", result.stdout)
        self.assertNotIn("--window-size=3840,2160", result.stdout)

    def test_failed_override_does_not_launch_browser_at_auto_selected_resolution(self):
        result = self.session(False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configured display mode failed; Chromium was not started", result.stdout)
        self.assertNotIn("BROWSER", result.stdout)


if __name__ == "__main__":
    unittest.main()

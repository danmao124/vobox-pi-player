import base64
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ad_layout as layout


def panel(station, x=0, y=0, w=100, h=100, **options):
    return dict(station=station, x=x, y=y, w=w, h=h, **options)


def raw(value):
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False).encode()).decode().rstrip("=")


def config_block():
    source = (ROOT / "tvads.sh").read_text()
    # Exercise the real startup config code without touching auth, devices, or renderers.
    return source.split("# ---------- load config ----------", 1)[1].split("# Device auth", 1)[0]


class LayoutTests(unittest.TestCase):
    def compile(self, source, stations):
        encoded = layout.compile_layout(source, stations)
        self.assertRegex(encoded, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(encoded, layout.compile_layout(source, stations))
        return layout.decode_layout(encoded)

    def test_website_split_vectors_and_shared_edges(self):
        # Mirrored from vobox-website/tests/ad-layout.test.mjs; no checkout needed.
        cases = [
            ("A:70 | (B / C):30", [panel("A", 0, 0, 70, 100), panel("B", 70, 0, 30, 50), panel("C", 70, 50, 30, 50)]),
            ("A | (B / C)", [panel("A", 0, 0, 50, 100), panel("B", 50, 0, 50, 50), panel("C", 50, 50, 50, 50)]),
            ("(A | B) / C", [panel("A", 0, 0, 50, 50), panel("B", 50, 0, 50, 50), panel("C", 0, 50, 100, 50)]),
            ("(A | B) / (C | D)", [panel("A", 0, 0, 50, 50), panel("B", 50, 0, 50, 50), panel("C", 0, 50, 50, 50), panel("D", 50, 50, 50, 50)]),
            ("A:2 | B", [panel("A", 0, 0, 66.666667, 100), panel("B", 66.666667, 0, 33.333333, 100)]),
            ("A | B | C", [panel("A", 0, 0, 33.333333, 100), panel("B", 33.333333, 0, 33.333334, 100), panel("C", 66.666667, 0, 33.333333, 100)]),
            (" A:.5 | (B / C:3):1.5 ", [panel("A", 0, 0, 25, 100), panel("B", 25, 0, 75, 25), panel("C", 25, 25, 75, 75)]),
        ]
        for expression, panels in cases:
            with self.subTest(expression=expression):
                ids = ",".join(item["station"] for item in reversed(panels))
                self.assertEqual(self.compile(expression, ids), {"v": 1, "panels": panels})

    def test_canonical_ascii_encoding_matches_website_json(self):
        expected = {"v": 1, "panels": [panel("A", 0, 0, 70, 100), panel("B", 70, 0, 30, 50), panel("C", 70, 50, 30, 50)]}
        encoded = base64.urlsafe_b64encode(json.dumps(expected, separators=(",", ":")).encode()).decode().rstrip("=")
        self.assertEqual(layout.compile_layout("A:70 | (B / C):30", "A,B,C"), encoded)

    def test_case_normalization_does_not_reorder_panels_or_authorize_stations(self):
        result = self.compile(" b | a ", " a, b, A ")
        self.assertEqual([item["station"] for item in result["panels"]], ["B", "A"])
        for stations in ("A", "A,B,C", "A,C"):
            with self.subTest(stations=stations), self.assertRaisesRegex(ValueError, "match ID exactly"):
                layout.compile_layout("A | B", stations)

    def test_blank_layout_preserves_legacy_behavior(self):
        for value in ("", " \n\t", "\ufeff"):
            self.assertEqual(layout.compile_layout(value, "A,B"), "")

    def test_encoded_unicode_gaps_overlaps_fits_and_layer_order(self):
        for fit in layout.FITS:
            custom = {"v": 1, "panels": [panel("主屏"), panel("Inset", 70, 70, 25, 25, fit=fit)]}
            expected = {"v": 1, "panels": [panel("主屏"), panel("INSET", 70, 70, 25, 25, fit=fit)]}
            self.assertEqual(self.compile("base64url:" + raw(custom), "inset,主屏"), expected)
        self.assertEqual(self.compile("主屏 | café", "CAFÉ,主屏")["panels"][1]["station"], "CAFÉ")

    def test_repeated_kiosk_stations_are_rejected_in_both_formats(self):
        for source, stations in (("(BABY | PEPE) / pepe", "BABY,PEPE"), ("a | A", "A"),
                                 ("base64url:" + raw({"v": 1, "panels": [panel("a"), panel("A")]}), "A")):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "public links.*playback reports"):
                layout.compile_layout(source, stations)

    def test_malformed_ambiguous_or_excessive_splits(self):
        invalid = ["A | B / C", "A |", "| A", "(A | B", "A B", "A:0 | B", "A:-1 | B",
                   "A:2junk | B", "A:1e2 | B", "A:1000001 | B", "A:NaN | B", "A:1. | B",
                   "A::2", "()", "A)", "A&index=9", "..", "A\\B", "A#B", "s1:A|B",
                   "(" * 17 + "A" + ")" * 17, "A " * 1025, "|".join(f"A{i}" for i in range(17)),
                   "A:0.000000001 | B"]
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(ValueError):
                value = layout.parse_split(source)
                # Match the parsed stations so a mismatch cannot mask a parser bug.
                layout.validate_layout(value, [item["station"].upper() for item in value["panels"]])
        self.assertEqual(self.compile("(" * 16 + "A" + ")" * 16, "A")["panels"], [panel("A")])
        stations = [f"A{i}" for i in range(16)]
        self.assertEqual(len(self.compile("|".join(stations), ",".join(stations))["panels"]), 16)

    def test_invalid_wire_options_and_geometry(self):
        changes = [{"x": -1}, {"y": -1}, {"w": 0}, {"h": -1}, {"w": 101}, {"x": 60, "w": 50},
                   {"y": 90, "h": 11}, {"x": "0"}, {"x": True}, {"w": float("inf")}, {"w": 10 ** 400},
                   {"x": 10 ** 400, "w": 0.5},
                   {"h": None}, {"fit": "fill"}, {"fit": None}, {"rotation": 90}, {"station": "A,B"},
                   {"station": ".."}, {"station": "A&index=9"}, {"station": " A"}, {"station": "A\x00"},
                   {"station": "A" * 129}, {"station": "💡" * 65}]
        invalid = [None, [], {}, {"v": 2, "panels": [panel("A")]}, {"v": True, "panels": [panel("A")]},
                   {"v": 1, "panels": []}, {"v": 1, "panels": [panel("A")] * 17},
                   {"v": 1, "panels": [panel("A")], "extra": True}, {"v": 1, "panels": [None]}]
        invalid += [{"v": 1, "panels": [{**panel("A"), **change}]} for change in changes]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                layout.compile_layout("base64url:" + raw(value), "A")

    def test_invalid_encoding_utf8_json_and_length(self):
        invalid = ["", "%bad", "abc=", "a", "a" * 8193, "_w", raw({"v": 2}), raw("not an object"),
                   base64.urlsafe_b64encode(b'[' * 2000).decode().rstrip("=")]
        for value in invalid:
            with self.subTest(value=value[:80]), self.assertRaises(ValueError):
                layout.compile_layout("base64url:" + value, "A")
        # Also bound the generated payload, not just encoded input.
        ids = ["主" * 126 + chr(65 + i) for i in range(16)]
        with self.assertRaisesRegex(ValueError, "8192"):
            layout.compile_layout("|".join(ids), ",".join(ids))

    def test_encoded_input_accepts_exact_8192_character_boundary(self):
        value = {"v": 1, "panels": [panel("A")]}
        data = json.dumps(value).ljust(6144).encode()
        encoded = base64.urlsafe_b64encode(data).decode()
        self.assertEqual(len(encoded), 8192)
        self.assertEqual(self.compile("base64url:" + encoded, "A"), value)
        result = subprocess.run([sys.executable, str(ROOT / "ad_layout.py"), "A"],
                                input="base64url:" + encoded, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(layout.decode_layout(result.stdout.strip()), value)


class LayoutConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.env"

    def load(self, value=None, stations=" c, a, B, a ", suffix=""):
        self.config.write_text("API_BASE=https://example.test/api\nID=" + shlex.quote(stations) + "\n" +
                               ("LAYOUT=" + shlex.quote(value) + "\n" if value is not None else ""))
        script = f"unset LAYOUT\nSCRIPT_DIR={shlex.quote(str(ROOT))}\nCONFIG={shlex.quote(str(self.config))}\n"
        script += config_block() + '\nprintf "%s\\n%s\\n" "$ID" "$LAYOUT_ENCODED"\n' + suffix
        return subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=self.root,
                              capture_output=True, text=True, timeout=5)

    def test_config_loads_and_reloads_without_changing_authorized_station_order(self):
        for value in ("a | (b / c)", "(a | b) / c", "", None):
            result = self.load(value)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.stdout.splitlines(), ["C,A,B", layout.compile_layout(value or "", "C,A,B")])

    def test_config_file_change_takes_effect_only_after_restart(self):
        result = self.load("A | (B / C)", suffix='printf "LAYOUT=\\\"(A | B) / C\\\"\\n" > "$CONFIG"\nprintf "%s\\n" "$LAYOUT_ENCODED"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1], result.stdout.splitlines()[2])

    def test_invalid_layout_stops_startup_with_actionable_diagnostic(self):
        cases = [("A | B / C", "parentheses"), ("A | B", "missing: 'C'"),
                 ("A | B | D", "unexpected: 'D'"), ("(A | B) / B", "public links"),
                 ("base64url:abc=", "unpadded base64url"), ("s1:A|B", "not supported")]
        for value, diagnostic in cases:
            result = self.load(value, suffix="echo WRONG-STARTUP\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("ERROR: invalid LAYOUT in config.env:", result.stderr)
            self.assertIn(diagnostic, result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_layout_is_literal_data_and_diagnostics_do_not_echo_the_input(self):
        for value in ("$(touch injected)", "`touch injected`", "A; touch injected", "A&secret=do-not-log"):
            result = self.load(value)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "injected").exists())
            self.assertNotIn(value, result.stdout + result.stderr)
        # Shell punctuation which the website allows in an ID also stays data.
        result = self.load("$A;`literal`", stations="$A;`literal`")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "tvads.sh").read_text()


def functions(*names):
    return "\n".join(
        name + "() {" + SOURCE.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        for name in names
    )


def run_shell(script):
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                          capture_output=True, text=True, check=True)


class StationMetadataTests(unittest.TestCase):
    def test_fetch_and_promotion_keep_ratio_bound_to_the_cached_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = {"response": {"data": [{"url": "https://example.test/ad.png"}],
                                     "orientation": "3:2", "index": "4", "blastIndex": "2"}}
            script = functions("fetch_batch_to", "with_cache_lock", "promote_main_list", "station_orientation")
            script += "\n" + "\n".join(line for line in SOURCE.splitlines() if line.startswith("JQ_"))
            script += f"\nSTATE_DIR={shlex.quote(directory)}\nRESPONSE={shlex.quote(json.dumps(response))}\n"
            script += r'''
MAIN_LIST="$STATE_DIR/main.txt" WEB_CONTENT_FILE="$STATE_DIR/web.txt"
ID=bay101 WEB_STATION='' API_BASE=https://example.test VIEW_PATH=view/billboard
CURL_API_OPTS=(--silent)
log() { :; }
build_curl_auth_headers() { curl_headers=(-H test-auth); }
note_fetch_reach_ok() { :; }
curl() { printf '%s' "$RESPONSE"; }
fetch_batch_to 0 0 "$STATE_DIR/pending.txt" "$STATE_DIR/next.txt" "$STATE_DIR/blast.txt"
promote_main_list "$STATE_DIR/pending.txt"
station_orientation
'''
            self.assertEqual(run_shell(script).stdout.strip(), "3:2")
            metadata = json.loads((root / "main.txt.cursor").read_text())
            self.assertEqual(metadata["stationId"], "BAY101")
            self.assertEqual(metadata["orientation"], "3:2")
            self.assertEqual(metadata["nextIndex"], 4)
            self.assertEqual(metadata["nextBlastIndex"], 2)
            self.assertEqual(metadata["playlistHash"], hashlib.sha256((root / "main.txt").read_bytes()).hexdigest())

    def test_cached_ratio_rejects_other_stations_mismatched_playlists_and_unsafe_values(self):
        with tempfile.TemporaryDirectory() as directory:
            playlist = Path(directory) / "main.txt"
            playlist.write_text("https://example.test/ad.png\n")
            metadata = {"playlistHash": hashlib.sha256(playlist.read_bytes()).hexdigest(),
                        "stationId": "BAY101", "orientation": "12:8"}
            script = functions("station_orientation") + f"\nMAIN_LIST={shlex.quote(str(playlist))}\nID=bay101\nstation_orientation\n"
            cases = [({}, "12:8"), ({"orientation": " portrait "}, "portrait"),
                     ({"orientation": None}, ""), ({"orientation": "3:2,another-option=yes"}, ""),
                     ({"orientation": "1" * 100 + ":2"}, ""), ({"playlistHash": "stale"}, ""),
                     ({"stationId": "MCB"}, "")]
            for override, expected in cases:
                with self.subTest(override=override):
                    Path(str(playlist) + ".cursor").write_text(json.dumps({**metadata, **override}))
                    self.assertEqual(run_shell(script).stdout.strip(), expected)
            Path(str(playlist) + ".cursor").unlink()
            self.assertEqual(run_shell(script).stdout, "")

    def test_ratio_changes_update_the_reused_player_once_and_missing_ratio_clears_margins(self):
        script = functions("update_mpv_layout") + r'''
MPV_LAYOUT_ORIENTATION=''
ratio='3:2'
station_orientation() { printf '%s' "$ratio"; }
mpv_send() { printf '%s\n' "$1"; }
update_mpv_layout
update_mpv_layout
ratio='1:2'
update_mpv_layout
ratio=''
update_mpv_layout
'''
        commands = [json.loads(line)["command"] for line in run_shell(script).stdout.splitlines()]
        self.assertEqual(commands, [["script-message", "station-aspect", ratio] for ratio in ("3:2", "1:2", "")])

    def test_mpv_launch_preserves_assets_and_loads_the_initial_station_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            script = functions("write_mpv_layout_script", "start_mpv_if_needed")
            script += f"\nSTATE_DIR={shlex.quote(directory)}\n"
            script += r'''
MPV_SOCK="$STATE_DIR/no-socket" ORIENTATION=90
wizard_active() { return 1; }
log() { :; }
station_orientation() { echo '12:8'; }
mpv() { printf '%s\n' "$@" > "$STATE_DIR/args"; }
sleep() { :; }
# No real display/socket in this harness; inspect the launch even though readiness times out.
start_mpv_if_needed || true
wait
'''
            run_shell(script)
            args = (Path(directory) / "args").read_text().splitlines()
            self.assertIn("--keepaspect=yes", args)
            self.assertIn("--panscan=0", args)
            self.assertIn("--video-rotate=90", args)
            self.assertIn("--script-opts=station-layout-ratio=12:8", args)
            self.assertIn(f"--script={directory}/station-layout.lua", args)


@unittest.skipUnless(shutil.which("lua"), "Lua is needed to execute the embedded mpv layout checks")
class MpvGeometryTests(unittest.TestCase):
    def test_geometry_rotation_resize_invalid_ratios_and_black_background(self):
        with tempfile.TemporaryDirectory() as directory:
            run_shell(functions("write_mpv_layout_script") + f"\nSTATE_DIR={shlex.quote(directory)}\nwrite_mpv_layout_script\n")
            harness = Path(directory) / "check.lua"
            harness.write_text(r'''
local props = { ['osd-dimensions'] = { w = 1920, h = 1080, aspect = 16 / 9 }, ['video-rotate'] = 0 }
if arg[2] == 'modern' then props['options/background-color'] = '#ffffff' end
local observers, messages = {}, {}
local writes = 0
package.preload['mp'] = function() return {
    get_property_native = function(name) return props[name] end,
    get_property_number = function(name, fallback) return props[name] or fallback end,
    set_property_number = function(name, value) props[name] = value; writes = writes + 1 end,
    set_property = function(name, value) props[name] = value end,
    observe_property = function(name, kind, callback) observers[name] = callback end,
    register_script_message = function(name, callback) messages[name] = callback end,
} end
package.preload['mp.options'] = function() return {
    read_options = function(options) options.ratio = '12:8' end,
} end
dofile(arg[1])
local function margins(x, y)
    for side, expected in pairs({left = x, right = x, top = y, bottom = y}) do
        local actual = props['video-margin-ratio-' .. side] or 0
        assert(math.abs(actual - expected) < 1e-9, side .. ': ' .. actual .. ' != ' .. expected)
    end
end
margins(0.078125, 0) -- 3:2 stage: 1620x1080 centered in a 1920x1080 output.
if arg[2] == 'modern' then
    assert(props.background == 'color' and props['background-color'] == '#000000')
else assert(props.background == '#000000') end
local before = writes
observers['osd-dimensions']()
assert(writes == before) -- Margin changes cannot create an observer feedback loop.
messages['station-aspect']('portrait'); margins(0.341796875, 0)
messages['station-aspect']('landscape'); margins(0, 0)
messages['station-aspect']('4:8'); margins(0.359375, 0)
messages['station-aspect']('1.5:1'); margins(0.078125, 0)
for _, rotation in ipairs({90, 270}) do
    props['video-rotate'] = rotation; observers['video-rotate']()
    margins(0.3125, 0) -- Rotate the logical 3:2 stage with the asset.
end
props['video-rotate'] = 180; observers['video-rotate'](); margins(0.078125, 0)
props['osd-dimensions'] = {w = 1080, h = 1920, aspect = 9 / 16}
observers['osd-dimensions'](); margins(0, 0.3125)
props['osd-dimensions'] = {w = 1200, h = 800, aspect = 1.5}
observers['osd-dimensions'](); margins(0, 0)
for _, value in ipairs({'', '0:1', '-1:2', 'nan:2', '9:1', '1:9', '1.0001:1', '10001:10001', '1.1.1:2', '1793:1792'}) do
    messages['station-aspect']('portrait')
    messages['station-aspect'](value)
    margins(0, 0) -- Unknown/invalid geometry fits the viewport without stretching.
end
props['osd-dimensions'] = { w = 0, h = 0 }
observers['osd-dimensions']() -- Initial VO creation may not have dimensions yet.
print('geometry verified')
''')
            for version in ("modern", "legacy"):
                with self.subTest(version=version):
                    result = subprocess.run([shutil.which("lua"), str(harness), str(Path(directory) / "station-layout.lua"), version],
                                            capture_output=True, text=True, check=True)
                    self.assertIn("geometry verified", result.stdout)


if __name__ == "__main__":
    unittest.main()

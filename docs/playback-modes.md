# Native and Chromium playback

Release `2026.10.09.1` reads the boolean `response.hasYoutube` from successful
`view/billboard` and `device/askforevent` responses. The backend evaluates the
whole currently eligible station playlist, including applicable default/blast
items, rather than only the returned batch.

- For one station, `true` selects Chromium at `/ads/:id`, which plays the complete mixed playlist.
- For one station, `false` selects mpv again, unless a separate web-station schedule still requests
  a browser page through `webContent`.
- Missing/malformed flags and failed requests preserve the last confirmed signal.
  A successful empty playlist clears the previous playlist.

To play multiple independent station panels, set the comma-separated `ID` in
`/data/player/config.env`, for example `ID="BAY101,MCB"`, then restart the service.
The player trims whitespace, uppercases IDs, removes duplicates, and accepts up
to 16 distinct stations. Multiple stations always use Chromium, including when
`hasYoutube` is false, so all panels remain visible. Removing a station from the
configuration takes effect on restart. Without an explicit `LAYOUT`, a single
remaining station resumes the normal YouTube-based renderer selection.

## Optional screen layout

Release `2026.10.10.1` adds `LAYOUT` in `/data/player/config.env`. See
[`config.env.example`](../config.env.example) for a complete example. For three
authorized stations:

```sh
ID='A,B,C'
LAYOUT='A:70 | (B / C):30'
```

This gives A the left 70% of the screen and divides the right 30% equally between
B above C. `|` means side by side, `/` means top to bottom, and parentheses group
a section. Splits default to equal sizes. Weights are relative, so `A:2 | B`
means two-thirds/one-third. Other examples for `ID='A,B,C'`:

```sh
LAYOUT='A | (B / C)'
LAYOUT='(A | B) / C'
```

Use parentheses when mixing the two operators: `A | B / C` is rejected as
ambiguous. Positive decimal weights may be at most 1000000. Readable expressions
may contain at most 2048 characters, 16 panels and 16 nesting levels. Coordinates
are rounded to six decimal places; weights so small that a panel rounds to zero
are rejected. IDs containing shorthand punctuation can use encoded JSON instead.

Every configured station must appear exactly once. IDs are matched
case-insensitively and normalized to uppercase. Layout panel order controls
geometry/layering; `ID` continues to control authorization, the primary station,
and URL path order. The existing shared `index=-2` remains separate and applies to
every panel. A layout never adds or removes authorized stations.

**Repeated stations are unsupported in authenticated Pi kiosks.** For example,
`ID='BABY,PEPE'` with `LAYOUT='(BABY | PEPE) / PEPE'` fails validation, even though
the same composition works on a public website link. Two panels for one kiosk
station/session would send competing playback reports. Enabling that requires
separate website/reporting work; the player does not drop copies or bypass kiosk
authentication.

An explicit layout selects Chromium even for a single station with no YouTube
items, and takes precedence over a web-station schedule, like multi-station mode.
Omitting `LAYOUT`, setting `LAYOUT=''`, or supplying only whitespace preserves the
existing layout and renderer selection. Restart the service after editing config;
there is no hot reload. Startup, recovery and daily refresh re-read and validate
the config in a fresh process with a new playback session. Invalid layouts stop
startup with an `ERROR: invalid LAYOUT in config.env` diagnostic instead of showing
a different layout. Restarting with blank `LAYOUT` restores the previous behavior.

Layout values are parsed as data, never evaluated as commands. The config file
itself follows the existing trusted Bash `source` convention: use single quotes
as above, especially around values containing shell punctuation. Do not put shell
commands or command substitutions in config values.

For gaps, overlaps, or per-panel fit, use `LAYOUT='base64url:ENCODED_VALUE'`, where
`ENCODED_VALUE` is unpadded base64url of UTF-8 JSON with this exact v1 schema:

```json
{"v":1,"panels":[{"station":"A","x":0,"y":0,"w":70,"h":100},{"station":"B","x":70,"y":0,"w":30,"h":50},{"station":"C","x":70,"y":50,"w":30,"h":50}]}
```

For example, save your JSON to `layout.json` and generate the config line with
Python's standard library:

```sh
python3 - <<'PY'
import base64
from pathlib import Path
encoded = base64.urlsafe_b64encode(Path('layout.json').read_bytes()).decode().rstrip('=')
print("LAYOUT='base64url:" + encoded + "'")
PY
```

Rectangles use screen percentages measured from the top left **before** whole-screen
rotation. Width/height must be positive and rectangles must stay within 0–100%.
Later panels draw over earlier ones; uncovered space is black. A panel may add
`"fit":"contain"`, `"cover"`, or `"stretch"`. Explicit layouts default to `contain`.
Unknown fields/versions, invalid rectangles, station mismatches, repeated stations,
and encoded payloads over 8192 characters are rejected. The `base64url:` prefix is
only a config marker; the player validates and sends just the encoded JSON as one
URL `layout` parameter. The proposed `s1:` URL syntax is not supported.

The deployable helper `ad_layout.py` uses only Python's standard library and the
existing `playback_mode.py`; it does not need Node.js or a website checkout. To
validate a literal value locally without launching a renderer, feed it on stdin:

```sh
printf '%s' 'A:70 | (B / C):30' | python3 ad_layout.py 'A,B,C'
```

## Audio, rotation and browser operation

To enable Chromium audio for one station, set `UNMUTED_ID` in the same config:

```sh
ID="baby,pepe"
UNMUTED_ID="baby"
```

With `API_BASE="https://venditt.com/api/v1/user"`, this launches
`https://venditt.com/ads/BABY%2CPEPE` with `unmute=BABY` alongside the kiosk
authentication parameters. Only BABY is unmuted; PEPE remains muted. The setting
is trimmed and matched case-insensitively against `ID`. Blank, omitted, or
unmatched values omit `unmute` entirely, keeping all Chromium ad panels muted.
Set only one station ID. Restart the player service after changing the config.
This setting controls the `/ads` browser page; mpv audio behavior is unchanged.

Every Chromium ad launch includes `index=-2`, for both single- and multi-station
layouts. The shared index applies to every ad panel. Web-station launch URLs
do not receive this parameter. Deploy the website URL-index support first.

Chromium ad pages receive `orientation=0|90|180|270` from `ORIENTATION` in
`/data/player/config.env` (clockwise degrees). Missing, blank, or invalid browser
orientation values default to `0`. For example, adding `ORIENTATION=90` to the
config above sends both `unmute=BABY` and `orientation=90`, along with the kiosk
authentication parameters. The website rotates the entire station layout together
and swaps its layout width/height at 90/270 degrees. Restart the service after
changing orientation; startup, renderer changes, recovery, and daily refresh all
use the same launch URL builder.

Before each Chromium launch, the player reads the first connected output with an
active CRTC mode from `kmsprint` and uses its width/height for `--window-size`
(for example, `3840,2160` for a 4K output). It checks other DRM cards if the default
card has no usable mode. Missing/failed `kmsprint` or no active mode falls back
to `1920,1080` with a warning. The selected size is logged. This samples the mode
before X starts; it does not change the HDMI mode or refresh rate. Orientation
still happens in the website, so the window dimensions are not swapped.
Install `kms++-utils` if `kmsprint` is missing. After deploying, restart the player
with the TV connected and check the `Chromium window size from kmsprint` log line.

The native heartbeat runs immediately after startup and then once per minute in
the device's existing slot. It continues while Chromium is active. Browser mode
also refreshes billboard metadata once per minute, including web schedules.
Changes interrupt native image/video waits. The player exits and the existing
systemd `Restart=always` policy restarts it, releasing mpv/X before starting the
new renderer. Expect a brief display interruption, including the service's startup
delay; this is not a seamless video transition.

`playback-mode.json` in the state directory is shared with background fetches using
a lock and atomic replacement. Request start times prevent an older response from
undoing a newer signal. State is bound to the configured station list and survives
a service restart in `/tmp`; a single-station machine reboot requires a fresh
response to select Chromium. Multiple stations start Chromium immediately.

## Watchdog and manual sync

Each player process creates a new `playbackSession`. Native heartbeats report it
with `playbackMode: "mpv"` or `"chromium"`. mpv retains its existing confirmed
playback reports and timed batch cutovers. Native heartbeats omit mpv telemetry
in Chromium mode.

Heartbeats register `adStationIds` for every panel and keep `adStationId` as the
first station for compatibility. The heartbeat's `hasYoutube` is true if any
registered station currently needs YouTube. Native billboard metadata requests
still use the first station; each browser panel fetches its own playlist.

The dedicated Chromium kiosk receives `kiosk=1`, `deviceId`, `secret`, and
`playbackSession` in its launch URL. The website removes credentials from the URL
and uses them in memory to sign `device/browserplayback` requests. Kiosk URLs are
not written to player logs. Public `/ads` visitors do not enroll in device sync.
The native supervisor refreshes the ad kiosk daily through a full service restart
with a new session, rather than reloading a URL whose credentials were removed.

The website reports actual image/video/YouTube playback using the same playlist
hash, one-based item position, timestamps, and batch-bound next cursors as mpv.
The backend accepts reports only for a whitelisted device's registered Chromium
session and station membership. Each panel has its own reporting, history, sync
timer, playlist cursor, and command deduplication. Sync commands carry
`data.adStationId`; syncing BAY101 does not stop, reload, or reset MCB. Untagged
legacy commands remain supported only for a single-station browser session.
The backend returns retained sync commands separately from the native command queue, so
native polling and Wi-Fi commands continue without consuming the browser's sync.
Both manual sync and the existing opt-in drift watchdog can target Chromium and
mpv devices. As before, sync is an application-level correction, not a guarantee
of frame-accurate decoding or YouTube network startup.

## Deployment and verification

Deploy the backend and website protocol support before enabling this player
release. Deploy `tvads.sh`, `playback_report.py`, `playback_mode.py`, and `ad_layout.py` together
and restart the player service. The existing service must restart on exit and
terminate its entire control group. Chromium, `startx`, and `xset` are required.
Deploy the website's `/ads?orientation=` support before this player release, and
its v1 `/ads?layout=` support before configuring a nonempty `LAYOUT`.
The player no longer rotates `/ads` through `xrandr`; both ad pages and existing
`/player/:orientation/:command` pages rotate in the website. Keep the X/display
configuration unrotated to avoid applying the same rotation twice. Native mpv
still uses `--video-rotate`, and the Wi-Fi wizard still rotates its text console
through fbcon. Neither of those paths changes.

Run `python3 -m unittest discover -s tests` and `bash -n tvads.sh` locally. Tests
use stubbed renderers, HTTP, and network commands. Before fleet rollout, check two
physical Pis: add and remove the final YouTube entry, confirm both mode changes,
check all deployed screen rotations, issue manual sync, and induce drift to
verify watchdog recovery in each mode. Also verify mode changes during an image,
during a long video, and after an API outage. With `ID="BAY101,MCB"`, sync each
station separately and then both together; confirm the sibling keeps playing,
including video/YouTube, and verify that removing a station rejects its old
session's reports. Native tests cannot establish
physical display handoff or real YouTube startup timing.

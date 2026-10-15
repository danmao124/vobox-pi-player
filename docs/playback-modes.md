# Native and Chromium playback

Release `2026.10.14.1` handles the native [`REBOOT_PLAYER` device command](device-commands.md)
through a detached `tvstop && tvstart` worker, including while Chromium is active.

Release `2026.10.11.6` applies explicit kernel `video=` resolutions to X before
launching Chromium. A venue's configured HDMI mode takes precedence over X's
automatic choice, and Chromium is sized after the configured mode is applied.

Release `2026.10.11.5` detects Chromium by its process name rather than its launcher
path. This lets startup proceed to the native heartbeat when the actual browser
runs from `/usr/lib/chromium/chromium`, so its layout session can register.

Release `2026.10.11.4` sizes Chromium inside the running X session, fixing a small
top-left browser window when X selects a higher resolution than the console.

Release `2026.10.11.3` reads the boolean `response.requiresBrowser` from successful
`view/billboard` and `device/askforevent` responses. The backend sets it for layout
stations or currently eligible playlists that require YouTube/browser playback,
including applicable default/blast items rather than only the returned batch.

- For one station, `true` selects Chromium at `/ads/:id`, where the website resolves
  the station's layout or plays its complete mixed playlist.
- For one station, `false` selects mpv again, unless a separate web-station schedule still requests
  a browser page through `webContent`.
- Before a valid `requiresBrowser` is received, the player falls back to the
  legacy `hasYoutube` signal introduced in release `2026.10.09.1`.
- Once confirmed, `requiresBrowser` takes precedence over `hasYoutube`.
  Missing/malformed values and failed requests preserve the last confirmed signal.
  A successful empty playlist clears the previous playlist while still applying
  the browser signal.

A backend-defined layout station needs only its ordinary `ID`, for example
`ID='APNIMANDI-41081-94538-MAIN'`. Configure its panels on the backend; the website
resolves the layout. Deploy the backend's `requiresBrowser` signal and updated
player together. The player no longer defines or encodes panel geometry.

To play multiple independent station panels, set the comma-separated `ID` in
`/data/player/config.env`, for example `ID="BAY101,MCB"`, then restart the service.
The player trims whitespace, uppercases IDs, removes duplicates, and accepts up
to 16 distinct stations. Multiple stations always use Chromium, including when
`requiresBrowser` is false, so all panels remain visible. Removing a station from the
configuration takes effect on restart. A single remaining station resumes the
backend-driven renderer selection.

## Audio, rotation and browser operation

To enable Chromium audio for one station, set `UNMUTED_ID` in the same config:

```sh
ID="baby,pepe"
UNMUTED_ID="baby"
```

With `API_BASE="https://venditt.com/api/v1/user"`, this launches
`https://venditt.com/ads/BABY%2CPEPE` with `unmute=BABY` alongside the kiosk
authentication parameters. Only BABY is unmuted; PEPE remains muted. The setting
is trimmed and uppercased. Starting with release `2026.10.11.1`, it is forwarded
without requiring a match in the top-level `ID`; the website resolves the target,
including child stations inside a backend layout station. For example:

```sh
ID='APNIMANDI-41081-94538-MAIN'
UNMUTED_ID='ABC'
```

With `requiresBrowser=true` from the backend, this launches Chromium for the layout
station and sends `unmute=ABC` so the website can unmute its nested ABC station.
`UNMUTED_ID` alone does not select Chromium.
Blank, omitted, or comma-separated values omit `unmute` entirely, keeping all
Chromium ad panels muted. Set only one station ID. Restart the player service
after changing the config. This setting controls the `/ads` browser page; mpv
audio behavior is unchanged.

Every Chromium ad launch includes `index=-2`, for both single- and multi-station
layouts. The shared index applies to every ad panel. Web-station launch URLs
do not receive this parameter. Deploy the website URL-index support first.

Chromium ad pages receive `orientation=0|90|180|270` from `ORIENTATION` in
`/data/player/config.env` (clockwise degrees). Missing, blank, or invalid browser
orientation values default to `0`. For example, adding `ORIENTATION=90` to the
BABY/PEPE example sends both `unmute=BABY` and `orientation=90`, along with the kiosk
authentication parameters. The website rotates the entire station layout together
and swaps its layout width/height at 90/270 degrees. Restart the service after
changing orientation; startup, renderer changes, recovery, and daily refresh all
use the same launch URL builder.

`startx` runs `tvads.sh --chromium-session <url>` as its X client. This entry point
skips supervisor initialization and inherits the session's `DISPLAY` and
`XAUTHORITY`. It first runs `display_mode.py`, which reads the effective boot
arguments from `/proc/cmdline`. For example:

```text
video=HDMI-A-1:1536x768M@60
```

This selects 1536×768 at approximately 60 Hz on the corresponding X output
(typically `HDMI-1`), even if X initially selected 4K. It verifies the active
output dimensions and refresh rate, then reads the X screen size with
`xrandr --current` for Chromium's `--window-size`. This changes the HDMI mode,
not just the browser window. The mode change can briefly blank the screen.

Existing X modes are used first. If a custom `M` (CVT) mode is missing, the helper
uses `cvt` and `xrandr --newmode/--addmode` to create it; `MR` retains reduced
blanking. Install the `xcvt` package providing `cvt` if that fallback is needed.
Missing interlaced/margin modes are not synthesized. If a configured mode cannot
be applied or verified, startup logs an error and does not launch Chromium at a
different resolution. The supervisor restarts through systemd as before.

Connector-specific overrides only affect that output. Disconnected outputs are
skipped; an unknown connector is an error. A global `video=WIDTHxHEIGHT...` is
accepted only when exactly one X output is connected. Multiple explicit
connector resolutions are applied separately. Console rotation/margin options
do not replace the website's existing `ORIENTATION` behavior.

Without explicit numeric `video=` dimensions, the player retains X's automatic
mode selection. Reboot after editing `/boot/firmware/cmdline.txt` so the effective
boot arguments reflect the change. The player does not rewrite that file.

If X sizing is unavailable, the client samples `kmsprint` **after X startup**,
using the first connected output with an active CRTC and checking other DRM cards
if needed. If both queries fail, it falls back to `1920,1080` with a warning.
These fallback queries do not change the HDMI mode or refresh rate. Orientation
still happens in the website, so the window dimensions are not swapped. Screen
blanking/DPMS setup also runs inside the authenticated X session; the client then `exec`s
Chromium so exiting the browser tears down X through the existing supervisor.

Install `x11-xserver-utils` for `xrandr`/`xset`, and `kms++-utils` for the KMS
fallback. After deploying, restart the player with the TV connected and check
`Chromium window size from X: 3840x2160` (or the explicitly labeled post-X KMS
fallback) on an automatic 4K screen. With the override above, check for
`Applied boot display mode: HDMI-1 1536x768 at 60 Hz` followed by
`Chromium window size from X: 1536x768`. Run `kmsprint` while the player is active
to verify the actual HDMI mode. An SSH shell may need the X session's authentication
environment to run `xrandr`; setting only `DISPLAY=:0` does not supply its authentication cookie.

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
first station for compatibility. The heartbeat's `requiresBrowser` is true if any
registered station is a layout station or otherwise requires browser playback.
The legacy `hasYoutube` flag continues to describe YouTube eligibility only.
Native billboard metadata requests still use the first station; each browser panel
fetches its own playlist.

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
release. Deploy `tvads.sh`, `display_mode.py`, `playback_report.py`, and `playback_mode.py` together
and restart the player service. The existing service must restart on exit and
terminate its entire control group. Chromium, `startx`, and `xset` are required.
Deploy the website's `/ads?orientation=` and backend-defined layout station support
before this player release.
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

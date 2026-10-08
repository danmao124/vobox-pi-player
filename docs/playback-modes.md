# Native and Chromium playback

Release `2026.10.08.3` reads the boolean `response.hasYoutube` from successful
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
configuration takes effect on restart. A single remaining station resumes the
normal YouTube-based renderer selection.

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
release. Deploy `tvads.sh`, `playback_report.py`, and `playback_mode.py` together
and restart the player service. The existing service must restart on exit and
terminate its entire control group. Chromium, `startx`, and `xset` are required;
rotated ad displays also require `xrandr`. The player rotates `/ads` through X;
existing `/player/:orientation/:command` pages retain their own CSS rotation.

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

# Native device commands

The Pi sends signed `POST device/askforevent` heartbeats immediately after startup
and then once per minute in its device slot, in both mpv and Chromium modes.
Commands are objects in `response.data` of a successful response
(`response.success: true`). Existing `sync` and `setWifi` commands continue to
use that flow. Command names are case-sensitive.

## REBOOT_PLAYER

Supported starting with player version `2026.10.14.1`. A response can contain:

```json
{
  "response": {
    "success": true,
    "message": "Acknowledgment Received",
    "data": [
      {"type": "REBOOT_PLAYER", "data": {}}
    ]
  }
}
```

This restarts the player using **`tvstop && tvstart`**. It does not reboot the Pi.
`tvstart` runs only if `tvstop` exits successfully. The command needs no arguments
or timestamp; `_id` is optional to the player. Payload fields are ignored and
cannot supply shell commands, executable paths, or service options. Multiple
`REBOOT_PLAYER` entries in one poll trigger one restart, after the other commands
in that response have been handled.

The existing backend acknowledgement is delivery-based: untimed commands are
removed from the queue when the backend returns them. The player does not send a
separate execution-result acknowledgement or automatically requeue failed
restarts. A successful handoff only means the restart worker launched. Confirm
recovery through a new `playbackSession` heartbeat and the device logs. After
fixing a failed restart, issue a new command.

## Execution and deployment

The player uses `sudo -n systemd-run` to launch the fixed shell sequence in a
separate transient service, `vobox-reboot-player.service`. This follows the Wi-Fi
wizard's transient-service pattern. `venditt-player.service` uses
`KillMode=control-group`: stopping it can kill the poller and its children before
they reach `tvstart`. The worker therefore runs outside that control group, with
systemd managing its lifetime and journal output. An active worker prevents a
second worker with the same name; completed and failed workers are collected so
later commands can retry.

Deploy the updated `tvads.sh` together with backend support for `REBOOT_PLAYER`.
The Pi needs systemd with `Type=exec` and `systemd-run --collect` support, and the
player user needs passwordless sudo for `systemd-run`, as with the Wi-Fi wizard.
The worker runs as root in systemd's clean service environment. Install executable
`tvstop` and `tvstart` helpers on the system service PATH (for example,
`/usr/local/bin`); interactive shell aliases/functions are insufficient. Both
helpers are checked inside the worker before stopping anything. A missing
dispatcher or rejected handoff is logged without falling back to an in-process
stop. A missing helper, failed stop, or failed start is recorded in the worker's
journal.

Inspect logs on a Pi with:

```sh
journalctl -u venditt-player.service -u vobox-reboot-player.service
```

Run `python3 -B -m unittest discover -s tests` and `bash -n tvads.sh` locally.
The reboot tests isolate PATH and substitute all privileged/restart commands;
they never invoke the development machine's helpers or systemd. They simulate
receiver termination, but actual systemd control-group behavior still requires a
Pi smoke test: issue the command, confirm a single stop/start cycle, and check
that heartbeats resume with a new playback session.

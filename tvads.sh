#!/usr/bin/env bash 
set -euo pipefail

# Bump for each player release, including changes to playback_report.py.
# Captured by the running process; updating files takes effect after restart.
readonly PLAYER_VERSION="2026.09.21.1"

CONFIG="/data/player/config.env"
STATE_DIR="/tmp/player/state"
ASSET_DIR="/data/assets"

MAIN_LIST="${ASSET_DIR}/main.txt"
PENDING_LIST="${STATE_DIR}/pending.txt"
INDEX_FILE="${STATE_DIR}/index.txt"
NEXT_FILE="${STATE_DIR}/next.txt"
NEXT_BLAST_FILE="${STATE_DIR}/nextblast.txt"
WEB_CONTENT_FILE="${STATE_DIR}/webcontent.txt"
# On disk so background (detached) fetches share the streak with the main loop
FAIL_STREAK_FILE="${STATE_DIR}/failstreak.txt"
RECOVERY_LOCK="${STATE_DIR}/recovery.lock"
WIZARD_LOCK="${STATE_DIR}/wizard.lock"
# Coordinated sync from askForEvent (batch + unix play-at)
SYNC_LIST="${STATE_DIR}/sync.txt"
SYNC_AT_FILE="${STATE_DIR}/sync_at.txt"
SYNC_NEXT_FILE="${STATE_DIR}/sync_next.txt"
SYNC_BLAST_FILE="${STATE_DIR}/sync_blast.txt"
# Bumped to invalidate in-flight background_fetch_pending writers (no PID kill)
FETCH_GEN_FILE="${STATE_DIR}/fetchgen.txt"
PLAYBACK_HISTORY="${STATE_DIR}/playback-history.json"
LAST_SYNC_COMMAND=""

VIEW_PATH="view/billboard"
ASK_FOR_EVENT_PATH="device/askforevent"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIFI_HOTKEY_SCRIPT="${SCRIPT_DIR}/wifi_hotkey.sh"
WIFI_WIZARD_SCRIPT="${SCRIPT_DIR}/wifi_wizard.sh"

# mpv IPC socket (lives in RAM; fine)
MPV_SOCK="/tmp/venditt-mpv.sock"
CHROMIUM_PID=""
WIFI_HOTKEY_PID=""
# Top-level PID so background fetches can nuke the whole player for a clean systemd restart
PLAYER_PID=$$

cleanup() {
  # stop Ctrl+I watcher first so it can't re-fire during teardown
  if [[ -n "${WIFI_HOTKEY_PID:-}" ]]; then
    kill "$WIFI_HOTKEY_PID" 2>/dev/null || true
    wait "$WIFI_HOTKEY_PID" 2>/dev/null || true
    WIFI_HOTKEY_PID=""
  fi

  # ask mpv to quit nicely; then hard kill if needed
  if [[ -S "$MPV_SOCK" ]]; then
    printf '%s\n' '{"command":["quit"]}' | socat - UNIX-CONNECT:"$MPV_SOCK" >/dev/null 2>&1 || true
  fi
  pkill -f "input-ipc-server=$MPV_SOCK" >/dev/null 2>&1 || true
  rm -f "$MPV_SOCK" >/dev/null 2>&1 || true

  # tear down chromium / X started by this script
  if [[ -n "${CHROMIUM_PID:-}" ]]; then
    kill "$CHROMIUM_PID" 2>/dev/null || true
    wait "$CHROMIUM_PID" 2>/dev/null || true
    CHROMIUM_PID=""
  fi
  pkill -f "/usr/bin/chromium" >/dev/null 2>&1 || true
  pkill -f "X :0" >/dev/null 2>&1 || true
  pkill -f "Xorg :0" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# ---------- load config ----------
if [[ ! -f "$CONFIG" ]]; then
  echo "Missing config: $CONFIG"
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"

: "${API_BASE:?Missing API_BASE in config.env}"
: "${ID:?Missing ID in config.env}"

IMAGE_SECONDS="${IMAGE_SECONDS:-15}"
MAX_CACHE_MB="${MAX_CACHE_MB:-30000}" # Trim above this limit to 90%; keep active assets.
ORIENTATION="${ORIENTATION:-0}"  # Screen orientation: 0, 90, 180, or 270
WEB_STATION="${WEB_STATION:-}"  # Optional web station id

# Device auth (same as api_client.py): device_id = hostname, secret = /etc/machine-id
DEVICE_ID="$(hostname)"
DEVICE_SECRET=""
if [[ -f /etc/machine-id ]]; then
  DEVICE_SECRET="$(cat /etc/machine-id | tr -d '\n')"
fi
if [[ -z "$DEVICE_SECRET" ]]; then
  echo "Missing or empty /etc/machine-id"
  exit 1
fi

# Spread askForEvent across seconds 0–29 of each minute (stable per device).
EVENT_SLOT=$(( 16#${DEVICE_SECRET: -8} % 30 ))

# Build curl auth headers: X-Device-Id, X-Timestamp, X-Signature (HMAC-SHA256(secret, "timestamp.SHA256(body)"))
# Use only openssl (no xxd) so it works on minimal systems e.g. Raspberry Pi.
build_curl_auth_headers() {
  local body="${1:-}"
  local timestamp body_hex canonical signature
  timestamp="$(date +%s)"
  body_hex="$(printf '%s' "$body" | openssl dgst -sha256 -r | awk '{print $1}')"
  canonical="${timestamp}.${body_hex}"
  signature="$(printf '%s' "$canonical" | openssl dgst -sha256 -hmac "$DEVICE_SECRET" -r | awk '{print $1}')"
  if [[ -z "$signature" ]]; then
    echo "ERROR: failed to compute signature (openssl dgst -sha256 -hmac)" >&2
    exit 1
  fi
  curl_headers=(-H "x-device-id: $DEVICE_ID" -H "x-timestamp: $timestamp" -H "x-signature: $signature")
}

CURL_API_OPTS=(--fail --silent --show-error --connect-timeout 5 --max-time 10 -L)
CURL_ASSET_OPTS=(--fail --silent --show-error --connect-timeout 5 --max-time 500 -L)
JQ_URLS='.response.data[]?.url // empty'
JQ_INDEX='.response.index // .response.message // empty'
JQ_BLAST='.response.blastIndex // empty'
JQ_WEBCONTENT='.response.webContent // empty'

log(){ echo "[$(date '+%F %T')] $*"; }

blast_idx=0
FETCH_FAIL_LIMIT=3

# Strip CRLF, trailing whitespace, and trailing commas from URLs
normalize_url() {
  local u="$1"
  u="${u//$'\r'/}"
  u="$(sed -E 's/[[:space:]]+$//; s/,+$//' <<<"$u")"
  printf '%s' "$u"
}

is_video() {
  local u="${1,,}"
  [[ "$u" == *".mp4"* || "$u" == *".webm"* || "$u" == *".m4v"* || "$u" == *".mov"* || "$u" == *".mkv"* ]]
}

ensure_dirs() {
  mkdir -p "$STATE_DIR" "$ASSET_DIR"
  [[ -f "$INDEX_FILE" ]] || echo "0" > "$INDEX_FILE"
  [[ -f "$MAIN_LIST"  ]] || : > "$MAIN_LIST"
  [[ -f "$PENDING_LIST" ]] || : > "$PENDING_LIST"
  [[ -f "$FETCH_GEN_FILE" ]] || echo "0" > "$FETCH_GEN_FILE"
  # No recovery/wizard can be in flight at startup; drop locks left by a killed run
  rm -rf "$RECOVERY_LOCK" "$WIZARD_LOCK" >/dev/null 2>&1 || true
  write_fail_streak 0
}

# Ctrl+I -> nmtui on a free VT -> restart player (see wifi_hotkey.sh / wifi_wizard.sh).
# No systemd unit changes; needs group `input` and passwordless sudo for openvt.
start_wifi_hotkey() {
  if [[ ! -x "$WIFI_HOTKEY_SCRIPT" || ! -x "$WIFI_WIZARD_SCRIPT" ]]; then
    log "WARN: Wi-Fi hotkey scripts missing or not executable (${WIFI_HOTKEY_SCRIPT}, ${WIFI_WIZARD_SCRIPT})"
    return 0
  fi
  if [[ -n "${WIFI_HOTKEY_PID:-}" ]] && kill -0 "$WIFI_HOTKEY_PID" 2>/dev/null; then
    return 0
  fi
  "$WIFI_HOTKEY_SCRIPT" "$PLAYER_PID" "$WIFI_WIZARD_SCRIPT" "$WIZARD_LOCK" &
  WIFI_HOTKEY_PID=$!
  disown "$WIFI_HOTKEY_PID" 2>/dev/null || true
  log "Wi-Fi hotkey watcher PID=$WIFI_HOTKEY_PID (Ctrl+I)"
}

wizard_active() {
  [[ -d "$WIZARD_LOCK" ]]
}

# Wi-Fi wizard kills mpv/Chromium for nmtui; hold playback/kiosk until it restarts us.
wait_while_wizard() {
  if ! wizard_active; then
    return 0
  fi
  log "Wi-Fi wizard active; pausing until player restart"
  while wizard_active; do
    sleep 1
  done
}

read_fail_streak() {
  local n
  n="$(cat "$FAIL_STREAK_FILE" 2>/dev/null || echo 0)"
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  printf '%s' "$n"
}

write_fail_streak() {
  echo "$1" > "$FAIL_STREAK_FILE" 2>/dev/null || true
}

# After consecutive network-outage signals: bounce Wi-Fi radio, else NetworkManager.
# mkdir is atomic, so overlapping fetches can't run two recoveries at once.
recover_network() {
  if ! mkdir "$RECOVERY_LOCK" 2>/dev/null; then
    log "Network recovery already in progress; skipping"
    return 0
  fi
  local rc=0
  run_network_recovery || rc=$?
  rm -rf "$RECOVERY_LOCK" >/dev/null 2>&1 || true
  return "$rc"
}

run_network_recovery() {
  local out
  log "WARN: network outage confirmed ${FETCH_FAIL_LIMIT}x in a row; recovering network"

  # Soft reset: bounce the Wi-Fi radio. NM reactivates the autoconnect profile
  # itself — avoids picking among multiple saved connections / device-connect quirks.
  log "Attempting Wi-Fi radio bounce (nmcli radio wifi off/on)..."
  if out="$(sudo -n nmcli radio wifi off 2>&1)"; then
    [[ -n "$out" ]] && log "  wifi off: $out"
    sleep 5
    if out="$(sudo -n nmcli radio wifi on 2>&1)"; then
      [[ -n "$out" ]] && log "  wifi on: $out"
      log "Wi-Fi radio bounce OK; waiting 15s for association/DHCP..."
      sleep 15
      if internet_ping_ok; then
        log "Network recovery via Wi-Fi radio bounce complete (ping 8.8.8.8 OK)"
        restart_web_kiosk_if_needed
        return 0
      fi
      log "WARN: Wi-Fi radio bounce succeeded but ping 8.8.8.8 still failing; escalating"
    else
      log "WARN: wifi on failed${out:+: $out}"
    fi
  else
    log "WARN: wifi off failed${out:+: $out}"
  fi

  log "Wi-Fi radio bounce did not restore connectivity; restarting NetworkManager..."
  if out="$(sudo -n systemctl restart NetworkManager 2>&1)"; then
    [[ -n "$out" ]] && log "  nm: $out"
    log "NetworkManager restarted; waiting 20s for connectivity..."
    sleep 20
    log "Network recovery via NetworkManager complete"
    restart_web_kiosk_if_needed
    return 0
  fi

  log "ERROR: NetworkManager restart failed${out:+: $out}"
  return 1
}

# Ping Google Public DNS — IP so we don't require working DNS to detect L3 outage
internet_ping_ok() {
  ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1
}

note_fetch_reach_failure() {
  local reason="${1:-network outage}"
  local streak
  streak=$(( $(read_fail_streak) + 1 ))
  write_fail_streak "$streak"
  log "WARN: $reason (consecutive outage: ${streak}/${FETCH_FAIL_LIMIT})"
  if (( streak >= FETCH_FAIL_LIMIT )); then
    write_fail_streak 0
    recover_network || true
  fi
}

note_fetch_reach_ok() {
  local streak
  streak="$(read_fail_streak)"
  (( streak > 0 )) || return 0
  log "Network reachable again; clearing outage streak (was ${streak})"
  write_fail_streak 0
}

# Serialize playlist publication with cache deletion. Fetches/downloads stay outside
# the lock; sync downloads check the cache only after their playlist is published.
with_cache_lock() {
  python3 - "${STATE_DIR}/cache-cleanup.lock" "$@" <<'PY'
import fcntl
import subprocess
import sys

with open(sys.argv[1], "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    sys.exit(subprocess.call(sys.argv[2:]))
PY
}

fetch_batch_to() {
  local idx="$1"
  local blast_idx="$2"
  local out="$3"
  local nextfile="$4"
  local nextblastfile="$5"

  local url="${API_BASE}/${VIEW_PATH}?id=${ID}&index=${idx}&blastIndex=${blast_idx}"
  if [[ -n "$WEB_STATION" ]]; then
    url="${url}&webStationId=${WEB_STATION}"
  fi
  log "Fetch: $url"

  build_curl_auth_headers ""
  local json curl_rc=0
  json="$(curl "${CURL_API_OPTS[@]}" "${curl_headers[@]}" "$url")" || curl_rc=$?
  if (( curl_rc != 0 )); then
    log "WARN: fetch failed (curl rc=$curl_rc); checking Google (8.8.8.8)..."
    if internet_ping_ok; then
      log "Google reachable; treating as server/API issue (not network outage)"
    else
      note_fetch_reach_failure "API unreachable and ping 8.8.8.8 failed"
    fi
    return 1
  fi
  note_fetch_reach_ok

  local urls next next_blast web_content
  # normalize lines coming from API (fixes "a.png," and CRLF issues)
  urls="$(jq -r "$JQ_URLS" <<<"$json" \
    | sed -E 's/\r$//; s/[[:space:]]+$//; s/,+$//; /^$/d' || true)"
  next="$(jq -r "$JQ_INDEX" <<<"$json" | sed '/^$/d' || true)"
  next_blast="$(jq -r "$JQ_BLAST" <<<"$json" | sed '/^$/d' || true)"
  web_content="$(jq -r "$JQ_WEBCONTENT" <<<"$json" | sed '/^$/d' || true)"

  if [[ -n "$web_content" ]]; then
    echo "$web_content" > "$WEB_CONTENT_FILE"
  else
    rm -f "$WEB_CONTENT_FILE"
  fi

  if [[ -z "$urls" ]]; then
    log "WARN: no urls in response"
    return 1
  fi

  # Publish the complete list under the cleanup lock, including sync fetches.
  local list_tmp="${out}.tmp"
  printf "%s\n" "$urls" > "$list_tmp"
  [[ -n "$next" ]] && echo "$next" > "$nextfile" || echo "$idx" > "$nextfile"
  [[ -n "$next_blast" ]] && echo "$next_blast" > "$nextblastfile" || echo "$blast_idx" > "$nextblastfile"
  # Keep the next cursor with this exact playlist, independent of prefetch advancement.
  local playlist_hash
  playlist_hash="$(openssl dgst -sha256 -r "$list_tmp" | awk '{print $1}')"
  jq -nc --arg hash "$playlist_hash" --arg next "$(cat "$nextfile")" \
    --arg blast "$(cat "$nextblastfile")" \
    '{playlistHash:$hash,nextIndex:($next|tonumber),nextBlastIndex:($blast|tonumber)}' > "${out}.cursor"
  if ! with_cache_lock mv -f "$list_tmp" "$out"; then
    rm -f "$list_tmp"
    return 1
  fi
  log "OK: $(wc -l < "$out" | tr -d ' ') assets, nextIndex=$(cat "$nextfile") nextBlastIndex=$(cat "$nextblastfile")"
}

cache_path_for_url() {
  local url="$1"
  local base="${url%%\?*}"
  local filename="${base##*/}"
  echo "${ASSET_DIR}/${filename}"
}

asset_lock_path() {
  echo "$(cache_path_for_url "$1").lock"
}

asset_download_in_progress() {
  local lock pid
  lock="$(asset_lock_path "$1")"
  [[ -f "$lock" ]] || return 1
  pid="$(cat "$lock" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# Kill hung/in-flight asset curls so a dead Wi-Fi doesn't pin the batch for max-time.
abort_in_flight_asset_downloads() {
  local lock pid tmp
  for lock in "$ASSET_DIR"/*.lock; do
    [[ -f "$lock" ]] || continue
    pid="$(cat "$lock" 2>/dev/null || true)"
    tmp="${lock%.lock}.tmp"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "Aborting asset download pid=$pid"
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$lock" "$tmp" >/dev/null 2>&1 || true
  done
}

# Kick off a non-blocking download if needed. Never blocks the play loop.
start_asset_download_bg() {
  local url="$1"
  local path tmp lock
  path="$(cache_path_for_url "$url")"
  tmp="${path}.tmp"
  lock="$(asset_lock_path "$url")"

  [[ -s "$path" ]] && return 0
  if asset_download_in_progress "$url"; then
    return 0
  fi

  # Stale lock / leftover partial from a previous crash
  rm -f "$lock" "$tmp" >/dev/null 2>&1 || true

  (
    set +e
    local curl_pid rc=1
    build_curl_auth_headers ""
    curl "${CURL_ASSET_OPTS[@]}" "${curl_headers[@]}" -o "$tmp" "$url" &
    curl_pid=$!
    echo "$curl_pid" > "$lock"
    trap 'rm -f "$lock"' EXIT
    wait "$curl_pid"
    rc=$?
    if (( rc == 0 )); then
      mv -f "$tmp" "$path"
      log "OK: downloaded $(basename "$path")"
      note_fetch_reach_ok
    else
      rm -f "$tmp" >/dev/null 2>&1 || true
      log "WARN: download failed: $url"
      if internet_ping_ok; then
        log "Google reachable; treating asset failure as server/CDN issue"
      else
        note_fetch_reach_failure "asset download failed and ping 8.8.8.8 failed"
      fi
    fi
  ) &
  disown || true
  log "Queued download: $url"
}

batch_has_downloads_in_progress() {
  local url
  [[ -s "$MAIN_LIST" ]] || return 1
  while IFS= read -r url; do
    url="$(normalize_url "$url")"
    [[ -n "$url" ]] || continue
    asset_download_in_progress "$url" && return 0
  done < "$MAIN_LIST"
  return 1
}

# Ready path on stdout if cached; otherwise queue a background download and fail.
# Playback must not block on the asset curl timeout.
cache_asset() {
  local url="$1"
  local path
  path="$(cache_path_for_url "$url")"

  if [[ -s "$path" ]]; then
    printf '%s\n' "$path"
    return 0
  fi

  start_asset_download_bg "$url"
  return 1
}

read_fetch_gen() {
  local g
  g="$(cat "$FETCH_GEN_FILE" 2>/dev/null || echo 0)"
  [[ "$g" =~ ^[0-9]+$ ]] || g=0
  printf '%s' "$g"
}

# Invalidate in-flight background fetches; returns the new generation.
bump_fetch_gen() {
  local g
  g="$(read_fetch_gen)"
  g=$((g + 1))
  echo "$g" > "$FETCH_GEN_FILE"
  printf '%s' "$g"
}

# Fetch into gen-scoped temp files; only promote if this gen is still current.
background_fetch_pending() {
  local idx="$1"
  local bidx="$2"
  local gen="$3"
  local out nextf blastf

  out="${STATE_DIR}/pending.${gen}.txt"
  nextf="${STATE_DIR}/next.${gen}.txt"
  blastf="${STATE_DIR}/nextblast.${gen}.txt"

  if fetch_batch_to "$idx" "$bidx" "$out" "$nextf" "$blastf"; then
    if [[ "$(read_fetch_gen)" == "$gen" ]]; then
      mv -f "${out}.cursor" "${PENDING_LIST}.cursor"
      with_cache_lock mv -f "$out" "$PENDING_LIST" || return 1
      mv -f "$nextf" "$INDEX_FILE"
      if [[ -f "$blastf" ]]; then
        mv -f "$blastf" "$NEXT_BLAST_FILE"
      fi
    else
      log "Discarding stale background fetch (gen=${gen}, current=$(read_fetch_gen))"
      rm -f "$out" "${out}.cursor" "$nextf" "$blastf" || true
    fi
  else
    rm -f "$out" "${out}.cursor" "$nextf" "$blastf" || true
  fi
}

start_background_fetch_pending() {
  local idx="$1"
  local bidx="$2"
  local gen
  gen="$(bump_fetch_gen)"
  background_fetch_pending "$idx" "$bidx" "$gen" &
  disown || true
}

read_next_blast_index() {
  if [[ -f "$NEXT_BLAST_FILE" ]]; then
    blast_idx="$(cat "$NEXT_BLAST_FILE")"
    rm -f "$NEXT_BLAST_FILE"
  fi
}

# ---------- askForEvent / coordinated sync ----------
# Parse sync data.timestamp (unix seconds or ISO-8601) -> epoch seconds on stdout.
parse_sync_timestamp() {
  local raw="$1"
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    printf '%s' "$raw"
    return 0
  fi
  # GNU date (Pi); fall back to nothing on failure
  date -d "$raw" +%s 2>/dev/null || true
}

clear_sync_pending() {
  rm -f "$SYNC_LIST" "${SYNC_LIST}.cursor" "$SYNC_AT_FILE" "$SYNC_NEXT_FILE" "$SYNC_BLAST_FILE" >/dev/null 2>&1 || true
}

# Fetch batch for a sync command; stage until SYNC_AT.
arm_sync_command() {
  local idx="$1"
  local ts_raw="$2"
  local sync_blast_idx="${3:-$blast_idx}"
  local at url

  if [[ -z "$idx" ]]; then
    log "WARN: sync command missing index; ignoring"
    return 1
  fi
  at="$(parse_sync_timestamp "$ts_raw")"
  if [[ -z "$at" || ! "$at" =~ ^[0-9]+$ ]]; then
    log "WARN: sync command bad timestamp (${ts_raw:-empty}); ignoring"
    return 1
  fi

  log "Sync armed: index=${idx} playAt=${at} (${ts_raw})"
  if ! fetch_batch_to "$idx" "$sync_blast_idx" "$SYNC_LIST" "$SYNC_NEXT_FILE" "$SYNC_BLAST_FILE"; then
    log "WARN: sync fetch failed for index=${idx}"
    clear_sync_pending
    return 1
  fi
  echo "$at" > "$SYNC_AT_FILE"

  # Kick off downloads so assets are warm by playAt
  while IFS= read -r url; do
    url="$(normalize_url "$url")"
    [[ -n "$url" ]] || continue
    start_asset_download_bg "$url"
  done < "$SYNC_LIST"
}

# Apply staged sync to MAIN when the play-at time has been reached (or is past).
# Returns 0 if MAIN was replaced and the play loop should restart the batch.
apply_sync_if_due() {
  local at now
  [[ -f "$SYNC_AT_FILE" && -s "$SYNC_LIST" ]] || return 1
  at="$(cat "$SYNC_AT_FILE" 2>/dev/null || true)"
  [[ "$at" =~ ^[0-9]+$ ]] || { clear_sync_pending; return 1; }
  now="$(date +%s)"
  (( now >= at )) || return 1

  # A slow device keeps its current batch and can retry on the next watchdog sync.
  local url
  while IFS= read -r url; do
    url="$(normalize_url "$url")"
    [[ -n "$url" ]] || continue
    if [[ ! -s "$(cache_path_for_url "$url")" ]]; then
      log "Sync missed: assets not ready; keeping current batch"
      clear_sync_pending
      return 1
    fi
  done < "$SYNC_LIST"

  log "Applying sync batch (playAt=${at}, lag=$(( now - at ))s)"
  # Invalidate any in-flight normal/pending fetch before touching playlist/index.
  bump_fetch_gen >/dev/null
  promote_main_list "$SYNC_LIST"
  if [[ -f "$SYNC_NEXT_FILE" ]]; then
    mv -f "$SYNC_NEXT_FILE" "$INDEX_FILE"
  fi
  if [[ -f "$SYNC_BLAST_FILE" ]]; then
    mv -f "$SYNC_BLAST_FILE" "$NEXT_BLAST_FILE"
    read_next_blast_index
  fi
  : > "$PENDING_LIST" || true
  rm -f "$SYNC_AT_FILE" >/dev/null 2>&1 || true
  # Prefetch the following batch after the synced one
  start_background_fetch_pending "$(cat "$INDEX_FILE" 2>/dev/null || echo "0")" "$blast_idx"
  return 0
}

# True when a staged sync should cut into current playback (due or within ~1s).
sync_should_interrupt_playback() {
  local at now
  [[ -f "$SYNC_AT_FILE" && -s "$SYNC_LIST" ]] || return 1
  at="$(cat "$SYNC_AT_FILE" 2>/dev/null || true)"
  [[ "$at" =~ ^[0-9]+$ ]] || return 1
  now="$(date +%s)"
  (( now >= at - 1 ))
}

# Busy-wait until play-at (if still in the future). Do not stop mpv — next
# loadfile replace keeps the last frame, same as a normal batch swap.
wait_out_sync_deadline() {
  local at now
  at="$(cat "$SYNC_AT_FILE" 2>/dev/null || true)"
  [[ "$at" =~ ^[0-9]+$ ]] || return 0
  while true; do
    now="$(date +%s)"
    (( now >= at )) && break
    sleep 0.05
  done
}

seconds_until_event_slot() {
  local now_s
  now_s=$(( $(date +%s) % 60 ))
  if (( now_s < EVENT_SLOT )); then
    echo $(( EVENT_SLOT - now_s ))
  elif (( now_s > EVENT_SLOT )); then
    echo $(( 60 - now_s + EVENT_SLOT ))
  else
    echo 0
  fi
}

build_event_body() {
  local playback_body
  playback_body="$(python3 "$SCRIPT_DIR/playback_report.py" snapshot "$PLAYBACK_HISTORY" 2>/dev/null || echo "{}")"
  jq -c --arg version "$PLAYER_VERSION" '. + {playerVersion: $version}' <<<"$playback_body"
}

ask_for_event() {
  local url body json curl_rc=0
  url="${API_BASE}/${ASK_FOR_EVENT_PATH}"
  body="$(build_event_body)"

  build_curl_auth_headers "$body"
  json="$(curl "${CURL_API_OPTS[@]}" -X POST \
    -H "Content-Type: application/json" \
    -d "$body" \
    "${curl_headers[@]}" \
    "$url")" || curl_rc=$?

  if (( curl_rc != 0 )); then
    log "WARN: askForEvent failed (curl rc=$curl_rc)"
    if internet_ping_ok; then
      log "Google reachable; treating askForEvent failure as server/API issue"
    else
      note_fetch_reach_failure "askForEvent unreachable and ping 8.8.8.8 failed"
    fi
    return 1
  fi
  note_fetch_reach_ok

  local count
  count="$(jq -r '(.response.data // []) | length' <<<"$json" 2>/dev/null || echo 0)"
  log "askForEvent: received ${count} command(s)"

  # Process sync commands (latest wins if multiple).
  local idx ts_raw sync_blast_idx
  while IFS=$'\t' read -r idx ts_raw sync_blast_idx; do
    [[ -n "$idx" ]] || continue
    [[ "$LAST_SYNC_COMMAND" == "$idx|$ts_raw|$sync_blast_idx" ]] && continue
    if arm_sync_command "$idx" "$ts_raw" "$sync_blast_idx"; then
      LAST_SYNC_COMMAND="$idx|$ts_raw|$sync_blast_idx"
    fi
  done < <(jq -r '
    (.response.data // [])[]
    | select((.type // "") == "sync")
    | [
        (.data.index // .data.Index // empty | tostring),
        (.data.timestamp // .data.Timestamp // empty | tostring),
        (.data.blastIndex // "" | tostring)
      ]
    | @tsv
  ' <<<"$json" 2>/dev/null || true)

  # Log other command types for now
  jq -r '
    (.response.data // [])[]
    | select((.type // "") != "sync")
    | "askForEvent: ignoring type=\(.type // "?")"
  ' <<<"$json" 2>/dev/null | while IFS= read -r line; do
    [[ -n "$line" ]] && log "$line"
  done || true
}

event_poll_loop() {
  log "askForEvent schedule: second ${EVENT_SLOT}/60 each minute (machine-id slot)"
  while true; do
    local wait_s
    wait_s="$(seconds_until_event_slot)"
    (( wait_s > 0 )) && sleep "$wait_s"
    ask_for_event || true
    # Leave this second so we don't double-fire before the minute rolls
    sleep 1
  done
}

cleanup_cache() {
  # Keep this helper in the script so an update does not need another runtime file.
  if ! python3 - "$ASSET_DIR" "$MAX_CACHE_MB" "${STATE_DIR}/cache-cleanup.lock" "$MAIN_LIST" "$PENDING_LIST" "$SYNC_LIST" <<'PY'
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cache cleanup: {message}")


def cleanup():
    assets = Path(sys.argv[1])
    limit_mb = int(sys.argv[2])
    if limit_mb <= 0:
        raise ValueError("MAX_CACHE_MB must be positive")
    playlists = [Path(path) for path in sys.argv[4:]]
    target_mb = limit_mb * 90 // 100
    target_bytes = target_mb * 1024 * 1024

    def usage_bytes():
        # du counts allocated space, including partial downloads and metadata.
        return int(subprocess.check_output(["du", "-sk", str(assets)]).split()[0]) * 1024

    def protected_names():
        names = {path.name for path in playlists}
        for index, playlist in enumerate(playlists):
            try:
                urls = playlist.read_text().split("\n")
            except FileNotFoundError:
                if index == 0:
                    raise  # Without the current playlist, do not guess what is safe.
                continue  # Pending/sync playlists can be absent between batches.
            for url in urls:
                # Match normalize_url + cache_path_for_url, including signed URLs.
                url = url.replace("\r", "").rstrip().rstrip(",")
                if url:
                    names.add(url.split("?", 1)[0].rsplit("/", 1)[-1])
        return names

    used = usage_bytes()
    if used <= limit_mb * 1024 * 1024:
        return

    # The publication lock keeps this snapshot valid throughout deletion. Never
    # remove metadata, partial downloads, locks, directories, or symlinks.
    protected = protected_names()
    candidates = []
    with os.scandir(assets) as entries:
        for entry in entries:
            if entry.name.endswith((".txt", ".json", ".cursor", ".tmp", ".lock")):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                candidates.append((info.st_mtime_ns, entry.name, info))
    candidates.sort(key=lambda item: (item[0], item[1]))

    log(f"{used / 1024 / 1024:.1f}MB used, trimming to {target_mb}MB")
    deleted = 0
    for _, name, original in candidates:
        if used <= target_bytes:
            # Account for concurrent downloads without rescanning after every file.
            used = usage_bytes()
            if used <= target_bytes:
                break
        path = assets / name
        if name in protected or Path(str(path) + ".lock").exists() or Path(str(path) + ".tmp").exists():
            continue
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (
                    original.st_dev, original.st_ino, original.st_mtime_ns, original.st_ctime_ns):
                continue  # Do not delete a file replaced/changed since the scan.
            path.unlink()
        except FileNotFoundError:
            continue
        used -= current.st_blocks * 512
        deleted += 1

    used = usage_bytes()
    log(f"removed {deleted} file(s); {used / 1024 / 1024:.1f}MB remaining")
    if used > target_bytes:
        log("target not reached; leaving protected/in-use files intact")


try:
    with open(sys.argv[3], "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cleanup()
except (OSError, ValueError, subprocess.SubprocessError) as error:
    print(f"Cache cleanup failed: {error}", file=sys.stderr)
    sys.exit(1)
PY
  then
    log "WARN: cache cleanup stopped; continuing playback"
  fi
}

# Only the main playback process promotes lists. Hash validation rejects mismatched metadata.
promote_main_list() {
  local source="$1"
  if [[ -f "${source}.cursor" ]]; then
    mv -f "${source}.cursor" "${MAIN_LIST}.cursor"
  else
    rm -f "${MAIN_LIST}.cursor"
  fi
  mv -f "$source" "$MAIN_LIST"
}

swap_pending_if_any() {
  if [[ -s "$PENDING_LIST" ]]; then
    log "Swap: pending -> main"
    promote_main_list "$PENDING_LIST"
    : > "$PENDING_LIST" || true
  fi

  cleanup_cache
  read_next_blast_index
  start_background_fetch_pending "$(cat "$INDEX_FILE" 2>/dev/null || echo "0")" "$blast_idx"
}

# ---------------- mpv IPC ----------------
mpv_send() {
  printf '%s\n' "$1" | socat - UNIX-CONNECT:"$MPV_SOCK" >/dev/null 2>&1 || true
}

mpv_query() {
  printf '%s\n' "$1" | socat - UNIX-CONNECT:"$MPV_SOCK" 2>/dev/null || true
}

start_mpv_if_needed() {
  wizard_active && return 0

  if [[ -S "$MPV_SOCK" ]]; then
    if ! mpv_query '{"command":["get_property","idle-active"]}' | grep -q '"data"'; then
      log "Stale mpv socket detected; restarting mpv"
      pkill -f "input-ipc-server=$MPV_SOCK" >/dev/null 2>&1 || true
      rm -f "$MPV_SOCK" || true
    else
      MPV_HAS_DISPLAY=1
      return 0
    fi
  fi

  rm -f "$MPV_SOCK" || true
  log "Starting mpv (persistent fullscreen, IPC, rotation=${ORIENTATION}°)"

  mpv --fs --no-border --really-quiet \
    --hwdec=auto \
    --mute=yes --volume=0 \
    --idle=yes --force-window=yes \
    --no-osc --cursor-autohide=always \
    --keep-open=always --keep-open-pause=no \
    --vo=gpu \
    --keepaspect=no \
    --panscan=0 \
    --no-config \
    --reset-on-next-file=no \
    --video-rotate="$ORIENTATION" \
    --input-ipc-server="$MPV_SOCK" \
    >/dev/null 2>&1 &

  # wait for socket
  for _ in {1..80}; do
    if [[ -S "$MPV_SOCK" ]]; then
      MPV_HAS_DISPLAY=1
      return 0
    fi
    sleep 0.1
  done

  log "ERROR: mpv IPC socket did not appear"
  return 1
}

mpv_get_prop() {
  local prop="$1"
  mpv_query "{\"command\":[\"get_property\",\"$prop\"]}"
}

mpv_get_prop_data() {
  local prop="$1"
  mpv_get_prop "$prop" | sed -nE 's/.*"data":[ ]*"?([^"}]*)"?[,}].*/\1/p'
}

mpv_get_duration_secs() {
  local r
  r="$(mpv_get_prop "duration")"
  echo "$r" | sed -nE 's/.*"data":[ ]*([0-9]+)(\.[0-9]+)?.*/\1/p'
}

mpv_wait_until_eof_with_timeout() {
  local timeout_secs="$1"
  local ticks=0
  local max_ticks=$((timeout_secs * 5))  # 0.2s ticks => *5

  while true; do
    wizard_active && return 0
    if sync_should_interrupt_playback; then
      wait_out_sync_deadline
      return 2
    fi
    mpv_get_prop "eof-reached" | grep -q '"data":true' && return 0
    sleep 0.2
    ticks=$((ticks+1))
    if (( ticks >= max_ticks )); then
      log "WARN: eof timeout after ${timeout_secs}s; skipping"
      mpv_send '{"command":["stop"]}'
      return 0
    fi
  done
}

# Returns 0 if something was shown, 1 if skipped (not cached yet / invalid), 2 if sync cutover.
play_url() {
  local url src playback_start
  url="$(normalize_url "$1")"

  # assert URL path has an extension (dot after the last '/')
  if [[ "${url%%\?*}" != */*.* ]]; then
    log "WARN: no extension in path, skipping: $url"
    return 1
  fi

  if ! src="$(cache_asset "$url")"; then
    log "WARN: skip (not cached yet): $url"
    return 1
  fi

  wait_while_wizard

  start_mpv_if_needed

  if is_video "$url"; then
    mpv_send '{"command":["set_property","loop-file","no"]}'
  else
    mpv_send '{"command":["set_property","loop-file","inf"]}'
  fi

  if ! playback_start="$(python3 "$SCRIPT_DIR/playback_report.py" load "$MPV_SOCK" "$src" "$MAIN_LIST" "$item_position" "$PLAYBACK_HISTORY")"; then
    playback_start=""
    log "WARN: playback reporting unavailable; loading without telemetry: $url"
    mpv_send "$(jq -nc --arg src "$src" '{command:["loadfile",$src,"replace"]}')"
  fi
  log "DBG: want_src=$(printf '%q' "$src") mpv_path=$(mpv_get_prop_data path) mpv_filename=$(mpv_get_prop_data filename)"

  if is_video "$url"; then
    local dur wait_rc=0
    dur="$(mpv_get_duration_secs || true)"
    if [[ -n "$dur" && "$dur" -gt 0 ]]; then
      mpv_wait_until_eof_with_timeout $((dur + 10)) || wait_rc=$?
    else
      mpv_wait_until_eof_with_timeout $((5 * 60)) || wait_rc=$?
    fi
    (( wait_rc == 2 )) && return 2
  else
    local image_wait_rc=0
    python3 "$SCRIPT_DIR/playback_report.py" wait-image "$playback_start" "$IMAGE_SECONDS" \
      "$WIZARD_LOCK" "$SYNC_AT_FILE" "$SYNC_LIST" || image_wait_rc=$?
    if (( image_wait_rc == 2 )); then
      wait_out_sync_deadline
      return 2
    elif (( image_wait_rc == 0 )); then
      return 0
    fi

    # Keep playback functional if the helper is missing during an update or fails.
    log "WARN: image deadline timer unavailable; using fallback timer"
    local i=0 ticks=$((IMAGE_SECONDS * 5))
    while (( i < ticks )); do
      wizard_active && return 0
      if sync_should_interrupt_playback; then
        wait_out_sync_deadline
        return 2
      fi
      sleep 0.2
      i=$((i + 1))
    done
  fi
  return 0
}

chromium_running() {
  pgrep -f "/usr/bin/chromium" >/dev/null 2>&1
}

x_display_running() {
  pgrep -f "X :0" >/dev/null 2>&1 || pgrep -f "Xorg :0" >/dev/null 2>&1
}

kiosk_startx_alive() {
  [[ -n "${CHROMIUM_PID:-}" ]] && kill -0 "$CHROMIUM_PID" 2>/dev/null
}

launch_web_kiosk() {
  local web_content="$1"
  local api_host
  api_host="$(echo "$API_BASE" | sed -E 's|^https?://||; s|/.*||')"
  local kiosk_url="https://${api_host}/player/${ORIENTATION}/${web_content}?id=${WEB_STATION}&secret=${DEVICE_SECRET}"

  if kiosk_startx_alive && chromium_running; then
    return 0
  fi
  # Background network recovery may have already relaunched Chromium; don't start a second one.
  if chromium_running; then
    return 0
  fi

  # Chromium gone but X still holding :0 — startx would fail until reboot without this.
  if x_display_running || kiosk_startx_alive; then
    log "WARN: Chromium not running but X/startx still present; clearing stale session"
    kill_web_kiosk
    sleep 1
  fi

  log "Launching Chromium kiosk: $kiosk_url"

  rm -rf ~/.cache/chromium ~/.config/chromium

  # --disable-dev-shm-usage: Pi /dev/shm is often too small; Chromium crashes without it.
  startx /usr/bin/chromium \
    --kiosk \
    --start-fullscreen \
    --window-position=0,0 \
    --window-size=1920,1080 \
    --force-device-scale-factor=1 \
    --noerrdialogs \
    --no-first-run \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-dev-shm-usage \
    "$kiosk_url" \
    -- :0 -nocursor -s off &
  CHROMIUM_PID=$!

  # Wait for X to accept connections, then disable blanking/DPMS.
  # (Immediate xset after startx races and silently fails.)
  (
    export DISPLAY=:0
    export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
    for _ in {1..50}; do
      if xset q >/dev/null 2>&1; then
        xset s off
        xset s noblank
        xset -dpms
        xset dpms 0 0 0
        log "Display blanking/DPMS disabled"
        exit 0
      fi
      sleep 0.2
    done
    log "WARN: could not disable display blanking (X not ready)"
  ) &
}

kill_web_kiosk() {
  if kiosk_startx_alive; then
    log "Stopping Chromium kiosk"
    kill "$CHROMIUM_PID" 2>/dev/null || true
    wait "$CHROMIUM_PID" 2>/dev/null || true
  fi
  CHROMIUM_PID=""
  pkill -f "/usr/bin/chromium" >/dev/null 2>&1 || true
  pkill -f "X :0" >/dev/null 2>&1 || true
  pkill -f "Xorg :0" >/dev/null 2>&1 || true
  sleep 0.5
}

# Full process restart: cleanup trap tears down mpv/X; systemd Restart= brings us back cold
# (X then mpv) — avoids mid-run startx fighting DRM mpv.
restart_player() {
  local reason="${1:-kiosk unhealthy}"
  log "WARN: $reason; exiting so systemd can restart the player"
  kill -TERM "$PLAYER_PID" 2>/dev/null || exit 1
  exit 1
}

# After Wi-Fi recovery, do a clean player restart rather than startx on a live DRM session.
restart_web_kiosk_if_needed() {
  wizard_active && return 0
  if [[ ! -f "$WEB_CONTENT_FILE" ]]; then
    return 0
  fi
  restart_player "network recovered; refreshing kiosk via full player restart"
}

# Set after the first intentional kiosk launch so "not running" means crash, not cold start.
KIOSK_BOOTSTRAPPED=""
# Set once mpv has taken DRM. Bringing Chromium up after that needs a full player restart.
MPV_HAS_DISPLAY=""

# Watchdog: launch once at boot; if Chromium dies later, nuke the whole player.
ensure_web_kiosk_healthy() {
  wizard_active && return 0

  if [[ ! -f "$WEB_CONTENT_FILE" ]]; then
    if chromium_running || x_display_running || kiosk_startx_alive; then
      kill_web_kiosk
    fi
    KIOSK_BOOTSTRAPPED=""
    return 0
  fi

  if chromium_running; then
    KIOSK_BOOTSTRAPPED=1
    if ! kiosk_startx_alive; then
      CHROMIUM_PID=""
    fi
    return 0
  fi

  # Cold start (before mpv): launch X first. After mpv owns DRM, or if Chromium
  # crashed, systemd restart gives a clean X-then-mpv boot — no startx fight.
  if [[ -n "$KIOSK_BOOTSTRAPPED" ]]; then
    restart_player "Chromium kiosk not running (crash or exit)"
  fi
  if [[ -n "$MPV_HAS_DISPLAY" ]]; then
    restart_player "web content enabled; restarting player so kiosk can take the display"
  fi

  launch_web_kiosk "$(cat "$WEB_CONTENT_FILE")"
  KIOSK_BOOTSTRAPPED=1
}

sync_web_kiosk() {
  wizard_active && return 0

  if [[ -f "$WEB_CONTENT_FILE" ]]; then
    ensure_web_kiosk_healthy
  else
    kill_web_kiosk
    KIOSK_BOOTSTRAPPED=""
  fi
}

main() {
  ensure_dirs
  start_wifi_hotkey
  clear_sync_pending
  rm -f "$PLAYBACK_HISTORY"

  log "Player version=${PLAYER_VERSION}"
  log "Device EVENT_SLOT=${EVENT_SLOT} (askForEvent during second ${EVENT_SLOT} of each minute)"
  event_poll_loop &
  disown || true

  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  if fetch_batch_to "$idx" "$blast_idx" "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; then
    promote_main_list "$PENDING_LIST"
    mv "$NEXT_FILE" "$INDEX_FILE"
    read_next_blast_index
  else
    if [[ -s "$MAIN_LIST" ]]; then
      log "Fetch failed at startup; using persisted MAIN_LIST"
    else
      log "No persisted MAIN_LIST; retrying initial fetch..."
      until fetch_batch_to "$idx" "$blast_idx" "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; do
        sleep 5
        idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
      done
      promote_main_list "$PENDING_LIST"
      mv "$NEXT_FILE" "$INDEX_FILE"
      read_next_blast_index
    fi
  fi

  sync_web_kiosk

  start_background_fetch_pending "$(cat "$INDEX_FILE" 2>/dev/null || echo "0")" "$blast_idx"

  start_mpv_if_needed

  while true; do
    wait_while_wizard

    # If a sync deadline is near/past, wait it out and cut over before more ads.
    if sync_should_interrupt_playback; then
      wait_out_sync_deadline
    fi
    if apply_sync_if_due; then
      sync_web_kiosk
      continue
    fi

    if [[ ! -s "$MAIN_LIST" ]]; then
      log "WARN: main list empty; refetching..."
      idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
      if fetch_batch_to "$idx" "$blast_idx" "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; then
        promote_main_list "$PENDING_LIST"
        mv "$NEXT_FILE" "$INDEX_FILE"
        read_next_blast_index
      else
        log "No images available; waiting 60s before retry..."
        local waited=0
        while (( waited < 60 )); do
          if sync_should_interrupt_playback; then
            break
          fi
          sleep 1
          waited=$((waited + 1))
        done
      fi
      sync_web_kiosk
      continue
    fi

    local n played_any=0 play_rc=0 item_position=0
    n="$(wc -l < "$MAIN_LIST" | tr -d ' ')"
    log "Playing batch ($n items)"

    while IFS= read -r url; do
      wait_while_wizard
      if sync_should_interrupt_playback; then
        wait_out_sync_deadline
        break
      fi
      if apply_sync_if_due; then
        played_any=0
        break
      fi
      url="$(normalize_url "$url")"
      [[ -n "$url" ]] || continue
      item_position=$((item_position + 1))
      play_rc=0
      play_url "$url" || play_rc=$?
      python3 "$SCRIPT_DIR/playback_report.py" finish "$PLAYBACK_HISTORY" || true
      if (( play_rc == 2 )); then
        # Sync interrupt during asset; apply and restart batch
        apply_sync_if_due || true
        played_any=0
        break
      elif (( play_rc == 0 )); then
        played_any=1
      fi
    done < "$MAIN_LIST"

    if apply_sync_if_due; then
      sync_web_kiosk
      continue
    fi

    # Keep looping the current batch while assets are still downloading so the
    # display stays on playable creatives instead of freezing on a blocking curl.
    if batch_has_downloads_in_progress; then
      # Don't wait out curl max-time for Wi-Fi recovery: probe each pass.
      if ! internet_ping_ok; then
        note_fetch_reach_failure "ping 8.8.8.8 failed while assets downloading"
        abort_in_flight_asset_downloads
      fi
      if (( ! played_any )); then
        log "Waiting for cached assets before first play..."
        sleep 3
      else
        log "Downloads still running; looping current batch"
      fi
      continue
    fi

    if (( ! played_any )); then
      log "No playable assets in batch; waiting before retry..."
      sleep 5
      continue
    fi

    swap_pending_if_any
    sync_web_kiosk
  done
}

main

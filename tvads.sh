#!/usr/bin/env bash 
set -euo pipefail

CONFIG="/data/player/config.env"
STATE_DIR="/tmp/player/state"
ASSET_DIR="/data/assets"

MAIN_LIST="${ASSET_DIR}/main.txt"
PENDING_LIST="${STATE_DIR}/pending.txt"
INDEX_FILE="${STATE_DIR}/index.txt"
NEXT_FILE="${STATE_DIR}/next.txt"
NEXT_BLAST_FILE="${STATE_DIR}/nextblast.txt"
WEB_CONTENT_FILE="${STATE_DIR}/webcontent.txt"

VIEW_PATH="view/billboard"

# mpv IPC socket (lives in RAM; fine)
MPV_SOCK="/tmp/venditt-mpv.sock"
CHROMIUM_PID=""

cleanup() {
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
MAX_CACHE_MB="${MAX_CACHE_MB:-30000}" # 30GB
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
CURL_ASSET_OPTS=(--fail --silent --show-error --connect-timeout 5 --max-time 20 -L)
JQ_URLS='.response.data[]?.url // empty'
JQ_INDEX='.response.index // .response.message // empty'
JQ_BLAST='.response.blastIndex // empty'
JQ_WEBCONTENT='.response.webContent // empty'

log(){ echo "[$(date '+%F %T')] $*"; }

blast_idx=0

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
  local json
  if ! json="$(curl "${CURL_API_OPTS[@]}" "${curl_headers[@]}" "$url")"; then
    log "WARN: fetch failed"
    return 1
  fi

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

  printf "%s\n" "$urls" > "$out"
  [[ -n "$next" ]] && echo "$next" > "$nextfile" || echo "$idx" > "$nextfile"
  [[ -n "$next_blast" ]] && echo "$next_blast" > "$nextblastfile" || echo "$blast_idx" > "$nextblastfile"
  log "OK: $(wc -l < "$out" | tr -d ' ') assets, nextIndex=$(cat "$nextfile") nextBlastIndex=$(cat "$nextblastfile")"
}

cache_path_for_url() {
  local url="$1"
  local base="${url%%\?*}"
  local filename="${base##*/}"
  echo "${ASSET_DIR}/${filename}"
}

cache_asset() {
  local url="$1"
  local path tmp
  path="$(cache_path_for_url "$url")"
  tmp="${path}.tmp"

  if [[ -s "$path" ]]; then
    printf '%s\n' "$path"
    return 0
  fi

  build_curl_auth_headers ""
  if curl "${CURL_ASSET_OPTS[@]}" "${curl_headers[@]}" -o "$tmp" "$url"; then
    mv -f "$tmp" "$path"
    printf '%s\n' "$path"
    return 0
  else
    rm -f "$tmp" >/dev/null 2>&1 || true
    log "WARN: download failed: $url"
    return 1
  fi
}

background_fetch_pending() {
  local idx="$1"
  local bidx="$2"
  if fetch_batch_to "$idx" "$bidx" "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; then
    mv "$NEXT_FILE" "$INDEX_FILE"
  else
    rm -f "$NEXT_FILE" "$NEXT_BLAST_FILE" || true
  fi
}

read_next_blast_index() {
  if [[ -f "$NEXT_BLAST_FILE" ]]; then
    blast_idx="$(cat "$NEXT_BLAST_FILE")"
    rm -f "$NEXT_BLAST_FILE"
  fi
}

cleanup_cache() {
  local used_mb
  used_mb=$(du -sm "$ASSET_DIR" | awk '{print $1}')

  if (( used_mb <= MAX_CACHE_MB )); then
    return
  fi

  log "Cache cleanup: ${used_mb}MB used, trimming to ${MAX_CACHE_MB}MB"

  find "$ASSET_DIR" -type f ! -name 'main.txt' -printf '%T@ %p\n' \
    | sort -n \
    | while read -r _ file; do
        rm -f "$file"
        used_mb=$(du -sm "$ASSET_DIR" | awk '{print $1}')
        (( used_mb <= MAX_CACHE_MB )) && break
      done
}

swap_pending_if_any() {
  if [[ -s "$PENDING_LIST" ]]; then
    log "Swap: pending -> main"
    mv "$PENDING_LIST" "$MAIN_LIST"
    : > "$PENDING_LIST" || true
  fi

  cleanup_cache
  read_next_blast_index
  background_fetch_pending "$(cat "$INDEX_FILE" 2>/dev/null || echo "0")" "$blast_idx" & disown || true
}

# ---------------- mpv IPC ----------------
mpv_send() {
  printf '%s\n' "$1" | socat - UNIX-CONNECT:"$MPV_SOCK" >/dev/null 2>&1 || true
}

mpv_query() {
  printf '%s\n' "$1" | socat - UNIX-CONNECT:"$MPV_SOCK" 2>/dev/null || true
}

start_mpv_if_needed() {
  if [[ -S "$MPV_SOCK" ]]; then
    if ! mpv_query '{"command":["get_property","idle-active"]}' | grep -q '"data"'; then
      log "Stale mpv socket detected; restarting mpv"
      pkill -f "input-ipc-server=$MPV_SOCK" >/dev/null 2>&1 || true
      rm -f "$MPV_SOCK" || true
    else
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
    [[ -S "$MPV_SOCK" ]] && return 0
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

play_url() {
  local url src
  url="$(normalize_url "$1")"

  # assert URL path has an extension (dot after the last '/')
  if [[ "${url%%\?*}" != */*.* ]]; then
    log "WARN: no extension in path, skipping: $url"
    return 0
  fi

  if ! src="$(cache_asset "$url")"; then
    log "WARN: skip (cache_asset failed): $url"
    return 0
  fi

  start_mpv_if_needed

  if is_video "$url"; then
    mpv_send '{"command":["set_property","loop-file","no"]}'
  else
    mpv_send '{"command":["set_property","loop-file","inf"]}'
  fi

  mpv_send "{\"command\":[\"loadfile\",\"$src\",\"replace\"]}"
  log "DBG: want_src=$(printf '%q' "$src") mpv_path=$(mpv_get_prop_data path) mpv_filename=$(mpv_get_prop_data filename)"

  if is_video "$url"; then
    local dur
    dur="$(mpv_get_duration_secs || true)"
    if [[ -n "$dur" && "$dur" -gt 0 ]]; then
      mpv_wait_until_eof_with_timeout $((dur + 10))
    else
      mpv_wait_until_eof_with_timeout $((5 * 60))
    fi
  else
    sleep "$IMAGE_SECONDS"
  fi
}

launch_web_kiosk() {
  local web_content="$1"
  local api_host
  api_host="$(echo "$API_BASE" | sed -E 's|^https?://||; s|/.*||')"
  local kiosk_url="https://${api_host}/player/${ORIENTATION}/${web_content}?id=${WEB_STATION}"

  if [[ -n "$CHROMIUM_PID" ]] && kill -0 "$CHROMIUM_PID" 2>/dev/null; then
    return 0
  fi

  log "Launching Chromium kiosk: $kiosk_url"

  # Run xset inside the X session so it can't race startx / miss XAUTHORITY.
  # Also pass -s off to the X server so screensaver is disabled at startup.
  startx /bin/bash -c "
    xset s off
    xset s noblank
    xset -dpms
    xset dpms 0 0 0
    exec /usr/bin/chromium \
      --kiosk \
      --start-fullscreen \
      --window-position=0,0 \
      --window-size=1920,1080 \
      --force-device-scale-factor=1 \
      --noerrdialogs \
      --no-first-run \
      --disable-infobars \
      --disable-session-crashed-bubble \
      $(printf '%q' "$kiosk_url")
  " -- :0 -nocursor -s off &
  CHROMIUM_PID=$!
}

kill_web_kiosk() {
  if [[ -n "$CHROMIUM_PID" ]] && kill -0 "$CHROMIUM_PID" 2>/dev/null; then
    log "Stopping Chromium kiosk"
    kill "$CHROMIUM_PID" 2>/dev/null || true
    wait "$CHROMIUM_PID" 2>/dev/null || true
    CHROMIUM_PID=""
  fi
  pkill -f "/usr/bin/chromium" >/dev/null 2>&1 || true
  pkill -f "X :0" >/dev/null 2>&1 || true
  pkill -f "Xorg :0" >/dev/null 2>&1 || true
}

sync_web_kiosk() {
  if [[ -f "$WEB_CONTENT_FILE" ]]; then
    launch_web_kiosk "$(cat "$WEB_CONTENT_FILE")"
  else
    kill_web_kiosk
  fi
}

main() {
  ensure_dirs

  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  if fetch_batch_to "$idx" "$blast_idx" "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; then
    mv "$PENDING_LIST" "$MAIN_LIST"
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
      mv "$PENDING_LIST" "$MAIN_LIST"
      mv "$NEXT_FILE" "$INDEX_FILE"
      read_next_blast_index
    fi
  fi

  sync_web_kiosk

  background_fetch_pending "$(cat "$INDEX_FILE" 2>/dev/null || echo "0")" "$blast_idx" & disown || true

  start_mpv_if_needed

  while true; do
    if [[ ! -s "$MAIN_LIST" ]]; then
      log "WARN: main list empty; refetching..."
      idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
      if fetch_batch_to "$idx" "$blast_idx" "$PENDING_LIST" "$NEXT_FILE" "$NEXT_BLAST_FILE"; then
        mv "$PENDING_LIST" "$MAIN_LIST"
        mv "$NEXT_FILE" "$INDEX_FILE"
        read_next_blast_index
      else
        log "No images available; waiting 60s before retry..."
        sleep 60
      fi
      sync_web_kiosk
      continue
    fi

    local n
    n="$(wc -l < "$MAIN_LIST" | tr -d ' ')"
    log "Playing batch ($n items)"

    while IFS= read -r url; do
      url="$(normalize_url "$url")"
      [[ -n "$url" ]] || continue
      play_url "$url"
    done < "$MAIN_LIST"

    swap_pending_if_any
    sync_web_kiosk
  done
}

main

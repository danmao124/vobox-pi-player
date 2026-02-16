#!/usr/bin/env bash 
set -euo pipefail

CONFIG="/data/player/config.env"
STATE_DIR="/tmp/player/state"
ASSET_DIR="/data/assets"

MAIN_LIST="${STATE_DIR}/main.txt"
PENDING_LIST="${STATE_DIR}/pending.txt"
INDEX_FILE="${STATE_DIR}/index.txt"
NEXT_FILE="${STATE_DIR}/next.txt"

VIEW_PATH="view/billboard"

# mpv IPC socket (lives in RAM; fine)
MPV_SOCK="/tmp/venditt-mpv.sock"

cleanup() {
  # ask mpv to quit nicely; then hard kill if needed
  if [[ -S "$MPV_SOCK" ]]; then
    printf '%s\n' '{"command":["quit"]}' | socat - UNIX-CONNECT:"$MPV_SOCK" >/dev/null 2>&1 || true
  fi
  pkill -f "input-ipc-server=$MPV_SOCK" >/dev/null 2>&1 || true
  rm -f "$MPV_SOCK" >/dev/null 2>&1 || true
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
JQ_NEXT='.response.message // empty'

log(){ echo "[$(date '+%F %T')] $*"; }

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
  local out="$2"
  local nextfile="$3"

  local url="${API_BASE}/${VIEW_PATH}?id=${ID}&index=${idx}"
  log "Fetch: $url"

  build_curl_auth_headers ""
  local json
  if ! json="$(curl "${CURL_API_OPTS[@]}" "${curl_headers[@]}" "$url")"; then
    log "WARN: fetch failed"
    return 1
  fi

  local urls next
  # normalize lines coming from API (fixes "a.png," and CRLF issues)
  urls="$(jq -r "$JQ_URLS" <<<"$json" \
    | sed -E 's/\r$//; s/[[:space:]]+$//; s/,+$//; /^$/d' || true)"
  next="$(jq -r "$JQ_NEXT" <<<"$json" | sed '/^$/d' || true)"

  if [[ -z "$urls" ]]; then
    log "WARN: no urls in response"
    return 1
  fi

  printf "%s\n" "$urls" > "$out"
  [[ -n "$next" ]] && echo "$next" > "$nextfile" || echo "$idx" > "$nextfile"
  log "OK: $(wc -l < "$out" | tr -d ' ') assets, nextIndex=$(cat "$nextfile")"
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
  local idx
  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  if fetch_batch_to "$idx" "$PENDING_LIST" "$NEXT_FILE"; then
    mv "$NEXT_FILE" "$INDEX_FILE"
  else
    rm -f "$NEXT_FILE" || true
  fi
}

cleanup_cache() {
  local used_mb
  used_mb=$(du -sm "$ASSET_DIR" | awk '{print $1}')

  if (( used_mb <= MAX_CACHE_MB )); then
    return
  fi

  log "Cache cleanup: ${used_mb}MB used, trimming to ${MAX_CACHE_MB}MB"

  find "$ASSET_DIR" -type f -printf '%T@ %p\n' \
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
  background_fetch_pending & disown || true
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

main() {
  ensure_dirs

  local idx
  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  until fetch_batch_to "$idx" "$MAIN_LIST" "$NEXT_FILE"; do
    log "Retry initial fetch in 5s..."
    sleep 5
    idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  done
  mv "$NEXT_FILE" "$INDEX_FILE"

  background_fetch_pending & disown || true

  start_mpv_if_needed

  while true; do
    if [[ ! -s "$MAIN_LIST" ]]; then
      log "WARN: main list empty; refetching..."
      idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
      fetch_batch_to "$idx" "$MAIN_LIST" "$NEXT_FILE" && mv "$NEXT_FILE" "$INDEX_FILE" || sleep 2
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
  done
}

main

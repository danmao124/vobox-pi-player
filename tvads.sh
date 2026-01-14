#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/player/config.env"
STATE_DIR="/data/player/state"
ASSET_DIR="/data/assets"

MAIN_LIST="${STATE_DIR}/main.txt"
PENDING_LIST="${STATE_DIR}/pending.txt"
INDEX_FILE="${STATE_DIR}/index.txt"
NEXT_FILE="${STATE_DIR}/next.txt"

VIEW_PATH="view/billboard"

# mpv IPC socket (lives in RAM; fine)
MPV_SOCK="/tmp/venditt-mpv.sock"

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

CURL_OPTS=(--fail --silent --show-error --connect-timeout 5 --max-time 20 -L)
JQ_URLS='.response.data[]?.url // empty'
JQ_NEXT='.response.message // empty'

log(){ echo "[$(date '+%F %T')] $*"; }

is_video() {
  local u="${1,,}"
  [[ "$u" == *".mp4"* || "$u" == *".webm"* ]]
}

ensure_dirs() {
  mkdir -p "$STATE_DIR" "$ASSET_DIR"
  [[ -f "$INDEX_FILE" ]] || echo "0" > "$INDEX_FILE"
  [[ -f "$MAIN_LIST"  ]] || : > "$MAIN_LIST"
  [[ -f "$PENDING_LIST" ]] || : > "$PENDING_LIST"
}

curl_headers=()
if [[ "${AUTH_HEADER:-}" != "" ]]; then
  curl_headers=(-H "$AUTH_HEADER")
fi

fetch_batch_to() {
  local idx="$1"
  local out="$2"
  local nextfile="$3"

  local url="${API_BASE}/${VIEW_PATH}?id=${ID}&index=${idx}"
  log "Fetch: $url"

  local json
  if ! json="$(curl "${CURL_OPTS[@]}" "${curl_headers[@]}" "$url")"; then
    log "WARN: fetch failed"
    return 1
  fi

  local urls next
  urls="$(jq -r "$JQ_URLS" <<<"$json" | sed '/^$/d' || true)"
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
  local ext=""
  if [[ "$url" =~ \.([A-Za-z0-9]{2,5})(\?|$) ]]; then
    ext=".${BASH_REMATCH[1]}"
  fi
  local hash
  hash="$(printf "%s" "$url" | sha256sum | awk '{print $1}')"
  echo "${ASSET_DIR}/${hash}${ext}"
}

cache_asset() {
  local url="$1"
  local path tmp
  path="$(cache_path_for_url "$url")"
  tmp="${path}.tmp"

  if [[ -s "$path" ]]; then
    echo "$path"
    return 0
  fi

  if curl "${CURL_OPTS[@]}" "${curl_headers[@]}" -o "$tmp" "$url"; then
    mv "$tmp" "$path"
    echo "$path"
  else
    rm -f "$tmp"
    log "WARN: download failed, streaming: $url"
    echo "$url"
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

mpv_get_idle() {
  local r
  r="$(mpv_query '{"command":["get_property","idle-active"]}')"
  echo "$r" | grep -q '"data":true'
}

start_mpv_if_needed() {
  if [[ -S "$MPV_SOCK" ]]; then
    return 0
  fi

  rm -f "$MPV_SOCK" || true
  log "Starting mpv (persistent fullscreen, IPC)"

  mpv --fs --no-border --really-quiet \
      --hwdec=auto \
      --mute=yes --volume=0 \
      --idle=yes --force-window=yes \
      --no-osc --cursor-autohide=always \
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

mpv_wait_until_not_idle() {
  # Wait until playback actually starts (idle-active becomes false).
  # Prevents instant "finished" on load failure.
  for _ in {1..100}; do
    if ! mpv_get_idle; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

mpv_wait_until_idle() {
  # Wait until playback ends (idle-active becomes true again)
  while true; do
    if mpv_get_idle; then
      return 0
    fi
    sleep 0.25
  done
}

play_url() {
  local url="$1"
  local src
  src="$(cache_asset "$url")"

  start_mpv_if_needed

  # Load file into the already-open fullscreen window
  mpv_send "{\"command\":[\"loadfile\",\"$src\",\"replace\"]}"

  # If it never leaves idle, the file probably failed; skip.
  if ! mpv_wait_until_not_idle; then
    log "WARN: playback did not start, skipping: $url"
    return 0
  fi

  if is_video "$url"; then
    # video ends naturally -> mpv returns to idle
    mpv_wait_until_idle
  else
    # images don't end -> we control timing then stop
    sleep "$IMAGE_SECONDS"
    mpv_send '{"command":["stop"]}'
    mpv_wait_until_idle
  fi
}

main() {
  ensure_dirs

  # initial main fetch
  local idx
  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  until fetch_batch_to "$idx" "$MAIN_LIST" "$NEXT_FILE"; do
    log "Retry initial fetch in 5s..."
    sleep 5
    idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  done
  mv "$NEXT_FILE" "$INDEX_FILE"

  background_fetch_pending & disown || true

  # Start mpv once, forever
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
      [[ -n "$url" ]] || continue
      play_url "$url"
    done < "$MAIN_LIST"

    # end of batch -> swap pending and continue
    swap_pending_if_any
  done
}

main

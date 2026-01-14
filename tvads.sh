#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/player/config.env"
STATE_DIR="/data/player/state"
ASSET_DIR="/data/assets"

MAIN_LIST="${STATE_DIR}/main.txt"
PENDING_LIST="${STATE_DIR}/pending.txt"
INDEX_FILE="${STATE_DIR}/index.txt"
NEXT_FILE="${STATE_DIR}/next.txt"
MPV_PLAYLIST="${STATE_DIR}/mpv.m3u"

VIEW_PATH="view/billboard"
MPV_SOCK="/tmp/venditt-mpv.sock"

# A flag file that means: "API wrapped, do a swap/reload once"
WRAP_FLAG="${STATE_DIR}/wrap.flag"

cleanup() {
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

CURL_OPTS=(--fail --silent --show-error --connect-timeout 3 --max-time 20 -L)
JQ_URLS='.response.data[]?.url // empty'
JQ_NEXT='.response.message // empty'

log(){ echo "[$(date '+%F %T')] $*"; }

ensure_dirs() {
  mkdir -p "$STATE_DIR" "$ASSET_DIR"
  [[ -f "$MAIN_LIST"  ]] || : > "$MAIN_LIST"
  [[ -f "$PENDING_LIST" ]] || : > "$PENDING_LIST"
  [[ -f "$INDEX_FILE" ]] || echo "0" > "$INDEX_FILE"
}

curl_headers=()
if [[ "${AUTH_HEADER:-}" != "" ]]; then
  curl_headers=(-H "$AUTH_HEADER")
fi

is_int() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

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

cleanup_cache() {
  local used_mb
  used_mb=$(du -sm "$ASSET_DIR" | awk '{print $1}')
  (( used_mb <= MAX_CACHE_MB )) && return

  log "Cache cleanup: ${used_mb}MB used, trimming to ${MAX_CACHE_MB}MB"
  find "$ASSET_DIR" -type f -printf '%T@ %p\n' \
    | sort -n \
    | while read -r _ file; do
        rm -f "$file"
        used_mb=$(du -sm "$ASSET_DIR" | awk '{print $1}')
        (( used_mb <= MAX_CACHE_MB )) && break
      done
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
    if mpv_query '{"command":["get_property","idle-active"]}' | grep -q '"data"'; then
      return 0
    fi
    log "Stale mpv socket detected; restarting mpv"
    pkill -f "input-ipc-server=$MPV_SOCK" >/dev/null 2>&1 || true
    rm -f "$MPV_SOCK" || true
  fi

  rm -f "$MPV_SOCK" || true
  log "Starting mpv (fullscreen, infinite playlist loop, IPC)"

  mpv --fs --no-border --really-quiet \
      --hwdec=auto \
      --mute=yes --volume=0 \
      --keep-open=yes \
      --loop-playlist=inf \
      --image-display-duration="$IMAGE_SECONDS" \
      --idle=yes --force-window=yes \
      --no-osc --cursor-autohide=always \
      --input-ipc-server="$MPV_SOCK" \
      >/dev/null 2>&1 &

  for _ in {1..80}; do
    [[ -S "$MPV_SOCK" ]] && return 0
    sleep 0.1
  done

  log "ERROR: mpv IPC socket did not appear"
  return 1
}

build_m3u_from_list() {
  local listfile="$1"
  local outm3u="$2"
  : > "$outm3u"
  while IFS= read -r url; do
    [[ -n "$url" ]] || continue
    echo "$(cache_asset "$url")" >> "$outm3u"
  done < "$listfile"
}

reload_playlist_from_main() {
  build_m3u_from_list "$MAIN_LIST" "$MPV_PLAYLIST"
  start_mpv_if_needed
  log "Reload playlist from MAIN: $(wc -l < "$MPV_PLAYLIST" | tr -d ' ') items (start at 0)"
  mpv_send "{\"command\":[\"loadlist\",\"$MPV_PLAYLIST\",\"replace\"]}"
  mpv_send '{"command":["set_property","playlist-pos",0]}'
  mpv_send '{"command":["set_property","time-pos",0]}'
}

# Fetch pending once. If API wrapped (next < idx) then set WRAP_FLAG.
fetch_pending_once() {
  local idx next
  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  is_int "$idx" || idx="0"

  if ! fetch_batch_to "$idx" "$PENDING_LIST" "$NEXT_FILE"; then
    rm -f "$NEXT_FILE" || true
    return 1
  fi

  next="$(cat "$NEXT_FILE" 2>/dev/null || echo "$idx")"
  is_int "$next" || next="$idx"

  # Update index file to whatever API says next is
  echo "$next" > "$INDEX_FILE"

  # WRAP detection: nextIndex < queriedIndex
  if (( next < idx )); then
    log "WRAP detected: queriedIndex=$idx -> nextIndex=$next"
    : > "$WRAP_FLAG"
  fi

  return 0
}

swap_pending_into_main() {
  if [[ -s "$PENDING_LIST" ]]; then
    log "Swap: pending -> main"
    mv "$PENDING_LIST" "$MAIN_LIST"
    : > "$PENDING_LIST" || true
  else
    log "WARN: wrap hit but pending list empty; nothing to swap"
  fi
}

main() {
  ensure_dirs

  # Always start API index at 0 when the script starts
  echo "0" > "$INDEX_FILE"
  rm -f "$WRAP_FLAG" || true

  # Initial fetch into MAIN from index 0
  local idx="0"
  until fetch_batch_to "$idx" "$MAIN_LIST" "$NEXT_FILE"; do
    log "Retry initial fetch in 5s..."
    sleep 5
    idx="0"
  done

  # Move API nextIndex to INDEX_FILE (for pending fetches)
  cat "$NEXT_FILE" > "$INDEX_FILE" || echo "0" > "$INDEX_FILE"
  rm -f "$NEXT_FILE" || true

  start_mpv_if_needed
  reload_playlist_from_main

  # Main loop:
  # - keep fetching pending forward using INDEX_FILE
  # - when wrap detected (nextIndex < idx), swap+reload ONCE and clear WRAP_FLAG
  while true; do
    cleanup_cache

    # fetch pending periodically
    fetch_pending_once || true

    # only reload when API wrap happens
    if [[ -f "$WRAP_FLAG" ]]; then
      rm -f "$WRAP_FLAG" || true
      swap_pending_into_main
      reload_playlist_from_main
      # immediately start collecting the next cycle's pending
      : > "$PENDING_LIST" || true
    fi

    sleep 5
  done
}

main

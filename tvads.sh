#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/player/config.env"
STATE_DIR="/data/player/state"
ASSET_DIR="/data/assets"

MAIN_LIST="${STATE_DIR}/main.txt"
PENDING_LIST="${STATE_DIR}/pending.txt"
INDEX_FILE="${STATE_DIR}/index.txt"

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
RESTART_HOURS="${RESTART_HOURS:-24}"
MAX_CACHE_MB="${MAX_CACHE_MB:-30000}" # 30GB
RESTART_SECONDS=$((RESTART_HOURS*60*60))

VIEW_PATH="view/billboard"

CURL_OPTS=(--fail --silent --show-error --connect-timeout 5 --max-time 20 -L)
JQ_URLS='.response.data[]?.url // empty'
JQ_NEXT='.response.message // empty'

MPV_COMMON=(--fs --no-border --really-quiet --keep-open=no --hwdec=auto --mute=yes --volume=0)
MPV_IMG=(--image-display-duration="${IMAGE_SECONDS}")

log(){ echo "[$(date '+%F %T')] $*"; }

is_video() {
  local u="${1,,}"
  [[ "$u" == *".mp4"* || "$u" == *".webm"* ]]
}

ensure_dirs() {
  mkdir -p "$STATE_DIR" "$ASSET_DIR"
  [[ -f "$INDEX_FILE" ]] || echo "0" > "$INDEX_FILE"
  [[ -f "$MAIN_LIST" ]] || : > "$MAIN_LIST"
  [[ -f "$PENDING_LIST" ]] || : > "$PENDING_LIST"
}

curl_headers=()
if [[ "${AUTH_HEADER:-}" != "" ]]; then
  curl_headers=(-H "$AUTH_HEADER")
fi

fetch_batch_to() {
  # args: index outfile nextfile
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
  # prints local path if downloaded, else original url
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

play_one() {
  local url="$1"
  local src
  src="$(cache_asset "$url")"

  if is_video "$url"; then
    log "PLAY video: $url"
    mpv "${MPV_COMMON[@]}" "$src" || log "WARN: mpv video failed"
  else
    log "SHOW image ${IMAGE_SECONDS}s: $url"
    mpv "${MPV_COMMON[@]}" "${MPV_IMG[@]}" "$src" || log "WARN: mpv image failed"
  fi
}

background_fetch_pending() {
  local idx
  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  local nextfile="${STATE_DIR}/next.txt"
  if fetch_batch_to "$idx" "$PENDING_LIST" "$nextfile"; then
    mv "$nextfile" "$INDEX_FILE"
  else
    rm -f "$nextfile" || true
  fi
}

cleanup_cache() {
  local used_mb
  used_mb=$(du -sm "$ASSET_DIR" | awk '{print $1}')

  if (( used_mb <= MAX_CACHE_MB )); then
    return
  fi

  log "Cache cleanup: ${used_mb}MB used, trimming to ${MAX_CACHE_MB}MB"

  # Delete oldest files first
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

  # always fetch next in background
  background_fetch_pending & disown || true
}

main() {
  ensure_dirs

  # initial main fetch
  local idx nextfile
  idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  nextfile="${STATE_DIR}/next.txt"
  until fetch_batch_to "$idx" "$MAIN_LIST" "$nextfile"; do
    log "Retry initial fetch in 5s..."
    sleep 5
    idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
  done
  mv "$nextfile" "$INDEX_FILE"

  # fetch next batch in background
  background_fetch_pending & disown || true

  local start_ts now total i url
  start_ts="$(date +%s)"

  while true; do
    now="$(date +%s)"
    if (( now - start_ts >= RESTART_SECONDS )); then
      log "Restart window hit (${RESTART_HOURS}h). Exiting for supervisor restart."
      exit 0
    fi

    if [[ ! -s "$MAIN_LIST" ]]; then
      log "WARN: main list empty; refetching..."
      idx="$(cat "$INDEX_FILE" 2>/dev/null || echo "0")"
      fetch_batch_to "$idx" "$MAIN_LIST" "$nextfile" && mv "$nextfile" "$INDEX_FILE" || sleep 2
      continue
    fi

    total="$(wc -l < "$MAIN_LIST" | tr -d ' ')"
    i=0

    while IFS= read -r url; do
      [[ -n "$url" ]] || continue
      play_one "$url"
      i=$((i+1))
      if (( i >= total )); then
        # wrap point: swap to pending like your Angular nextIndex==0 behavior
        swap_pending_if_any
      fi
    done < "$MAIN_LIST"
  done
}

main

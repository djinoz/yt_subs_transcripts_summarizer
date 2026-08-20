#!/bin/bash
# YouTube Summarizer VPN Wrapper
# Behavior:
# - If a VPN is already live, honor it and leave it running at the end.
# - If no VPN is live, start Surfshark OpenVPN, verify it actually comes up,
#   run the summarizer through it, then stop only the VPN this script started.

set -u

PROJECT_DIR="/Users/prismismmacstudio/projects/OpenAI_Youtube/yt_subs_transcripts_summarizer"
SURFSHARK_DIR="/Users/prismismmacstudio/.surfshark"
SERVER="${SURFSHARK_SERVER:-au-adl.prod.surfshark.com_udp.ovpn}"
OPENVPN_BIN="${OPENVPN_BIN:-/opt/homebrew/opt/openvpn/sbin/openvpn}"
AUTH_FILE="${SURFSHARK_AUTH_FILE:-$SURFSHARK_DIR/auth.txt}"
OVPN_CONF="${SURFSHARK_OVPN_CONF:-$SURFSHARK_DIR/$SERVER}"
SURFSHARK_SERVICE="${SURFSHARK_SERVICE:-Surfshark. WireGuard}"
BASELINE_NON_VPN_IP="${BASELINE_NON_VPN_IP:-220.233.28.1}"
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/vpn_run_$TIMESTAMP.log"
DAEMON_LOG="$LOG_DIR/openvpn_$TIMESTAMP.log"
CLONE_PROFILE="${JOPLIN_PROFILE:-${HOME}/.hermes/state/joplin/primary-profile}"
CLONE_SETTINGS_JSON="$CLONE_PROFILE/settings.json"
WRAPPER_STARTED_OPENVPN=0
OPENVPN_PID=""
INITIAL_IP=""
ACTIVE_IP=""
STATUS_REUSED_EXISTING_VPN=0
STATUS_STARTED_OWN_VPN=0
STATUS_PREFLIGHT_PASSED=0
STATUS_CLEANED_UP_OWN_VPN=0

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

public_ip() {
    curl -s --max-time 10 https://api.ipify.org || echo 'Unknown'
}

surfshark_connected() {
    scutil --nc status "$SURFSHARK_SERVICE" 2>/dev/null | grep -q '^Connected'
}

openvpn_running() {
    pgrep -x openvpn >/dev/null 2>&1
}

default_interface() {
    route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}'
}

tunnel_interface_live() {
    /sbin/ifconfig -a | awk '
        /^utun[0-9]+:/ { iface=$1; sub(":", "", iface) }
        /inet 10\./ { if (iface ~ /^utun[0-9]+$/) found=1 }
        END { exit(found ? 0 : 1) }
    '
}

vpn_live() {
    if surfshark_connected; then
        return 0
    fi
    if openvpn_running; then
        return 0
    fi
    if tunnel_interface_live; then
        return 0
    fi
    return 1
}

wait_for_vpn_ready() {
    max_retries="${1:-30}"
    count=0
    while [ "$count" -lt "$max_retries" ]; do
        if surfshark_connected || (openvpn_running && tunnel_interface_live); then
            return 0
        fi
        sleep 2
        count=$((count + 1))
    done
    return 1
}

show_recent_openvpn_log() {
    if [ -f "$DAEMON_LOG" ]; then
        tail -n 40 "$DAEMON_LOG" | tee -a "$LOG_FILE"
    else
        latest_log=$(find "$LOG_DIR" -maxdepth 1 -name 'openvpn_*.log' -type f | sort | tail -n 1)
        if [ -n "${latest_log:-}" ] && [ -f "$latest_log" ]; then
            tail -n 40 "$latest_log" | tee -a "$LOG_FILE"
        fi
    fi
}

emit_status_summary() {
    log "STATUS reused-existing-vpn=$STATUS_REUSED_EXISTING_VPN started-own-vpn=$STATUS_STARTED_OWN_VPN preflight-passed=$STATUS_PREFLIGHT_PASSED cleaned-up-own-vpn=$STATUS_CLEANED_UP_OWN_VPN"
}

cleanup() {
    if [ "$WRAPPER_STARTED_OPENVPN" -eq 1 ]; then
        log "Stopping OpenVPN started by this wrapper..."
        if [ -n "$OPENVPN_PID" ] && kill -0 "$OPENVPN_PID" 2>/dev/null; then
            sudo kill "$OPENVPN_PID" 2>/dev/null || true
            sleep 3
        fi
        if openvpn_running; then
            sudo /usr/bin/killall openvpn 2>/dev/null || true
        fi
        STATUS_CLEANED_UP_OWN_VPN=1
        log "IP after OpenVPN stop: $(public_ip)"
    else
        log "Leaving pre-existing VPN state unchanged."
    fi
    emit_status_summary
}

preflight_transcript_fetch() {
    local video_id="${VPN_PREFLIGHT_VIDEO_ID:-dWDKfgs364Y}"
    log "Running transcript preflight for video: $video_id"
    (
        cd "$PROJECT_DIR"
        ./venv/bin/python - "$video_id" <<'PY'
import sys
from youtube_transcript_api import YouTubeTranscriptApi
video_id = sys.argv[1]
try:
    fetched = YouTubeTranscriptApi().fetch(video_id, languages=['en'])
    text = " ".join(snippet.text.strip() for snippet in fetched if getattr(snippet, 'text', '').strip())
    if not text:
        raise RuntimeError('Transcript fetch returned empty text')
    print(f"[preflight-ok] {video_id} first 60 chars: {text[:60]}")
except Exception as e:
    print(f"[preflight-fail] {type(e).__name__}: {e}", file=sys.stderr)
    raise
PY
    ) >> "$LOG_FILE" 2>&1
}

trap cleanup EXIT

INITIAL_IP="$(public_ip)"
log "--- Starting YouTube Summarizer VPN Wrapper ---"
log "Initial IP: $INITIAL_IP"
log "Default route interface: $(default_interface)"

if vpn_live; then
    ACTIVE_IP="$INITIAL_IP"
    STATUS_REUSED_EXISTING_VPN=1
    if surfshark_connected; then
        log "Pre-existing Surfshark VPN detected. Reusing existing VPN and leaving it running afterward."
    elif openvpn_running; then
        log "Pre-existing OpenVPN tunnel detected. Reusing existing VPN and leaving it running afterward."
    else
        log "Pre-existing VPN route detected. Reusing existing VPN and leaving it running afterward."
    fi
    log "Active VPN IP: $ACTIVE_IP"
else
    log "No live VPN detected. Testing direct YouTube reachability before starting OpenVPN."
    if preflight_transcript_fetch; then
        STATUS_REUSED_EXISTING_VPN=1
        STATUS_PREFLIGHT_PASSED=1
        log "Direct path already works; skipping OpenVPN."
    else
        log "Direct path failed. Starting OpenVPN with config: $OVPN_CONF"

        if [ ! -x "$OPENVPN_BIN" ]; then
            log "ERROR: OpenVPN binary not executable: $OPENVPN_BIN"
            exit 1
        fi
        if [ ! -f "$OVPN_CONF" ]; then
            log "ERROR: OpenVPN config missing: $OVPN_CONF"
            exit 1
        fi
        if [ ! -f "$AUTH_FILE" ]; then
            log "ERROR: Auth file missing: $AUTH_FILE"
            exit 1
        fi

        sudo "$OPENVPN_BIN" \
            --config "$OVPN_CONF" \
            --auth-user-pass "$AUTH_FILE" \
            --daemon \
            --log "$DAEMON_LOG"

        sleep 2
        OPENVPN_PID="$(pgrep -xn openvpn || true)"
        WRAPPER_STARTED_OPENVPN=1
        STATUS_STARTED_OWN_VPN=1

        if ! wait_for_vpn_ready 45; then
            log "ERROR: VPN tunnel failed to establish in time."
            show_recent_openvpn_log
            exit 1
        fi

        ACTIVE_IP="$(public_ip)"
        log "OpenVPN tunnel detected. Public IP probe: $ACTIVE_IP"

        if ! preflight_transcript_fetch; then
            log "ERROR: Transcript preflight failed on current VPN path."
            exit 1
        fi
        STATUS_PREFLIGHT_PASSED=1
    fi
fi

JOPLIN_TOKEN=$(CLONE_PROFILE="$CLONE_PROFILE" python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/'.hermes'/'state'/'joplin'/'primary-profile'/'settings.json'
if not p.exists():
    raise SystemExit(1)
print(json.loads(p.read_text()).get('api.token', ''))
PY
)
[ -n "$JOPLIN_TOKEN" ] || { log "ERROR: Missing headless Joplin API token in $CLONE_SETTINGS_JSON"; exit 1; }

export JOPLIN_DIRECT_IMPORT=1
export JOPLIN_API_URL="http://127.0.0.1:41184"
export JOPLIN_API_TOKEN="$JOPLIN_TOKEN"
export JOPLIN_IMPORT_FOLDER_ID="b7bbdb6f010f482b949ab65113434ace"
export JOPLIN_IMPORT_TAGS="yt_subs_transcripts_summarizer,youtube"
export JOPLIN_IMPORT_IS_TODO=0

JOPLIN_PROFILE="$CLONE_PROFILE" "$HOME/.hermes/scripts/ensure_joplin_headless_api.sh" >> "$LOG_FILE" 2>&1

log "Running YouTube Summarizer via VPN..."
cd "$PROJECT_DIR"
./venv/bin/python yt_subs_summarizer.py "$@" >> "$LOG_FILE" 2>&1
RESULT=$?

if [ $RESULT -eq 0 ]; then
    if ! joplin --profile "$CLONE_PROFILE" sync >>"$LOG_FILE" 2>&1; then
        RESULT=$?
        log "ERROR: Clone-profile sync failed after direct import (exit code: $RESULT)"
    fi
fi

log "Summarizer finished with exit code: $RESULT"
log "--- YouTube Summarizer VPN Wrapper Finished ---"
exit $RESULT

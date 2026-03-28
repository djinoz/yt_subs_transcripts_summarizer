#!/bin/bash
# YouTube Summarizer Wrapper (with OpenVPN support)
# Restored OpenVPN support because residential/GUI-only approach was inconsistent for transcript fetching.

PROJECT_DIR="/Users/prismismmacstudio/projects/OpenAI_Youtube/yt_subs_transcripts_summarizer"
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/vpn_run_$TIMESTAMP.log"
OVPN_CONF="/Users/prismismmacstudio/.surfshark/au-adl.prod.surfshark.com_udp.ovpn"
AUTH_FILE="/Users/prismismmacstudio/.surfshark/auth.txt"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "--- Starting YouTube Summarizer Wrapper (OpenVPN) ---"
log "Initial IP: $(curl -s --max-time 5 https://api.ipify.org || echo 'Unknown')"

# Cleanup stale OpenVPN
if pgrep -x "openvpn" > /dev/null; then
    log "Cleaning up existing OpenVPN process..."
    sudo /usr/bin/killall openvpn
    sleep 5
fi

# Start OpenVPN
log "Starting OpenVPN with config: $OVPN_CONF"
# Note: Path with NOPASSWD sudo rights on this machine
sudo /opt/homebrew/opt/openvpn/sbin/openvpn --config "$OVPN_CONF" --auth-user-pass "$AUTH_FILE" --daemon --log "$LOG_DIR/openvpn_$TIMESTAMP.log"

# Helper to detect Surfshark/OpenVPN tunnel (utun with 10.x IP)
has_vpn() {
    /sbin/ifconfig -a | awk '
        /^utun[0-9]:/ { iface=$1 }
        /inet 10\./ { if (iface ~ /^utun[0-9]:/) { found=1 } }
        END { exit(found ? 0 : 1) }
    '
}

# Wait for VPN to establish
MAX_RETRIES=45
COUNT=0
while [ $COUNT -lt $MAX_RETRIES ]; do
    if has_vpn; then
        log "VPN tunnel (utun) with 10.x IP established."
        break
    fi
    log "Waiting for VPN... ($COUNT/$MAX_RETRIES)"
    sleep 2
    COUNT=$((COUNT + 1))
done

if ! has_vpn; then
    log "ERROR: VPN failed to establish. Checking OpenVPN log..."
    # Try to find any recent log since we can't reliably predict the exact name if TIMESTAMP drifts
    ls -t "$LOG_DIR"/openvpn_*.log | head -n 1 | xargs tail -n 20 | tee -a "$LOG_FILE"
    exit 1
fi

log "VPN IP: $(curl -s --max-time 5 https://api.ipify.org || echo 'Unknown')"

# Run the Summarizer
log "Running YouTube Summarizer..."
cd "$PROJECT_DIR"
./venv/bin/python yt_subs_summarizer.py "$@" >> "$LOG_FILE" 2>&1
RESULT=$?

log "Summarizer finished with exit code: $RESULT"

# Cleanup: Stop OpenVPN if we started it? 
# Usually better to leave it for the next run or cron to manage,
# but if the user wants isolation, we kill it.
# log "Stopping OpenVPN..."
# sudo pkill -x openvpn

log "--- YouTube Summarizer Wrapper Finished ---"
exit $RESULT

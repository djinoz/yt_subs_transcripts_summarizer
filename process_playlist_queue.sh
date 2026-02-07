#!/bin/bash
# Process videos from "yt-summariser" playlist (user-curated queue)
# Ignores age limits - processes any video in the playlist once only
# Uses Surfshark VPN to avoid rate limits

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Surfshark/VPN Config
SURFSHARK_DIR="$HOME/.surfshark"
SERVER="${VPN_SERVER:-jp-tok.prod.surfshark.com_udp.ovpn}"
OPENVPN="/opt/homebrew/opt/openvpn/sbin/openvpn"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOG_DIR/playlist_queue_${TIMESTAMP}.log"
VPN_LOG="$LOG_DIR/vpn_playlist_${TIMESTAMP}.log"

echo "[$(date)] Starting Playlist Queue VPN wrapper..." | tee -a "$LOG_FILE"

# Connect VPN
echo "[$(date)] Connecting to VPN ($SERVER)..." | tee -a "$LOG_FILE"
sudo "$OPENVPN" \
  --config "$SURFSHARK_DIR/$SERVER" \
  --auth-user-pass "$SURFSHARK_DIR/auth.txt" \
  --daemon \
  --log "$VPN_LOG"

# Wait for connection
sleep 10

# Check if VPN is up
if pgrep -x openvpn > /dev/null; then
    NEW_IP=$(curl -s --max-time 10 https://api.ipify.org || echo "IP check failed")
    echo "[$(date)] VPN connected. IP: $NEW_IP" | tee -a "$LOG_FILE"
    
    # Activate venv
    source .venv/bin/activate

    # Run the summarizer
    echo "[$(date)] Running YouTube summarizer (playlist mode)..." | tee -a "$LOG_FILE"
    python3 yt_subs_summarizer.py \
        --playlist "yt-summariser" \
        --max-age-days 0 \
        --log-level INFO \
        2>&1 | tee -a "$LOG_FILE"
    
    RESULT=${PIPESTATUS[0]}
    
    # Disconnect VPN
    echo "[$(date)] Disconnecting VPN..." | tee -a "$LOG_FILE"
    sudo killall openvpn 2>/dev/null
    
    if [ $RESULT -eq 0 ]; then
        echo "[$(date)] ✅ Playlist queue processed successfully" | tee -a "$LOG_FILE"
    else
        echo "[$(date)] ❌ Playlist queue processing failed (exit code: $RESULT)" | tee -a "$LOG_FILE"
    fi
    exit $RESULT
else
    echo "[$(date)] ERROR: VPN failed to connect" | tee -a "$LOG_FILE"
    cat "$VPN_LOG" 2>/dev/null | tee -a "$LOG_FILE"
    exit 1
fi

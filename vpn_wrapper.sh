#!/bin/bash
# Dynamic VPN Wrapper for YouTube Summarizer
# This script manages an OpenVPN connection specifically for the YouTube summarizer
# to bypass IP blocking when not using a residential IP.

PROJECT_DIR="/Users/prismismmacstudio/projects/OpenAI_Youtube/yt_subs_transcripts_summarizer"
SURFSHARK_DIR="/Users/prismismmacstudio/.surfshark"
SERVER="nz-akl.prod.surfshark.com_udp.ovpn"
OPENVPN="/opt/homebrew/opt/openvpn/sbin/openvpn"
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/vpn_run_$TIMESTAMP.log"

mkdir -p "$LOG_DIR"

echo "[$(date)] --- Starting Dynamic VPN Wrapper ---" | tee -a "$LOG_FILE"

# 1. Check if OpenVPN is already running
if pgrep -x openvpn > /dev/null; then
    echo "[$(date)] OpenVPN is already running. IP: $(curl -s --max-time 5 https://api.ipify.org)" | tee -a "$LOG_FILE"
    # We proceed using the existing connection
else
    echo "[$(date)] Connecting to Surfshark VPN ($SERVER)..." | tee -a "$LOG_FILE"
    # Note: Requires passwordless sudo for openvpn in /etc/sudoers
    sudo "$OPENVPN" \
      --config "$SURFSHARK_DIR/$SERVER" \
      --auth-user-pass "$SURFSHARK_DIR/auth.txt" \
      --daemon \
      --log "$LOG_DIR/vpn_daemon_$TIMESTAMP.log"
    
    # Wait for connection to establish
    MAX_WAIT=20
    WAIT_COUNT=0
    while ! pgrep -x openvpn > /dev/null && [ $WAIT_COUNT -lt $MAX_WAIT ]; do
        sleep 2
        WAIT_COUNT=$((WAIT_COUNT+2))
    done
    
    if pgrep -x openvpn > /dev/null; then
        sleep 5 # Extra buffer for routing
        NEW_IP=$(curl -s --max-time 10 https://api.ipify.org)
        echo "[$(date)] VPN Connected. Current IP: $NEW_IP" | tee -a "$LOG_FILE"
    else
        echo "[$(date)] ERROR: VPN failed to connect within $MAX_WAIT seconds." | tee -a "$LOG_FILE"
        exit 1
    fi
fi

# 2. Run the Summarizer
echo "[$(date)] Running YouTube Summarizer..." | tee -a "$LOG_FILE"
cd "$PROJECT_DIR"
./venv/bin/python yt_subs_summarizer.py >> "$LOG_FILE" 2>&1
RESULT=$?

# 3. Cleanup
echo "[$(date)] Summarizer finished with exit code: $RESULT" | tee -a "$LOG_FILE"
echo "[$(date)] Disconnecting VPN..." | tee -a "$LOG_FILE"
sudo killall openvpn 2>/dev/null

echo "[$(date)] --- Dynamic VPN Wrapper Finished ---" | tee -a "$LOG_FILE"
exit $RESULT

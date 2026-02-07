#!/bin/bash
# VPN Wrapper for YouTube Summarizer
# Connects to Surfshark VPN, runs summarizer, then disconnects

SURFSHARK_DIR="$HOME/.surfshark"
SERVER="${VPN_SERVER:-jp-tok.prod.surfshark.com_udp.ovpn}"
OPENVPN="/opt/homebrew/opt/openvpn/sbin/openvpn"
LOG_DIR="$HOME/projects/OpenAI_Youtube/yt_subs_transcripts_summarizer/logs"

mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "[$(date)] Starting VPN wrapper..."

# Connect VPN
echo "[$(date)] Connecting to VPN ($SERVER)..."
sudo "$OPENVPN" \
  --config "$SURFSHARK_DIR/$SERVER" \
  --auth-user-pass "$SURFSHARK_DIR/auth.txt" \
  --daemon \
  --log "$LOG_DIR/vpn_$TIMESTAMP.log"

# Wait for connection
sleep 8

# Check if VPN is up
if pgrep -x openvpn > /dev/null; then
    NEW_IP=$(curl -s --max-time 10 https://api.ipify.org)
    echo "[$(date)] VPN connected. IP: $NEW_IP"
    
    # Run the summarizer
    echo "[$(date)] Running YouTube summarizer..."
    cd ~/projects/OpenAI_Youtube/yt_subs_transcripts_summarizer
    source venv/bin/activate
    python3 yt_subs_summarizer.py 2>&1 | tee "$LOG_DIR/summarizer_$TIMESTAMP.log"
    RESULT=$?
    
    # Disconnect VPN
    echo "[$(date)] Disconnecting VPN..."
    sudo killall openvpn 2>/dev/null
    
    echo "[$(date)] Done. Summarizer exit code: $RESULT"
    exit $RESULT
else
    echo "[$(date)] ERROR: VPN failed to connect"
    cat "$LOG_DIR/vpn_$TIMESTAMP.log" 2>/dev/null
    exit 1
fi

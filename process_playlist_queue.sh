#!/bin/bash
# Process videos from "yt-summariser" playlist (user-curated queue)
# Ignores age limits - processes any video in the playlist once only
# NOTE: VPN removed - YouTube transcript API blocks known VPN IP ranges (see MEMORY.md 2026-02-08)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOG_DIR/playlist_queue_${TIMESTAMP}.log"

echo "[$(date)] Starting Playlist Queue processor..." | tee -a "$LOG_FILE"
echo "[$(date)] Current IP: $(curl -s --max-time 5 https://api.ipify.org || echo 'Unknown')" | tee -a "$LOG_FILE"

# Activate venv and run
source venv/bin/activate
python3 yt_subs_summarizer.py \
    --playlist "yt-summariser" \
    --max-age-days 0 \
    --log-level INFO \
    2>&1 | tee -a "$LOG_FILE"
RESULT=${PIPESTATUS[0]}

if [ $RESULT -eq 0 ]; then
    echo "[$(date)] ✅ Playlist queue processed successfully" | tee -a "$LOG_FILE"
else
    echo "[$(date)] ❌ Playlist queue processing failed (exit code: $RESULT)" | tee -a "$LOG_FILE"
fi
exit $RESULT

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

CLONE_PROFILE="${JOPLIN_PROFILE:-${HOME}/.hermes/state/joplin/primary-profile}"
CLONE_SETTINGS_JSON="$CLONE_PROFILE/settings.json"
JOPLIN_TOKEN=$(CLONE_SETTINGS_JSON="$CLONE_SETTINGS_JSON" python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ['CLONE_SETTINGS_JSON'])
if not p.exists():
    raise SystemExit(1)
print(json.loads(p.read_text()).get('api.token', ''))
PY
)
[ -n "$JOPLIN_TOKEN" ] || { echo "[$(date)] ❌ Missing headless Joplin API token in $CLONE_SETTINGS_JSON" | tee -a "$LOG_FILE"; exit 1; }

export JOPLIN_DIRECT_IMPORT=1
export JOPLIN_API_URL="http://127.0.0.1:41184"
export JOPLIN_API_TOKEN="$JOPLIN_TOKEN"
export JOPLIN_IMPORT_FOLDER_ID="b7bbdb6f010f482b949ab65113434ace"
export JOPLIN_IMPORT_TAGS="yt_subs_transcripts_summarizer,youtube"
export JOPLIN_IMPORT_IS_TODO=0

JOPLIN_PROFILE="$CLONE_PROFILE" JOPLIN_PROFILE="$CLONE_PROFILE" "$HOME/.hermes/scripts/ensure_joplin_headless_api.sh" >> "$LOG_FILE" 2>&1

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
    if ! joplin --profile "$CLONE_PROFILE" sync >>"$LOG_FILE" 2>&1; then
        RESULT=$?
        echo "[$(date)] ❌ Playlist queue sync failed (exit code: $RESULT)" | tee -a "$LOG_FILE"
    else
        echo "[$(date)] ✅ Playlist queue processed successfully" | tee -a "$LOG_FILE"
    fi
else
    echo "[$(date)] ❌ Playlist queue processing failed (exit code: $RESULT)" | tee -a "$LOG_FILE"
fi
exit $RESULT

#!/usr/bin/env python3
"""
Mark all current videos in the "yt-summariser" playlist as already processed
without actually processing them. This prevents backlog processing on first run.

Run this once when setting up the playlist queue system.
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import Set, Dict

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from yt_subs_summarizer import (
    load_dotenv,
    get_youtube_service,
    resolve_playlist_id,
    list_videos_from_playlist_id,
    load_state,
    save_state,
    log_message
)

def main():
    # Load config
    load_dotenv()
    
    # Get state file path
    state_file = os.getenv("YT_STATE_FILE", "yt_state.json")
    max_age_days = int(os.getenv("YT_MAX_AGE_DAYS", "14"))
    
    # Load existing state
    processed_ids, video_errors, processed_timestamps = load_state(state_file, max_age_days)
    
    log_message("Authorizing with YouTube…")
    youtube = get_youtube_service()
    
    # Resolve playlist
    playlist_name = "yt-summariser"
    log_message(f"Resolving playlist: {playlist_name}")
    resolved = resolve_playlist_id(youtube, playlist_name)
    
    if not resolved:
        log_message(f"❌ Playlist not found: {playlist_name}", file=sys.stderr)
        sys.exit(1)
    
    playlist_id, playlist_title = resolved
    log_message(f"Found playlist: {playlist_title} ({playlist_id})")
    
    # Fetch all videos (no age limit)
    videos, _ = list_videos_from_playlist_id(youtube, playlist_id, max_age_days=0)
    
    log_message(f"Fetched {len(videos)} videos from playlist")
    
    # Mark all as processed (without actually processing)
    new_count = 0
    already_processed = 0
    current_time = time.time()
    
    for video in videos:
        vid = video["videoId"]
        if vid in processed_ids:
            already_processed += 1
        else:
            processed_ids.add(vid)
            processed_timestamps[vid] = current_time
            new_count += 1
            log_message(f"  Marked: {video['channelTitle']} — {video['title']}")
    
    # Save state
    save_state(state_file, processed_ids, video_errors, processed_timestamps)
    
    log_message(f"\n✅ Marked {new_count} new videos as processed")
    log_message(f"   ({already_processed} were already in state file)")
    log_message(f"\nPlaylist queue is now ready. Future videos added to the playlist will be processed.")

if __name__ == "__main__":
    main()

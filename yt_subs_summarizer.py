#!/usr/bin/env python3
# yt_subs_summarizer.py
# Modes:
#   (default) subscriptions
#   --playlist "<name or id>"
#   --urls <url_or_id> [more...]
# Options:
#   --dryrun            : print human-readable info; no files/state
#   --show-transcripts  : with --dryrun, list available caption langs/types per video
#   --skip-state        : do not update the state file (still read for filtering)
#
# ENV (.env) highlights:
#   YT_MAX_VIDEOS=30
#   YT_MAX_AGE_DAYS=14
#   YT_PER_CHANNEL_LIMIT=3
#   YT_EXCLUDE_SHORTS=1
#   YT_SHORTS_MAX_SECONDS=180
#   YT_STATE_FILE=yt_state.json
#   YT_USE_EFFICIENT_API=1              # use efficient API (default, recommended)
#   YT_TAKEOUT_WATCH_JSON=
#   YT_COOKIES_FILE=~/youtube_cookies.txt   # cookies (Netscape) for gated captions
#   HTTP_PROXY=
#   HTTPS_PROXY=
#   YT_TRANSCR_PREF_LANGS=en,en-US,en-GB,en-CA,en-AU
#   YT_TRANSLATE_TO=en
#   YT_ACCEPT_NON_EN=1
#   YT_MARK_PROCESSED_ON_NO_TRANSCRIPT=0
#   YT_LOG_LEVEL=ERROR                  # ERROR, WARN, INFO (default ERROR)
#   OPENAI_API_KEY= (optional)
#   OPENAI_MODEL=gpt-4o-mini

import os
import sys
import re
import json
import argparse
import pathlib
import datetime as dt
from typing import List, Dict, Optional, Tuple, Set

from dotenv import load_dotenv

# Global flag to track quota exhaustion
QUOTA_EXHAUSTED = False

# OpenAI summarization prompt
OPENAI_SUMMARY_PROMPT = (
    "You are a concise assistant. Summarize the following YouTube transcript into:\n"
    "1) A 120-200 word paragraph TL;DR\n"
    "2) 5 bullet key takeaways\n"
    "3) 3 suggested follow-up actions (if relevant)\n"
    "4) 1 direct quote (up to 50 words) that captures the most salient point - could be the spiciest take, heterodoxical viewpoint, or crisp encapsulation\n"
    "Keep it faithful and non-speculative."
)

from tqdm import tqdm

# HTTP + parsing (for yt-dlp fallback)
import html
import requests
import xml.etree.ElementTree as ET

# Google / YouTube API
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import google.auth.exceptions
import pickle

# Transcripts
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    CouldNotRetrieveTranscript,
    IpBlocked,
)

# Local summarizer (TextRank via sumy)
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer

# Optional OpenAI
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

def log_message(message: str, file=sys.stdout):
    """Prints a message to the specified file stream with a timestamp."""
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", file=file)

def should_log_level(level: str, current_level: str) -> bool:
    """Check if a log level should be shown based on current log level setting."""
    levels = {"ERROR": 0, "WARN": 1, "INFO": 2}
    return levels.get(level, 2) <= levels.get(current_level, 2)

# ------------------ Config & State ------------------

def load_config(args=None):
    load_dotenv()
    cfg = {
        "YT_MAX_VIDEOS": int(os.getenv("YT_MAX_VIDEOS", "30")),
        "YT_MAX_AGE_DAYS": int(os.getenv("YT_MAX_AGE_DAYS", "14")),
        "YT_PER_CHANNEL_LIMIT": int(os.getenv("YT_PER_CHANNEL_LIMIT", "3")),
        "OUTPUT_DIR": os.getenv("OUTPUT_DIR", "./ToJoplin"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", "").strip(),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "PREF_LANGS": [s.strip() for s in os.getenv("YT_TRANSCR_PREF_LANGS", "en,en-US,en-GB,en-CA,en-AU").split(",") if s.strip()],
        "TRANSLATE_TO": os.getenv("YT_TRANSLATE_TO", "en").strip() or "en",
        "ACCEPT_NON_EN": os.getenv("YT_ACCEPT_NON_EN", "1").strip() not in ("0", "false", "False"),
        "LOG_SKIPS": os.getenv("YT_LOG_SKIPS", "1").strip() not in ("0", "false", "False"),
        "LOG_LEVEL": os.getenv("YT_LOG_LEVEL", "ERROR").strip().upper(),
        "STATE_FILE": os.getenv("YT_STATE_FILE", "yt_state.json"),
        "TAKEOUT_WATCH_HISTORY_JSON": os.getenv("YT_TAKEOUT_WATCH_JSON", "").strip(),
        "MARK_PROCESSED_ON_NO_TRANSCRIPT": os.getenv("YT_MARK_PROCESSED_ON_NO_TRANSCRIPT", "0").strip() in ("1","true","True"),
        "EXCLUDE_SHORTS": os.getenv("YT_EXCLUDE_SHORTS", "1").strip() not in ("0","false","False"),
        "SHORTS_MAX_SECONDS": int(os.getenv("YT_SHORTS_MAX_SECONDS", "180")),
        # cookies + proxy support for transcript fetching
        "COOKIES_FILE": os.getenv("YT_COOKIES_FILE", "").strip() or None,
        "HTTP_PROXY": os.getenv("HTTP_PROXY", "").strip() or None,
        "HTTPS_PROXY": os.getenv("HTTPS_PROXY", "").strip() or None,
        "USE_EFFICIENT_API": os.getenv("YT_USE_EFFICIENT_API", "1").strip() not in ("0", "false", "False"),
    }
    
    # Apply command-line overrides if provided
    if args:
        if args.max_age_days is not None:
            cfg["YT_MAX_AGE_DAYS"] = args.max_age_days
        if args.max_videos is not None:
            cfg["YT_MAX_VIDEOS"] = args.max_videos
        if getattr(args, 'per_channel_limit', None) is not None:
            cfg["YT_PER_CHANNEL_LIMIT"] = args.per_channel_limit
        if getattr(args, 'log_level', None) is not None:
            cfg["LOG_LEVEL"] = args.log_level
    
    return cfg

def load_state(path: str, max_age_days: int = 14) -> Tuple[Set[str], Dict[str, str], Dict[str, float]]:
    """
    Load state returning (processed_ids, video_errors, processed_timestamps).
    
    Migrates old format (list) to new format (dict with timestamps).
    Prunes entries older than max_age_days.
    """
    import time
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Read processed videos (support both old and new format)
        processed_timestamps = data.get("processed_timestamps", {})
        old_list = data.get("processed_video_ids", [])
        
        # Migrate old format entries to current timestamp
        current_time = time.time()
        for vid in old_list:
            if vid not in processed_timestamps:
                processed_timestamps[vid] = current_time
        
        # Prune old entries (older than max_age_days)
        cutoff_time = current_time - (max_age_days * 86400)
        processed_timestamps = {
            vid: ts for vid, ts in processed_timestamps.items()
            if ts > cutoff_time
        }
        
        processed_ids = set(processed_timestamps.keys())
        video_errors = data.get("video_errors", {})
        return processed_ids, video_errors, processed_timestamps
    except Exception:
        return set(), {}, {}

def save_state(path: str, processed_ids: Set[str], video_errors: Dict[str, str] = None, processed_timestamps: Dict[str, float] = None):
    """Save state with processed IDs (with timestamps) and error information"""
    import time
    
    if video_errors is None:
        video_errors = {}
    if processed_timestamps is None:
        processed_timestamps = {}
    
    # Ensure all processed_ids have timestamps
    current_time = time.time()
    for vid in processed_ids:
        if vid not in processed_timestamps:
            processed_timestamps[vid] = current_time
    
    tmp = {
        "processed_timestamps": processed_timestamps,
        "video_errors": video_errors
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tmp, f, indent=2)

_YT_URL_RE = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_\-]{11})")
_YT_ID_RE  = re.compile(r"^[A-Za-z0-9_\-]{11}$")
_PLAYLIST_ID_RE = re.compile(r"^(PL|UU|LL|WL|FL)[A-Za-z0-9_\-]{10,}$")

def _extract_video_id(s: str) -> Optional[str]:
    if _YT_ID_RE.match(s or ""):
        return s
    m = _YT_URL_RE.search(s or "")
    return m.group(1) if m else None

def looks_like_playlist_id(s: str) -> bool:
    return bool(_PLAYLIST_ID_RE.match(s or ""))

def load_takeout_history_ids(path: str) -> Set[str]:
    ids: Set[str] = set()
    if not path or not os.path.exists(path):
        return ids
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        for e in entries:
            url = e.get("titleUrl") or e.get("titleUrl ") or ""
            vid = _extract_video_id(url)
            if vid:
                ids.add(vid)
    except Exception as e:
        log_message(f"[warn] Could not parse Takeout watch history at {path}: {e}", file=sys.stderr)
    return ids

# ------------------ API helpers ------------------

def get_youtube_service() -> object:
    creds = None
    token_path = "token.pickle"
    if os.path.exists(token_path):
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except google.auth.exceptions.RefreshError:
                creds = None
        if not creds:
            if not os.path.exists("client_secret.json"):
                log_message("ERROR: Put your OAuth 'client_secret.json' in this folder.", file=sys.stderr)
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)
    return build("youtube", "v3", credentials=creds)

def iso_to_dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

def _http_error_reason(err: HttpError) -> Tuple[int, Optional[str]]:
    status = getattr(err.resp, "status", None) or getattr(err, "status_code", None) or 0
    reason = None
    try:
        data = json.loads(err.content.decode("utf-8"))
        errors = data.get("error", {}).get("errors", [])
        if errors:
            reason = errors[0].get("reason")
    except Exception:
        pass
    return int(status), reason

def _should_retry(status: int, reason: Optional[str]) -> bool:
    return (status in (500, 502, 503, 504, 429)) or (status == 403 and reason in ("rateLimitExceeded","userRateLimitExceeded","quotaExceeded"))

def _execute_with_backoff(request, what: str, max_attempts: int = 5):
    global QUOTA_EXHAUSTED
    
    # If quota is already exhausted, don't make any more API calls
    if QUOTA_EXHAUSTED:
        log_message(f"[skip] {what}: Quota exhausted, abandoning API calls", file=sys.stderr)
        return None
    
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as e:
            status, reason = _http_error_reason(e)
            
            # Check for quota exhaustion
            if status == 403 and reason == "quotaExceeded":
                QUOTA_EXHAUSTED = True
                log_message(f"[QUOTA] {what}: YouTube API quota exhausted. Abandoning further API calls but will process any videos already retrieved.", file=sys.stderr)
                return None
                
            if status in (403, 404) and (reason in (
                "playlistNotFound",
                "playlistItemsNotAccessible",
                "forbidden",
                "channelClosed",
                "channelSuspended",
                "channelDisabled",
            ) or what.startswith("playlistItems.list:")):
                log_message(f"[skip] {what}: {status} {reason}", file=sys.stderr)
                return None
            if not _should_retry(status, reason):
                log_message(f"[fail] {what}: HTTP {status} ({reason})", file=sys.stderr)
                raise
            log_message(f"[retry] {what}: HTTP {status} ({reason}), attempt {attempt}/{max_attempts}, sleep {delay:.1f}s", file=sys.stderr)
            import time as _t; _t.sleep(delay)
            delay = min(delay * 2, 30.0)
        except Exception as e:
            if attempt >= 3:
                log_message(f"[fail] {what}: {e}", file=sys.stderr)
                raise
            log_message(f"[retry] {what}: {e}, attempt {attempt}/3, sleep {delay:.1f}s", file=sys.stderr)
            import time as _t; _t.sleep(delay)
            delay = min(delay * 2, 30.0)
    return None

# ------------------ Subscriptions → uploads playlists ------------------

def get_subscribed_upload_playlists(youtube) -> List[Dict]:
    """
    Returns: list of dicts {playlist_id, channel_id, channel_title}
    """
    out: List[Dict] = []
    subs_req = youtube.subscriptions().list(
        part="snippet,contentDetails",
        mine=True,
        maxResults=50,
        order="relevance",
    )
    while subs_req:
        subs_resp = _execute_with_backoff(subs_req, "subscriptions.list")
        if not subs_resp:
            break
        channel_ids = [item["snippet"]["resourceId"]["channelId"] for item in subs_resp.get("items", [])]
        for i in range(0, len(channel_ids), 50):
            ch_req = youtube.channels().list(part="contentDetails,snippet", id=",".join(channel_ids[i:i + 50]))
            ch_resp = _execute_with_backoff(ch_req, "channels.list")
            if not ch_resp:
                continue
            for ch in ch_resp.get("items", []):
                try:
                    uploads_id = ch["contentDetails"]["relatedPlaylists"]["uploads"]
                    title = ch["snippet"]["title"]
                    out.append({"playlist_id": uploads_id, "channel_id": ch["id"], "channel_title": title})
                except KeyError:
                    continue
        subs_req = youtube.subscriptions().list_next(subs_req, subs_resp)
    # dedupe by playlist_id
    seen = set(); uniq = []
    for e in out:
        if e["playlist_id"] not in seen:
            uniq.append(e); seen.add(e["playlist_id"])
    return uniq

# ------------------ Duration / Shorts helpers ------------------

def _parse_iso8601_duration_to_seconds(s: str) -> int:
    if not s or not s.startswith("PT"):
        return 0
    s = s[2:]
    h = m = sec = 0; num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        else:
            if ch == "H": h = int(num or 0); num = ""
            elif ch == "M": m = int(num or 0); num = ""
            elif ch == "S": sec = int(num or 0); num = ""
    return h*3600 + m*60 + sec

def _format_duration(seconds: int) -> str:
    """Format duration in seconds to MM:SS or HH:MM:SS format"""
    if seconds < 0:
        return "0:00"
    
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"

def exclude_shorts(youtube, videos: List[Dict], max_seconds: int, log_level: str = "INFO", dryrun: bool = False) -> List[Dict]:
    """
    Filter out YouTube Shorts based on duration.

    This filter should ALWAYS be applied when EXCLUDE_SHORTS is enabled, regardless of quota status.
    If quota is exhausted, we filter based on any duration data already present in the video objects.
    Videos without duration data will be kept (conservative approach to avoid over-filtering).
    """
    if not videos:
        return videos

    kept: List[Dict] = []
    videos_needing_duration: List[Dict] = []

    # First pass: filter videos that already have duration data
    for v in videos:
        if "duration_seconds" in v:
            # Duration already known (from --urls mode or previous fetch)
            secs = v["duration_seconds"]
            if "duration" not in v:
                v["duration"] = _format_duration(secs)
            if secs > max_seconds:
                kept.append(v)
            elif dryrun or should_log_level("INFO", log_level):
                log_message(f"[skip] SHORT ({secs}s) {v['channelTitle']} — {v['title']}", file=sys.stderr)
        else:
            # Need to fetch duration
            videos_needing_duration.append(v)

    # Second pass: fetch durations for videos that don't have it yet
    # This will respect QUOTA_EXHAUSTED flag via _execute_with_backoff
    if videos_needing_duration:
        ids = [v["videoId"] for v in videos_needing_duration]
        for i in range(0, len(ids), 50):
            chunk = ids[i:i+50]
            req = youtube.videos().list(part="contentDetails", id=",".join(chunk))
            resp = _execute_with_backoff(req, "videos.list:shorts_filter")

            if resp:
                # Successfully fetched duration data
                details = {item["id"]: item.get("contentDetails", {}) for item in resp.get("items", [])}
                for v in videos_needing_duration[i:i+50]:
                    dur = details.get(v["videoId"], {}).get("duration")
                    secs = _parse_iso8601_duration_to_seconds(dur or "PT0S")
                    v["duration_seconds"] = secs
                    if "duration" not in v:
                        v["duration"] = _format_duration(secs)
                    if secs > max_seconds:
                        kept.append(v)
                    elif dryrun or should_log_level("INFO", log_level):
                        log_message(f"[skip] SHORT ({secs}s) {v['channelTitle']} — {v['title']}", file=sys.stderr)
            else:
                # API call failed (likely quota exhausted) - keep videos conservatively
                # Better to process a few shorts than to skip legitimate long-form content
                if should_log_level("WARN", log_level):
                    log_message(f"[warn] Could not fetch duration for {len(videos_needing_duration[i:i+50])} videos (likely quota exhausted). Keeping them to avoid over-filtering.", file=sys.stderr)
                kept.extend(videos_needing_duration[i:i+50])

    return kept

# ------------------ Efficient Subscription API ------------------

def get_recent_subscription_videos_efficient(youtube, max_videos: int, max_age_days: int) -> List[Dict]:
    """
    Efficiently get recent videos from subscriptions using a hybrid approach:
    1. Get a sample of most relevant subscription channels (~1 API call)
    2. Use search API to get recent videos from those channels (~20 API calls)
    
    Total: ~21 API calls instead of 200+ with the old method!
    Retrieves extra videos to account for heavy filtering (Shorts, already processed, etc.)
    """
    # Get a sample of subscribed channels (increased to get more candidates for filtering)
    channels = []
    subs_req = youtube.subscriptions().list(
        part="snippet",
        mine=True,
        maxResults=20,  # Increased from 8 to get more channels and survive filtering
        order="relevance"  # Get most relevant channels
    )
    
    resp = _execute_with_backoff(subs_req, "subscriptions.list:sample")
    if not resp:
        return []
        
    for item in resp.get("items", []):
        try:
            channel_id = item["snippet"]["resourceId"]["channelId"]
            channel_title = item["snippet"]["title"]
            channels.append({"id": channel_id, "title": channel_title})
        except KeyError:
            continue
    
    log_message(f"Searching recent videos from {len(channels)} most active subscribed channels…")
    
    # Now use search API to get recent videos from these channels
    cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=max_age_days) if max_age_days > 0 else None
    videos = []
    
    for channel in channels[:20]:  # Increased from 10 to process more channels
        if len(videos) >= max_videos:
            break
            
        search_req = youtube.search().list(
            part="snippet",
            channelId=channel["id"],
            type="video",
            order="date",
            maxResults=min(15, max_videos // 15 + 5),  # Increased from 5 to get more videos per channel
            publishedAfter=(cutoff.isoformat() if cutoff else None)
        )
        
        resp = _execute_with_backoff(search_req, f"search.list:{channel['title']}")
        if not resp:
            continue
            
        for item in resp.get("items", []):
            if len(videos) >= max_videos:
                break
                
            try:
                videos.append({
                    "videoId": item["id"]["videoId"],
                    "publishedAt": item["snippet"]["publishedAt"],
                    "title": item["snippet"]["title"],
                    "channelTitle": item["snippet"]["channelTitle"],
                    "videoOwnerChannelTitle": item["snippet"]["channelTitle"],  # Same as channelTitle for subscription videos
                })
            except KeyError:
                continue
    
    return videos

# ------------------ Listing + Filters ------------------

def iter_recent_from_uploads(youtube, uploads_info: List[Dict], per_channel_max_age_days: int, per_channel_limit: int, dryrun: bool=False) -> List[Dict]:
    """First page per uploads playlist; per-channel age filter & cap."""
    cutoff = None
    if per_channel_max_age_days and per_channel_max_age_days > 0:
        cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=per_channel_max_age_days)
    videos: List[Dict] = []
    for entry in tqdm(uploads_info, desc="Scanning subscriptions"):
        pid = entry["playlist_id"]; channel_title = entry["channel_title"]
        # Pull a small buffer above the cap to survive later filters
        page_size = min(50, max(5, per_channel_limit * 3))
        req = youtube.playlistItems().list(part="snippet,contentDetails", playlistId=pid, maxResults=page_size)
        resp = _execute_with_backoff(req, f"playlistItems.list:{channel_title}")
        if not resp:
            continue
        got = 0
        for item in resp.get("items", []):
            try:
                published_at = iso_to_dt(item["contentDetails"]["videoPublishedAt"]).astimezone()
            except Exception:
                continue
            if cutoff and published_at < cutoff:
                continue
            try:
                videos.append({
                    "videoId": item["contentDetails"]["videoId"],
                    "publishedAt": item["contentDetails"]["videoPublishedAt"],
                    "title": item["snippet"]["title"],
                    "channelTitle": channel_title,
                    "videoOwnerChannelTitle": channel_title,  # Same as channelTitle for subscription videos
                })
                got += 1
                if got >= per_channel_limit:
                    break
            except Exception:
                continue
        if dryrun and got == 0:
            log_message(f"[info] No recent items for channel: {channel_title}")
    return videos

def list_videos_from_playlist_id(youtube, playlist_id: str, max_age_days: int) -> Tuple[List[Dict], str]:
    """First page of a specific playlist; returns (videos, playlist_title)."""
    pl_req = youtube.playlists().list(part="snippet", id=playlist_id, maxResults=1)
    pl_resp = _execute_with_backoff(pl_req, "playlists.get")
    
    # Handle quota exhaustion or failed fetch
    if pl_resp is None:
        return [], "(playlist)"
        
    playlist_title = (pl_resp.get("items",[{}])[0].get("snippet",{}) or {}).get("title","(playlist)")
    cutoff = None
    if max_age_days and max_age_days > 0:
        cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=max_age_days)
    out: List[Dict] = []
    req = youtube.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50)
    resp = _execute_with_backoff(req, f"playlistItems.list:{playlist_title}")
    if resp:
        for item in resp.get("items", []):
            try:
                published_at = iso_to_dt(item["contentDetails"]["videoPublishedAt"]).astimezone()
            except Exception:
                continue
            if cutoff and published_at < cutoff:
                continue
            try:
                out.append({
                    "videoId": item["contentDetails"]["videoId"],
                    "publishedAt": item["contentDetails"]["videoPublishedAt"],
                    "title": item["snippet"]["title"],
                    "channelTitle": item["snippet"]["channelTitle"],
                    "videoOwnerChannelTitle": item["snippet"].get("videoOwnerChannelTitle", item["snippet"]["channelTitle"]),
                })
            except Exception:
                continue
    return out, playlist_title

# ------------------ Playlist resolution ------------------

def resolve_playlist_id(youtube, query: str) -> Optional[Tuple[str, str]]:
    """Return (playlist_id, playlist_title) or None. Tries direct ID → your playlists exact-title → public exact-title."""
    q = (query or "").strip()
    if not q:
        return None
    if looks_like_playlist_id(q):
        req = youtube.playlists().list(part="snippet", id=q, maxResults=1)
        resp = _execute_with_backoff(req, "playlists.get:id")
        title = (resp.get("items",[{}])[0].get("snippet",{}) or {}).get("title", q) if resp else q
        return (q, title)
    page_token = None
    while True:
        req = youtube.playlists().list(part="snippet,contentDetails", mine=True, maxResults=50, pageToken=page_token)
        resp = _execute_with_backoff(req, "playlists.list:mine")
        if not resp:
            break
        for it in resp.get("items", []):
            title = it.get("snippet", {}).get("title", "")
            if title.lower().strip() == q.lower():
                return (it["id"], title)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    req = youtube.search().list(part="snippet", q=q, type="playlist", maxResults=5)
    resp = _execute_with_backoff(req, f"search.list:{q}")
    if resp:
        candidates = []
        for it in resp.get("items", []):
            title = it.get("snippet", {}).get("title", "")
            pid = it.get("id", {}).get("playlistId")
            if pid and title.lower().strip() == q.lower():
                return (pid, title)
            if pid:
                candidates.append((title, pid))
        if candidates:
            log_message("[error] No exact playlist title match. Did you mean one of:", file=sys.stderr)
            for t, pid in candidates:
                log_message(f"  - {t} (id: {pid})", file=sys.stderr)
    return None

# ------------------ Transcript helpers ------------------

def _list_transcripts_debug(video_id: str, cookies_path: Optional[str], proxies: Optional[Dict[str,str]]) -> str:
    try:
        api = YouTubeTranscriptApi()
        listing = api.list(video_id)
        return str(listing)
    except IpBlocked as e:
        log_message(f"\n❌ ERROR: YouTube is blocking requests from your IP address.", file=sys.stderr)
        log_message(f"This usually happens when:", file=sys.stderr)
        log_message(f"- You've made too many requests and your IP has been temporarily blocked", file=sys.stderr)
        log_message(f"- Your IP belongs to a cloud provider (AWS, Google Cloud, Azure, etc.)", file=sys.stderr)
        log_message(f"\n💡 Solutions:", file=sys.stderr)
        log_message(f"- Connect to a VPN and try again", file=sys.stderr)
        log_message(f"- Wait a few hours before trying again", file=sys.stderr)
        log_message(f"- Use a residential IP address instead of cloud/datacenter IP", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        return f"(unable to list transcripts: {type(e).__name__}: {e})"

def _requests_proxies(proxies: Optional[Dict[str,str]]):
    # requests expects {'http': 'http://..', 'https': 'http://..'}
    return proxies if proxies else None

def _fetch_url_text(url: str, proxies: Optional[Dict[str,str]]) -> Optional[str]:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"}, proxies=_requests_proxies(proxies))
        if r.status_code == 200:
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
    except Exception:
        pass
    return None

def _vtt_to_text(vtt: str) -> str:
    lines = []
    for line in vtt.splitlines():
        if not line.strip():
            continue
        if line.startswith("WEBVTT"):
            continue
        if "-->" in line:
            continue
        if line.strip().isdigit():
            continue
        lines.append(line)
    return " ".join(lines)

def _srv3_or_ttml_to_text(xml_text: str) -> str:
    # Handle YouTube srv3/ttml variants into plain text
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return ""
    out = []
    for elem in root.iter():
        if elem.text and elem.text.strip():
            out.append(html.unescape(elem.text.strip()))
    return " ".join(out)

def _fetch_transcript_via_ytdlp(video_id: str, cookies_path: Optional[str], proxies: Optional[Dict[str,str]]) -> Optional[Dict[str,str]]:
    # Try Python module first, then external command if module missing
    url = f"https://www.youtube.com/watch?v={video_id}"
    # 1) Python module
    try:
        import yt_dlp  # optional dependency
        ydl_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "cookiefile": os.path.expanduser(cookies_path) if cookies_path else None,
        }
        if proxies and ("https" in proxies or "http" in proxies):
            ydl_opts["proxy"] = proxies.get("https") or proxies.get("http")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        cand = []
        for key in ("subtitles", "automatic_captions"):
            d = info.get(key) or {}
            for lang, items in d.items():
                if not lang.startswith("en"):
                    continue
                for it in items:
                    u = it.get("url"); ext = (it.get("ext") or "").lower()
                    if u:
                        cand.append((lang, ext, u))
        # Prefer vtt > srv3 > ttml > json3
        rank = {"vtt":0, "srv3":1, "ttml":2, "json3":3}
        cand.sort(key=lambda t: rank.get(t[1], 9))
        for lang, ext, u in cand:
            raw = _fetch_url_text(u, proxies)
            if not raw: 
                continue
            if ext == "vtt":
                text = _vtt_to_text(raw)
            else:
                text = _srv3_or_ttml_to_text(raw)
            if text and text.strip():
                return {"text": text, "lang": lang, "translated": False}
    except Exception:
        pass

    # 2) External yt-dlp command is overkill here; skip to keep things simple/portable
    return None

def fetch_transcript_any_lang(
    video_id: str,
    pref_langs: List[str],
    translate_to: str = "en",
    accept_non_en: bool = True,
    log_skips: bool = True,
    cookies_path: Optional[str] = None,
    proxies: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """
    Strategy using new API v1.2.2:
      A) Try to fetch with preferred languages directly
      B) Try translation to target language if available
      C) Accept any available language if accept_non_en
      D) yt-dlp fallback
    """
    reasons = []
    api = YouTubeTranscriptApi()

    # --- A) Try preferred languages first
    try:
        fetched_transcript = api.fetch(video_id, languages=pref_langs)
        # Verify valid transcript data
        if not fetched_transcript.snippets:
             reasons.append("A:empty_snippets")
        else:
             text = " ".join(snippet.text.strip() for snippet in fetched_transcript.snippets if snippet.text.strip())
             if text:
                 # Success: Log proof-of-life locally
                 print(f"      [proof-of-life] {video_id} first 30 chars: {text[:30]}", file=sys.stderr)
                 return {"text": text, "lang": fetched_transcript.language_code, "translated": False}
    except IpBlocked as e:
        log_message(f"\n❌ ERROR: YouTube is blocking requests from your IP address.", file=sys.stderr)
        log_message(f"This usually happens when:", file=sys.stderr)
        log_message(f"- You've made too many requests and your IP has been temporarily blocked", file=sys.stderr)
        log_message(f"- Your IP belongs to a cloud provider (AWS, Google Cloud, Azure, etc.)", file=sys.stderr)
        log_message(f"\n💡 Solutions:", file=sys.stderr)
        log_message(f"- Connect to a VPN and try again", file=sys.stderr)
        log_message(f"- Wait a few hours before trying again", file=sys.stderr)
        log_message(f"- Use a residential IP address instead of cloud/datacenter IP", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        reasons.append(f"A:fetch_preferred:{type(e).__name__}")

    # --- B) Try any available language
    try:
        # Try to fetch any available transcript (defaults to English)
        fetched_transcript = api.fetch(video_id)
        if not fetched_transcript.snippets:
             reasons.append("B:empty_snippets")
        else:
             text = " ".join(snippet.text.strip() for snippet in fetched_transcript.snippets if snippet.text.strip())
             if text:
                 print(f"      [proof-of-life] {video_id} first 30 chars: {text[:30]}", file=sys.stderr)
                 return {"text": text, "lang": fetched_transcript.language_code, "translated": False}
            
    except IpBlocked as e:
        log_message(f"\n❌ ERROR: YouTube is blocking requests from your IP address.", file=sys.stderr)
        log_message(f"This usually happens when:", file=sys.stderr)
        log_message(f"- You've made too many requests and your IP has been temporarily blocked", file=sys.stderr)
        log_message(f"- Your IP belongs to a cloud provider (AWS, Google Cloud, Azure, etc.)", file=sys.stderr)
        log_message(f"\n💡 Solutions:", file=sys.stderr)
        log_message(f"- Connect to a VPN and try again", file=sys.stderr)
        log_message(f"- Wait a few hours before trying again", file=sys.stderr)
        log_message(f"- Use a residential IP address instead of cloud/datacenter IP", file=sys.stderr)
        sys.exit(1)
    except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as e:
        # Re-raise these errors so they can be caught and stored in the main loop
        raise
    except Exception as e:
        reasons.append(f"B:fetch_any:{type(e).__name__}")

    # --- C) yt-dlp fallback
    ytdlp_result = _fetch_transcript_via_ytdlp(video_id, cookies_path, proxies)
    if ytdlp_result:
        log_message(f"[fallback] {video_id} transcript fetched via yt-dlp", file=sys.stderr)
        return ytdlp_result

    if log_skips:
        if reasons:
            log_message(f"[skip] {video_id} transcripts exist but were not retrievable. Reasons: {', '.join(reasons)}", file=sys.stderr)
        else:
            log_message(f"[skip] {video_id} transcripts exist but none usable with current policy", file=sys.stderr)
    return None

# ------------------ Summaries ------------------

def summarize_local_textrank(text: str, sentences: int = 5) -> str:
    try:
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarizer = TextRankSummarizer()
        sent_list = summarizer(parser.document, sentences)
        if not sent_list:
            from sumy.summarizers.lsa import LsaSummarizer
            summarizer = LsaSummarizer()
            sent_list = summarizer(parser.document, min(3, sentences))
        return " ".join(str(s) for s in sent_list)
    except Exception:
        return (text[:800] + "…") if len(text) > 800 else text

def summarize_openai(text: str, api_key: str, model: str = "gpt-4o-mini") -> str:
    if not OpenAI:
        raise RuntimeError("openai package not available")
    client = OpenAI(api_key=api_key)
    content = [
        {"type": "text", "text": OPENAI_SUMMARY_PROMPT},
        {"type": "text", "text": text[:150000]}
    ]
    resp = client.chat.completions.create(
        model=model, 
        messages=[{"role": "user", "content": content}], 
        temperature=0.2
    )
    return resp.choices[0].message.content.strip()

def save_markdown(out_dir: pathlib.Path, video: Dict, transcript_info: Dict[str, str], summary_block: str, youtube=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    published = iso_to_dt(video["publishedAt"]).astimezone().strftime("%Y-%m-%d")
    # Decode HTML entities first, then clean for filesystem
    clean_title = html.unescape(video["title"])
    safe_title = "".join(c for c in clean_title if c not in r'\/:*?"<>|').strip()
    # New filename format: TITLE - DATE
    path = out_dir / f"{safe_title} - {published}.md"
    url = f"https://www.youtube.com/watch?v={video['videoId']}"
    lang = transcript_info.get("lang", "unknown")
    translated = transcript_info.get("translated", False)
    
    # Use video owner channel title if different from channel title (for playlists)
    display_channel = video.get("videoOwnerChannelTitle", video["channelTitle"])
    
    # Get duration if available, otherwise try to fetch it
    duration_display = video.get("duration")
    if duration_display is None and youtube:
        try:
            req = youtube.videos().list(part="contentDetails", id=video["videoId"])
            resp = _execute_with_backoff(req, "videos.list:duration")
            if resp and resp.get("items"):
                duration_iso = resp["items"][0].get("contentDetails", {}).get("duration", "PT0S")
                duration_seconds = _parse_iso8601_duration_to_seconds(duration_iso)
                duration_display = _format_duration(duration_seconds)
            else:
                duration_display = "Unknown"
        except Exception:
            duration_display = "Unknown"
    elif duration_display is None:
        duration_display = "Unknown"
    
    # YAML moved to bottom - decode HTML entities for clean display
    md = f"""# {clean_title}
**Channel:** {html.unescape(display_channel)}  
**Duration:** {duration_display}  
**Published:** {video['publishedAt']}  
**Link:** {url}

## Summary
{summary_block}

---
title: "{clean_title}"
channel: "{html.unescape(display_channel)}"
video_id: "{video['videoId']}"
published_at: "{video['publishedAt']}"
source_url: "{url}"
transcript_language: "{lang}"
transcript_translated: {str(bool(translated)).lower()}
---

## Transcript

{transcript_info['text']}
"""
    path.write_text(md, encoding="utf-8")
    return str(path)

# ------------------ Main ------------------

def main():
    ap = argparse.ArgumentParser(description="Summarize transcripts for latest videos.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--playlist", type=str, help="Playlist name or ID to process instead of subscriptions.")
    mode.add_argument("--urls", nargs="*", help="Explicit list of YouTube video URLs or IDs.")
    ap.add_argument("--dryrun", action="store_true", help="Print human-readable info; no files/state.")
    ap.add_argument("--show-transcripts", action="store_true", help="With --dryrun, list available caption langs/types per video.")
    ap.add_argument("--skip-state", action="store_true", help="Do not update the state file (still read for filtering).")
    ap.add_argument("--max-age-days", type=int, help="Override YT_MAX_AGE_DAYS from .env file.")
    ap.add_argument("--max-videos", type=int, help="Override YT_MAX_VIDEOS from .env file.")
    ap.add_argument("--per-channel-limit", type=int, help="Override YT_PER_CHANNEL_LIMIT from .env file.")
    ap.add_argument("--log-level", choices=["ERROR", "WARN", "INFO"], help="Set logging level (ERROR, WARN, INFO)")
    args = ap.parse_args()

    cfg = load_config(args)
    out_dir = pathlib.Path(cfg["OUTPUT_DIR"])
    use_openai = bool(cfg["OPENAI_API_KEY"])

    # proxies map for youtube_transcript_api (and requests fallback)
    proxies = {}
    if cfg.get("HTTP_PROXY"): proxies["http"] = cfg["HTTP_PROXY"]
    if cfg.get("HTTPS_PROXY"): proxies["https"] = cfg["HTTPS_PROXY"]
    if not proxies:
        proxies = None

    # State & optional watch-history
    state_file = cfg["STATE_FILE"]
    processed_ids, video_errors, processed_timestamps = load_state(state_file, cfg["YT_MAX_AGE_DAYS"])
    takeout_ids = load_takeout_history_ids(cfg["TAKEOUT_WATCH_HISTORY_JSON"])
    if takeout_ids:
        log_message(f"Loaded {len(takeout_ids)} watched IDs from Takeout.")

    log_message("Authorizing with YouTube…")
    youtube = get_youtube_service()

    candidates: List[Dict] = []
    human_context = ""

    if args.playlist:
        query = args.playlist.strip()
        resolved = resolve_playlist_id(youtube, query)
        if not resolved:
            log_message(f"[error] Playlist not found for: {query}", file=sys.stderr)
            sys.exit(2)
        pid, pl_title = resolved
        human_context = f'Playlist: "{pl_title}"'
        log_message(f'Using playlist: {pl_title}')
        videos, _title = list_videos_from_playlist_id(youtube, pid, 0)  # No age filter for explicit playlists
        candidates = videos
        log_message(f"Candidates from playlist: {len(candidates)}")

    elif args.urls is not None:
        if len(args.urls) == 0:
            log_message("[error] --urls provided but no URLs/IDs given.", file=sys.stderr)
            sys.exit(2)
        vids, bad = [], []
        for u in args.urls:
            vid = _extract_video_id(u)
            (vids if vid else bad).append(vid or u)
        if not vids:
            log_message("[error] No valid video URLs/IDs parsed from --urls.", file=sys.stderr)
            if bad: log_message("  Invalid:", ", ".join(bad), file=sys.stderr)
            sys.exit(2)
        for i in range(0, len(vids), 50):
            chunk = vids[i:i+50]
            req = youtube.videos().list(part="snippet,contentDetails", id=",".join(chunk))
            resp = _execute_with_backoff(req, "videos.list:urls")
            if not resp:
                continue
            for it in resp.get("items", []):
                try:
                    duration_iso = it.get("contentDetails", {}).get("duration", "PT0S")
                    duration_seconds = _parse_iso8601_duration_to_seconds(duration_iso)
                    candidates.append({
                        "videoId": it["id"],
                        "publishedAt": it["snippet"]["publishedAt"],
                        "title": it["snippet"]["title"],
                        "channelTitle": it["snippet"]["channelTitle"],
                        "videoOwnerChannelTitle": it["snippet"]["channelTitle"],  # Same as channelTitle for individual videos
                        "duration": _format_duration(duration_seconds),
                        "duration_seconds": duration_seconds,  # Store raw seconds for shorts filter
                    })
                except Exception:
                    continue
        human_context = "Explicit URLs"

    else:
        if cfg["USE_EFFICIENT_API"]:
            log_message("Fetching recent videos from subscriptions (efficient API)…")
            try:
                # Use the efficient search-based approach
                candidates = get_recent_subscription_videos_efficient(
                    youtube, 
                    max_videos=cfg["YT_MAX_VIDEOS"] * 10,  # Get 10x to survive heavy filtering (Shorts + already processed)
                    max_age_days=cfg["YT_MAX_AGE_DAYS"]
                )
                human_context = "Subscriptions (Efficient API)"
                log_message(f"Candidates from efficient API: {len(candidates)}")
                
                # Check if quota was exhausted during retrieval
                if QUOTA_EXHAUSTED:
                    log_message(f"⚠️  API quota exhausted during video retrieval. Will process {len(candidates)} videos that were retrieved before quota limit.")
                    
            except Exception as e:
                log_message(f"[error] Efficient API failed: {e}", file=sys.stderr)
                # If quota exhausted, don't exit - we might have some candidates to process
                if not QUOTA_EXHAUSTED:
                    log_message(f"Please disable YT_USE_EFFICIENT_API in .env and try the legacy method if needed.", file=sys.stderr)
                    sys.exit(1)
                else:
                    log_message("Quota exhausted - will process any videos retrieved before the limit.", file=sys.stderr)
                    candidates = []  # No candidates if we failed due to quota
        else:
            log_message("⚠️  WARNING: Using legacy uploads playlist method - this may cause YouTube API rate limiting!")
            log_message("   Recommend setting YT_USE_EFFICIENT_API=1 for much better performance.")
            try:
                uploads = get_subscribed_upload_playlists(youtube)
                log_message(f"Found {len(uploads)} subscriptions with uploads playlists.")
                candidates = iter_recent_from_uploads(
                    youtube,
                    uploads,
                    per_channel_max_age_days=cfg["YT_MAX_AGE_DAYS"],
                    per_channel_limit=cfg["YT_PER_CHANNEL_LIMIT"],
                    dryrun=args.dryrun
                )
                human_context = "Subscriptions (Legacy Method)"
                log_message(f"Candidates after per-channel age & cap: {len(candidates)}")
                
                # Check if quota was exhausted during retrieval
                if QUOTA_EXHAUSTED:
                    log_message(f"⚠️  API quota exhausted during video retrieval. Will process {len(candidates)} videos that were retrieved before quota limit.")
                    
            except Exception as e:
                log_message(f"[error] Legacy API failed: {e}", file=sys.stderr)
                if QUOTA_EXHAUSTED:
                    log_message("Quota exhausted - will process any videos retrieved before the limit.", file=sys.stderr)
                    candidates = []  # No candidates if we failed due to quota
                else:
                    raise

    # Shorts exclusion (all modes)
    # ALWAYS apply shorts filter when enabled, regardless of quota status
    # The exclude_shorts function will handle quota exhaustion gracefully
    if cfg["EXCLUDE_SHORTS"] and candidates:
        before = len(candidates)
        candidates = exclude_shorts(youtube, candidates, cfg["SHORTS_MAX_SECONDS"], cfg["LOG_LEVEL"], args.dryrun)
        log_message(f"After Shorts filter: kept {len(candidates)}/{before}")

    # Unwatched proxy: remove already processed, errored videos & (optionally) watched via Takeout
    before = len(candidates)
    filtered = []
    for v in candidates:
        vid = v["videoId"]
        if vid in processed_ids:
            if cfg["LOG_SKIPS"] and (args.dryrun or should_log_level("INFO", cfg["LOG_LEVEL"])):
                log_message(f"[skip] already processed: {v['channelTitle']} — {v['title']}", file=sys.stderr)
            continue
        if vid in video_errors:
            if cfg["LOG_SKIPS"] and (args.dryrun or should_log_level("INFO", cfg["LOG_LEVEL"])):
                log_message(f"[skip] previous error ({video_errors[vid]}): {v['channelTitle']} — {v['title']}", file=sys.stderr)
            continue
        if takeout_ids and vid in takeout_ids:
            if cfg["LOG_SKIPS"] and (args.dryrun or should_log_level("INFO", cfg["LOG_LEVEL"])):
                log_message(f"[skip] in watch history: {v['channelTitle']} — {v['title']}", file=sys.stderr)
            continue
        filtered.append(v)
    candidates = filtered
    log_message(f"After unwatched proxy filter: kept {len(candidates)}/{before}")

    # Sort newest-first & apply global cap (not for --urls)
    candidates.sort(key=lambda x: x.get("publishedAt",""), reverse=True)
    if cfg["YT_MAX_VIDEOS"] > 0 and args.urls is None:
        candidates = candidates[:cfg["YT_MAX_VIDEOS"]]
    cap_info = cfg['YT_MAX_VIDEOS'] if args.urls is None else 'n/a (--urls)'
    log_message(f"Final selection count: {len(candidates)} (cap={cap_info})")

    if not candidates:
        log_message("No videos to process after filters.")
        if not args.dryrun and not args.skip_state:
            save_state(state_file, processed_ids, video_errors, processed_timestamps)
        return

    if args.dryrun:
        log_message(f"---- DRY RUN LIST ({human_context}) ----")
        for v in candidates:
            vid = v["videoId"]
            url = f"https://www.youtube.com/watch?v={vid}"
            log_message(f"- {v['channelTitle']} | {v['title']} | {url}")
            if args.show_transcripts:
                info_line = _list_transcripts_debug(vid, cfg["COOKIES_FILE"], proxies)
                log_message(f"  captions available: {info_line}")
            try:
                info = fetch_transcript_any_lang(
                    vid,
                    pref_langs=cfg["PREF_LANGS"],
                    translate_to=cfg["TRANSLATE_TO"],
                    accept_non_en=cfg["ACCEPT_NON_EN"],
                    log_skips=cfg["LOG_SKIPS"],
                    cookies_path=cfg["COOKIES_FILE"],
                    proxies=proxies,
                )
                snippet = ("not found" if not info else (info["text"][:100].replace("\n", " ") + ("…" if len(info["text"])>100 else "")))
            except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as e:
                snippet = f"error: {type(e).__name__}"
            log_message(f"  transcript: {snippet}")
        log_message("---- END DRY RUN ----")
        return

    log_message(f"Processing {len(candidates)} videos…")
    saved = 0
    for v in tqdm(candidates, desc="Summarizing"):
        vid = v["videoId"]
        try:
            info = fetch_transcript_any_lang(
                vid,
                pref_langs=cfg["PREF_LANGS"],
                translate_to=cfg["TRANSLATE_TO"],
                accept_non_en=cfg["ACCEPT_NON_EN"],
                log_skips=cfg["LOG_SKIPS"],
                cookies_path=cfg["COOKIES_FILE"],
                proxies=proxies,
            )
        except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as e:
            # Store the error type for this video to avoid retrying
            error_type = type(e).__name__
            video_errors[vid] = error_type
            if cfg["LOG_SKIPS"] and should_log_level("WARN", cfg["LOG_LEVEL"]):
                log_message(f"[skip] {vid} transcript error recorded: {error_type}", file=sys.stderr)
            continue
        
        if not info:
            if cfg["MARK_PROCESSED_ON_NO_TRANSCRIPT"] and not args.skip_state:
                processed_ids.add(vid)
            continue
        try:
            if use_openai:
                summary_block = summarize_openai(
                    info["text"], 
                    cfg["OPENAI_API_KEY"], 
                    cfg["OPENAI_MODEL"]
                )
            else:
                summary_block = summarize_local_textrank(info["text"], sentences=6)
            save_markdown(out_dir, v, info, summary_block, youtube)
            saved += 1
            if not args.skip_state:
                processed_ids.add(vid)
        except Exception as e:
            log_message(f"[warn] failed to save/mark {vid}: {e}", file=sys.stderr)

    if not args.skip_state:
        save_state(state_file, processed_ids, video_errors, processed_timestamps)
    
    # Final summary message
    if QUOTA_EXHAUSTED:
        log_message(f"✓ Completed processing despite YouTube API quota exhaustion.")
        log_message(f"  Processed {saved} videos that were retrieved before quota limit.")
        log_message(f"  Markdown files saved to: {out_dir.resolve()}")
        log_message(f"  Script will retry remaining videos on next scheduled run (quota resets daily).")
    else:
        log_message(f"Done. Markdown files saved to: {out_dir.resolve()} (wrote {saved} files)")

if __name__ == "__main__":
    main()


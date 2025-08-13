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
#   YT_SHORTS_MAX_SECONDS=65
#   YT_STATE_FILE=yt_state.json
#   YT_TAKEOUT_WATCH_JSON=
#   YT_COOKIES_FILE=~/youtube_cookies.txt   # cookies (Netscape) for gated captions
#   HTTP_PROXY=
#   HTTPS_PROXY=
#   YT_TRANSCR_PREF_LANGS=en,en-US,en-GB,en-CA,en-AU
#   YT_TRANSLATE_TO=en
#   YT_ACCEPT_NON_EN=1
#   YT_MARK_PROCESSED_ON_NO_TRANSCRIPT=0
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

# ------------------ Config & State ------------------

def load_config():
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
        "STATE_FILE": os.getenv("YT_STATE_FILE", "yt_state.json"),
        "TAKEOUT_WATCH_HISTORY_JSON": os.getenv("YT_TAKEOUT_WATCH_JSON", "").strip(),
        "MARK_PROCESSED_ON_NO_TRANSCRIPT": os.getenv("YT_MARK_PROCESSED_ON_NO_TRANSCRIPT", "0").strip() in ("1","true","True"),
        "EXCLUDE_SHORTS": os.getenv("YT_EXCLUDE_SHORTS", "1").strip() not in ("0","false","False"),
        "SHORTS_MAX_SECONDS": int(os.getenv("YT_SHORTS_MAX_SECONDS", "65")),
        # cookies + proxy support for transcript fetching
        "COOKIES_FILE": os.getenv("YT_COOKIES_FILE", "").strip() or None,
        "HTTP_PROXY": os.getenv("HTTP_PROXY", "").strip() or None,
        "HTTPS_PROXY": os.getenv("HTTPS_PROXY", "").strip() or None,
    }
    return cfg

def load_state(path: str) -> Set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("processed_video_ids", []))
    except Exception:
        return set()

def save_state(path: str, processed_ids: Set[str]):
    tmp = {"processed_video_ids": sorted(processed_ids)}
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
        print(f"[warn] Could not parse Takeout watch history at {path}: {e}", file=sys.stderr)
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
                print("ERROR: Put your OAuth 'client_secret.json' in this folder.", file=sys.stderr)
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
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as e:
            status, reason = _http_error_reason(e)
            if status in (403, 404) and (reason in (
                "playlistNotFound",
                "playlistItemsNotAccessible",
                "forbidden",
                "channelClosed",
                "channelSuspended",
                "channelDisabled",
            ) or what.startswith("playlistItems.list:")):
                print(f"[skip] {what}: {status} {reason}", file=sys.stderr)
                return None
            if not _should_retry(status, reason):
                print(f"[fail] {what}: HTTP {status} ({reason})", file=sys.stderr)
                raise
            print(f"[retry] {what}: HTTP {status} ({reason}), attempt {attempt}/{max_attempts}, sleep {delay:.1f}s", file=sys.stderr)
            import time as _t; _t.sleep(delay)
            delay = min(delay * 2, 30.0)
        except Exception as e:
            if attempt >= 3:
                print(f"[fail] {what}: {e}", file=sys.stderr)
                raise
            print(f"[retry] {what}: {e}, attempt {attempt}/3, sleep {delay:.1f}s", file=sys.stderr)
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

def exclude_shorts(youtube, videos: List[Dict], max_seconds: int) -> List[Dict]:
    if not videos:
        return videos
    kept: List[Dict] = []
    ids = [v["videoId"] for v in videos]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i+50]
        req = youtube.videos().list(part="contentDetails", id=",".join(chunk))
        resp = _execute_with_backoff(req, "videos.list")
        details = {item["id"]: item.get("contentDetails", {}) for item in (resp.get("items", []) if resp else [])}
        for v in videos[i:i+50]:
            dur = details.get(v["videoId"], {}).get("duration")
            secs = _parse_iso8601_duration_to_seconds(dur or "PT0S")
            if secs > max_seconds:
                kept.append(v)
            else:
                print(f"[skip] SHORT ({secs}s) {v['channelTitle']} — {v['title']}", file=sys.stderr)
    return kept

# ------------------ Listing + Filters ------------------

def iter_recent_from_uploads(youtube, uploads_info: List[Dict], per_channel_max_age_days: int, per_channel_limit: int, dryrun: bool=False) -> List[Dict]:
    """First page per uploads playlist; per-channel age filter & cap."""
    cutoff = None
    if per_channel_max_age_days and per_channel_max_age_days > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=per_channel_max_age_days)
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
                published_at = iso_to_dt(item["contentDetails"]["videoPublishedAt"]).astimezone(dt.timezone.utc)
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
                })
                got += 1
                if got >= per_channel_limit:
                    break
            except Exception:
                continue
        if dryrun and got == 0:
            print(f"[info] No recent items for channel: {channel_title}")
    return videos

def list_videos_from_playlist_id(youtube, playlist_id: str, max_age_days: int) -> Tuple[List[Dict], str]:
    """First page of a specific playlist; returns (videos, playlist_title)."""
    pl_req = youtube.playlists().list(part="snippet", id=playlist_id, maxResults=1)
    pl_resp = _execute_with_backoff(pl_req, "playlists.get")
    playlist_title = (pl_resp.get("items",[{}])[0].get("snippet",{}) or {}).get("title","(playlist)")
    cutoff = None
    if max_age_days and max_age_days > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
    out: List[Dict] = []
    req = youtube.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50)
    resp = _execute_with_backoff(req, f"playlistItems.list:{playlist_title}")
    if resp:
        for item in resp.get("items", []):
            try:
                published_at = iso_to_dt(item["contentDetails"]["videoPublishedAt"]).astimezone(dt.timezone.utc)
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
            print("[error] No exact playlist title match. Did you mean one of:", file=sys.stderr)
            for t, pid in candidates:
                print(f"  - {t} (id: {pid})", file=sys.stderr)
    return None

# ------------------ Transcript helpers ------------------

def _list_transcripts_debug(video_id: str, cookies_path: Optional[str], proxies: Optional[Dict[str,str]]) -> str:
    try:
        api = YouTubeTranscriptApi()
        listing = api.list(video_id)
        return str(listing)
    except IpBlocked as e:
        print(f"\n❌ ERROR: YouTube is blocking requests from your IP address.", file=sys.stderr)
        print(f"This usually happens when:", file=sys.stderr)
        print(f"- You've made too many requests and your IP has been temporarily blocked", file=sys.stderr)
        print(f"- Your IP belongs to a cloud provider (AWS, Google Cloud, Azure, etc.)", file=sys.stderr)
        print(f"\n💡 Solutions:", file=sys.stderr)
        print(f"- Connect to a VPN and try again", file=sys.stderr)
        print(f"- Wait a few hours before trying again", file=sys.stderr)
        print(f"- Use a residential IP address instead of cloud/datacenter IP", file=sys.stderr)
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
        text = " ".join(snippet.text.strip() for snippet in fetched_transcript.snippets if snippet.text.strip())
        if text:
            return {"text": text, "lang": fetched_transcript.language_code, "translated": False}
    except IpBlocked as e:
        print(f"\n❌ ERROR: YouTube is blocking requests from your IP address.", file=sys.stderr)
        print(f"This usually happens when:", file=sys.stderr)
        print(f"- You've made too many requests and your IP has been temporarily blocked", file=sys.stderr)
        print(f"- Your IP belongs to a cloud provider (AWS, Google Cloud, Azure, etc.)", file=sys.stderr)
        print(f"\n💡 Solutions:", file=sys.stderr)
        print(f"- Connect to a VPN and try again", file=sys.stderr)
        print(f"- Wait a few hours before trying again", file=sys.stderr)
        print(f"- Use a residential IP address instead of cloud/datacenter IP", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        reasons.append(f"A:fetch_preferred:{type(e).__name__}")

    # --- B) Try any available language
    try:
        # Try to fetch any available transcript (defaults to English)
        fetched_transcript = api.fetch(video_id)
        text = " ".join(snippet.text.strip() for snippet in fetched_transcript.snippets if snippet.text.strip())
        if text:
            return {"text": text, "lang": fetched_transcript.language_code, "translated": False}
            
    except IpBlocked as e:
        print(f"\n❌ ERROR: YouTube is blocking requests from your IP address.", file=sys.stderr)
        print(f"This usually happens when:", file=sys.stderr)
        print(f"- You've made too many requests and your IP has been temporarily blocked", file=sys.stderr)
        print(f"- Your IP belongs to a cloud provider (AWS, Google Cloud, Azure, etc.)", file=sys.stderr)
        print(f"\n💡 Solutions:", file=sys.stderr)
        print(f"- Connect to a VPN and try again", file=sys.stderr)
        print(f"- Wait a few hours before trying again", file=sys.stderr)
        print(f"- Use a residential IP address instead of cloud/datacenter IP", file=sys.stderr)
        sys.exit(1)
    except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as e:
        if log_skips:
            print(f"[skip] {video_id} no transcripts: {type(e).__name__}", file=sys.stderr)
        return None
    except Exception as e:
        reasons.append(f"B:fetch_any:{type(e).__name__}")

    # --- C) yt-dlp fallback
    ytdlp_result = _fetch_transcript_via_ytdlp(video_id, cookies_path, proxies)
    if ytdlp_result:
        print(f"[fallback] {video_id} transcript fetched via yt-dlp", file=sys.stderr)
        return ytdlp_result

    if log_skips:
        if reasons:
            print(f"[skip] {video_id} transcripts exist but were not retrievable. Reasons: {', '.join(reasons)}", file=sys.stderr)
        else:
            print(f"[skip] {video_id} transcripts exist but none usable with current policy", file=sys.stderr)
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

def summarize_openai(text: str, api_key: str, model: str = "gpt-4o-mini") -> Dict[str, str]:
    if not OpenAI:
        raise RuntimeError("openai package not available")
    client = OpenAI(api_key=api_key)
    prompt = (
        "You are a concise assistant. Summarize the following YouTube transcript into:\n"
        "1) A 120-200 word paragraph TL;DR\n"
        "2) 5 bullet key takeaways\n"
        "3) 3 suggested follow-up actions (if relevant)\n"
        "Keep it faithful and non-speculative."
    )
    content = [{"type": "text", "text": prompt}, {"type": "text", "text": text[:150000]}]
    resp = client.chat.completions.create(model=model, messages=[{"role": "user", "content": content}], temperature=0.2)
    return {"summary": resp.choices[0].message.content.strip()}

def save_markdown(out_dir: pathlib.Path, video: Dict, transcript_info: Dict[str, str], summary_block: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    published = iso_to_dt(video["publishedAt"]).astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    safe_title = "".join(c for c in video["title"] if c not in r'\/:*?"<>|').strip()
    path = out_dir / f"{published} - {video['channelTitle']} - {safe_title} ({video['videoId']}).md"
    url = f"https://www.youtube.com/watch?v={video['videoId']}"
    lang = transcript_info.get("lang", "unknown")
    translated = transcript_info.get("translated", False)
    md = f"""---
title: "{video['title']}"
channel: "{video['channelTitle']}"
video_id: "{video['videoId']}"
published_at: "{video['publishedAt']}"
source_url: "{url}"
transcript_language: "{lang}"
transcript_translated: {str(bool(translated)).lower()}
---

# {video['title']}
**Channel:** {video['channelTitle']}  
**Published:** {video['publishedAt']}  
**Link:** {url}

## Summary
{summary_block}

<details>
<summary>Transcript (collapsed)</summary>

{transcript_info['text']}

</details>
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
    args = ap.parse_args()

    cfg = load_config()
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
    processed_ids = load_state(state_file)
    takeout_ids = load_takeout_history_ids(cfg["TAKEOUT_WATCH_HISTORY_JSON"])
    if takeout_ids:
        print(f"Loaded {len(takeout_ids)} watched IDs from Takeout.")

    print("Authorizing with YouTube…")
    youtube = get_youtube_service()

    candidates: List[Dict] = []
    human_context = ""

    if args.playlist:
        query = args.playlist.strip()
        resolved = resolve_playlist_id(youtube, query)
        if not resolved:
            print(f"[error] Playlist not found for: {query}", file=sys.stderr)
            sys.exit(2)
        pid, pl_title = resolved
        human_context = f'Playlist: "{pl_title}"'
        print(f'Using playlist: {pl_title}')
        videos, _title = list_videos_from_playlist_id(youtube, pid, cfg["YT_MAX_AGE_DAYS"])
        candidates = videos
        print(f"Candidates from playlist after age filter: {len(candidates)}")

    elif args.urls is not None:
        if len(args.urls) == 0:
            print("[error] --urls provided but no URLs/IDs given.", file=sys.stderr)
            sys.exit(2)
        vids, bad = [], []
        for u in args.urls:
            vid = _extract_video_id(u)
            (vids if vid else bad).append(vid or u)
        if not vids:
            print("[error] No valid video URLs/IDs parsed from --urls.", file=sys.stderr)
            if bad: print("  Invalid:", ", ".join(bad), file=sys.stderr)
            sys.exit(2)
        for i in range(0, len(vids), 50):
            chunk = vids[i:i+50]
            req = youtube.videos().list(part="snippet,contentDetails", id=",".join(chunk))
            resp = _execute_with_backoff(req, "videos.list:urls")
            if not resp:
                continue
            for it in resp.get("items", []):
                try:
                    candidates.append({
                        "videoId": it["id"],
                        "publishedAt": it["snippet"]["publishedAt"],
                        "title": it["snippet"]["title"],
                        "channelTitle": it["snippet"]["channelTitle"],
                    })
                except Exception:
                    continue
        human_context = "Explicit URLs"

    else:
        print("Collecting subscribed channels' uploads playlists…")
        uploads = get_subscribed_upload_playlists(youtube)
        print(f"Found {len(uploads)} subscriptions with uploads playlists.")
        candidates = iter_recent_from_uploads(
            youtube,
            uploads,
            per_channel_max_age_days=cfg["YT_MAX_AGE_DAYS"],
            per_channel_limit=cfg["YT_PER_CHANNEL_LIMIT"],
            dryrun=args.dryrun
        )
        human_context = "Subscriptions (Uploads)"
        print(f"Candidates after per-channel age & cap: {len(candidates)}")

    # Shorts exclusion (all modes)
    if cfg["EXCLUDE_SHORTS"] and candidates:
        before = len(candidates)
        candidates = exclude_shorts(youtube, candidates, cfg["SHORTS_MAX_SECONDS"])
        print(f"After Shorts filter: kept {len(candidates)}/{before}")

    # Unwatched proxy: remove already processed & (optionally) watched via Takeout
    before = len(candidates)
    filtered = []
    for v in candidates:
        vid = v["videoId"]
        if vid in processed_ids:
            if cfg["LOG_SKIPS"]:
                print(f"[skip] already processed: {v['channelTitle']} — {v['title']}", file=sys.stderr)
            continue
        if takeout_ids and vid in takeout_ids:
            if cfg["LOG_SKIPS"]:
                print(f"[skip] in watch history: {v['channelTitle']} — {v['title']}", file=sys.stderr)
            continue
        filtered.append(v)
    candidates = filtered
    print(f"After unwatched proxy filter: kept {len(candidates)}/{before}")

    # Sort newest-first & apply global cap (not for --urls)
    candidates.sort(key=lambda x: x.get("publishedAt",""), reverse=True)
    if cfg["YT_MAX_VIDEOS"] > 0 and args.urls is None:
        candidates = candidates[:cfg["YT_MAX_VIDEOS"]]
    cap_info = cfg['YT_MAX_VIDEOS'] if args.urls is None else 'n/a (--urls)'
    print(f"Final selection count: {len(candidates)} (cap={cap_info})")

    if not candidates:
        print("No videos to process after filters.")
        if not args.dryrun and not args.skip_state:
            save_state(state_file, processed_ids)
        return

    if args.dryrun:
        print(f"---- DRY RUN LIST ({human_context}) ----")
        for v in candidates:
            vid = v["videoId"]
            url = f"https://www.youtube.com/watch?v={vid}"
            print(f"- {v['channelTitle']} | {v['title']} | {url}")
            if args.show_transcripts:
                info_line = _list_transcripts_debug(vid, cfg["COOKIES_FILE"], proxies)
                print(f"  captions available: {info_line}")
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
            print(f"  transcript: {snippet}")
        print("---- END DRY RUN ----")
        return

    print(f"Processing {len(candidates)} videos…")
    saved = 0
    for v in tqdm(candidates, desc="Summarizing"):
        vid = v["videoId"]
        info = fetch_transcript_any_lang(
            vid,
            pref_langs=cfg["PREF_LANGS"],
            translate_to=cfg["TRANSLATE_TO"],
            accept_non_en=cfg["ACCEPT_NON_EN"],
            log_skips=cfg["LOG_SKIPS"],
            cookies_path=cfg["COOKIES_FILE"],
            proxies=proxies,
        )
        if not info:
            if cfg["MARK_PROCESSED_ON_NO_TRANSCRIPT"] and not args.skip_state:
                processed_ids.add(vid)
            continue
        try:
            if use_openai:
                client = OpenAI(api_key=cfg["OPENAI_API_KEY"])
                resp = client.chat.completions.create(
                    model=cfg["OPENAI_MODEL"],
                    messages=[{"role": "user", "content": [
                        {"type":"text","text":"You are a concise assistant. Summarize the following YouTube transcript into:\n1) A 120-200 word paragraph TL;DR\n2) 5 bullet key takeaways\n3) 3 suggested follow-up actions (if relevant)\nKeep it faithful and non-speculative."},
                        {"type":"text","text":info["text"][:150000]}
                    ]}],
                    temperature=0.2,
                )
                summary_block = resp.choices[0].message.content.strip()
            else:
                summary_block = summarize_local_textrank(info["text"], sentences=6)
            save_markdown(out_dir, v, info, summary_block)
            saved += 1
            if not args.skip_state:
                processed_ids.add(vid)
        except Exception as e:
            print(f"[warn] failed to save/mark {vid}: {e}", file=sys.stderr)

    if not args.skip_state:
        save_state(state_file, processed_ids)
    print(f"Done. Markdown files saved to: {out_dir.resolve()} (wrote {saved} files)")

if __name__ == "__main__":
    main()


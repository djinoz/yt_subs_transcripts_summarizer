# YouTube Subscriptions Transcript Summarizer

Cutdown on the number of videos I watch by getting my subscriptions fresh vids summarized in Joplin to review before watching.

Fetch transcripts for the **latest videos from channels you're subscribed to** and save **Markdown summaries** (perfect for Joplin).

## What it does

- Authenticates with your Google account (OAuth, read‑only).
- Finds every channel you're subscribed to and each channel's **Uploads** playlist.
- Collects the most recent videos across all those channels (or specific playlists/URLs).
- Pulls transcripts (including auto‑generated when available) via `youtube-transcript-api`.
- Summarizes each transcript:
  - **Local** TextRank (fallback, no API keys required, pretty crap), or
  - **OpenAI** (recommended if `OPENAI_API_KEY` is set) for high-quality structured summaries.
- Writes clean **Markdown** files to a folder you can point Joplin at.
- I use the "Hotfolder" plugin for Joplin that monitors the above folder for new .md files. 

## Features

- **Highly efficient**: Uses only ~11 YouTube API calls instead of 200+ (95% reduction)
- **Multiple modes**: Process subscriptions, specific playlists, or individual video URLs
- **Smart filtering**: Skip shorts, already processed videos, and videos from your watch history
- **Rate limiting protection**: Safe to run regularly without hitting YouTube API limits
- **Quota exhaustion handling**: Gracefully processes any retrieved videos even if API quota is exceeded
- **IP blocking protection**: Graceful error handling with helpful troubleshooting messages
- **Durable history tracking**: SQLite-backed history remembers processed videos and durable transcript failures without JSON pruning surprises
- **Single-run overlap protection**: lock file prevents concurrent runs in the same mode from reprocessing the same videos
- **High-quality summaries**: OpenAI generates structured summaries with TL;DR, key takeaways, and action items

## Privacy and Security

### ⚠️ OpenAI Data Privacy
When you enable OpenAI summaries by setting the `OPENAI_API_KEY`, please be aware that **the full transcript of each video is sent to OpenAI's servers** for processing. While this provides high-quality summaries, you should consider the privacy implications before processing private, unlisted, or sensitive videos.

### Credential Storage
This script requires access to your Google account and optionally your OpenAI account. This is handled as follows:
- `client_secret.json`: Your Google OAuth credentials, which you must download.
- `token.pickle`: Stores the authorization token from Google after you log in. It is created automatically.
- `.env`: Stores your `OPENAI_API_KEY` and other settings.

These files are sensitive and should **never be shared or committed to version control**. The repository's `.gitignore` file is already configured to exclude them, but you are responsible for keeping them secure.

### A Note on `token.pickle`
The script uses Python's `pickle` format to save authentication tokens, following the recommended practice from the Google Auth library. While standard for this type of application, be aware that loading a `pickle` file from an untrusted source can be a security risk. Ensure that your local `token.pickle` file is not replaced or tampered with by a malicious actor.

## Quick start

1. **Create a Google Cloud project** → enable **YouTube Data API v3**.
2. **Create OAuth client credentials** (Desktop app), download `client_secret.json` and put it in this folder.
3. Install deps:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and tweak if you like (optional).

```bash
cp .env.example .env
```

5. Run:

```bash
# Process your subscriptions (default)
python yt_subs_summarizer.py

# Process a specific playlist
python yt_subs_summarizer.py --playlist "My Playlist Name"

# Process specific videos
python yt_subs_summarizer.py --urls "https://www.youtube.com/watch?v=VIDEO_ID"

# Dry run to see what would be processed
python yt_subs_summarizer.py --dryrun --show-transcripts
```

The first run will open a browser window to authorize read‑only access to your YouTube account. Summaries will land in `./ToJoplin` (or change `OUTPUT_DIR` in `.env`).

## Operational reporting

### End-of-run summary

The normal run now ends with **counts-only** summary lines, designed to be cron/log friendly:

- `Run summary:` candidates, filtered counts, selected, successes, and stage-group failure totals
- `Failure breakdown:` request-blocked / transcripts-disabled / no-transcript / unavailable / private / generic transcript fetch

These lines intentionally avoid listing video titles.

### Ad hoc stats script

Use `stats_report.py` to inspect history and operational counts over a time window without making network calls:

```bash
# All modes, human-readable
python stats_report.py

# Subscription-only, last 7 days, JSON
python stats_report.py --mode subscription --since 2026-03-22T00:00:00 --json

# Playlist-only with optional pie chart PNG (if matplotlib is installed)
python stats_report.py --mode playlist --pie-chart reports/playlist_stats.png
```

Supported modes:
- `subscription`
- `playlist`
- `urls`
- `all`

The script reports DB-backed counts for:
- success
- failed temporary
- failed permanent
- failed unknown

It also derives operational counts from local logs where available:
- shorts filtered
- VPN failures
- YouTube API failures / quota events
- transcript fetch failures
- summary/model/save failures

Log-derived stage counts depend on the retained local log files and current log message patterns; the SQLite DB still stores only coarse success/temporary/permanent history.

## Troubleshooting

### YouTube API Quota Exhaustion
If you see `[QUOTA] YouTube API quota exhausted` messages:
- **This is normal** - YouTube limits API usage to 10,000 units per day
- **Videos are still processed** - Any videos retrieved before quota exhaustion are summarized normally
- **Automatic recovery** - The script will continue on next run when quota resets (daily at midnight Pacific)
- **Optimized for scheduled runs** - Current settings use ~5,400 units/day with 4-hour intervals, leaving safety buffer

### IP Blocking Issues
If you get "YouTube is blocking requests from your IP address":
- **Connect to a VPN** and try again
- **Wait a few hours** before trying again  
- Avoid running the script too frequently (YouTube has rate limits)
- Cloud/datacenter IPs (AWS, Google Cloud, etc.) are often blocked

### No Transcripts Found
- Not every video has transcripts available
- Some channels disable auto-generated transcripts
- Private/unlisted videos may not have accessible transcripts
- Use `--dryrun --show-transcripts` to check transcript availability

## Configuration

All settings can be configured in `.env`:

- **YT_MAX_VIDEOS**: Maximum videos to process per run (default: 30)
- **YT_MAX_AGE_DAYS**: Only process videos newer than this (default: 14 days) 
- **YT_PER_CHANNEL_LIMIT**: Max videos per channel (default: 3, applies to uploads-playlist scanning and legacy method)
- **YT_EXCLUDE_SHORTS**: Skip YouTube Shorts (default: true)
- **YT_USE_EFFICIENT_API**: Use uploads-playlist scanning instead of search.list (default: true, **highly recommended**)
- **YT_HISTORY_DB**: SQLite history database path (default: `yt_history.db`)
- **YT_SUBSCRIPTION_HISTORY_RETENTION_DAYS**: How long subscription/URL successes suppress reprocessing (default: 365)
- **YT_RUN_LOCK_FILE**: pidfile used to block overlapping runs in the same mode (default: `.yt_subs_summarizer.lock`)
- **OPENAI_API_KEY**: For high-quality structured summaries
- **OUTPUT_DIR**: Where to save markdown files (default: ./ToJoplin)

### History migration notes

- On first run, legacy `yt_state.json` is imported into `yt_history.db` automatically.
- Imported legacy entries are treated as **subscription-mode** history, which preserves backward compatibility without forcing playlist reprocessing.
- Playlist successes are retained indefinitely by default; subscription and `--urls` successes use the long retention window from `YT_SUBSCRIPTION_HISTORY_RETENTION_DAYS`.
- Permanent transcript failures are stored durably and skipped on future runs; temporary failures remain retryable.

### ⚠️ API Method Warning

**Default (Recommended): Efficient API**
- Uses ~11 YouTube API calls regardless of subscription count
- Focuses on your most active/relevant subscribed channels
- **Safe to run regularly** without hitting rate limits

**Legacy Method** (`YT_USE_EFFICIENT_API=0`)
- Uses 200+ API calls if you have many subscriptions  
- ⚠️ **WARNING: High risk of YouTube API rate limiting/blocking**
- Only use if the efficient method doesn't work for your use case
- May cause IP blocking that requires VPN to resolve

## Notes & tips

- **API Efficiency**: The default method uses only ~11 YouTube API calls total, making it safe to run regularly without hitting quota limits.
- **Channel Selection**: The efficient method focuses on your most active/relevant subscribed channels rather than checking every subscription.
- **Transcripts**: Not every video has one. Auto‑generated transcripts are often available, but some channels disable transcripts entirely.
- **Age filter**: Set `YT_MAX_AGE_DAYS` to avoid summarizing ancient videos.
- **Joplin**: Point Joplin's monitored folder at the same `OUTPUT_DIR`, or drop the folder into your Syncthing path (e.g. `Documents/ToJoplin`) so it imports automatically.
- **OpenAI**: Generates structured summaries with TL;DR, key takeaways, and suggested follow-up actions.
- **History tracking**: The script now keeps durable per-mode history in `yt_history.db`. On first run it migrates legacy `yt_state.json` entries into SQLite and then relies on the database for filtering.
- **Overlap protection**: If another run in the same mode is already active, the script exits early instead of racing and creating duplicate summaries. Stale lock files are automatically cleaned up when the owning PID is gone.

## Requirements

- Python 3.8+
- Google Cloud project with YouTube Data API v3 enabled
- OAuth credentials for YouTube access
- Optional: OpenAI API key for enhanced summaries

## Dependencies

Key libraries used:
- `youtube-transcript-api==1.2.2` - Transcript extraction with IP blocking protection
- `openai==1.99.9` - AI-powered summaries (optional)  
- `google-api-python-client` - YouTube Data API access
- `sumy` - Local TextRank summarization fallback

## Recent Updates

**v2025.08.13** - Major improvements:
- ✅ **New efficient API method** - 95% fewer YouTube API calls (now default)
- ✅ **Quota exhaustion handling** - Gracefully processes videos even when API quota is exceeded
- ✅ Fixed IP blocking issues with updated transcript API  
- ✅ Enhanced OpenAI integration with structured summaries
- ✅ Added graceful error handling and troubleshooting
- ✅ Support for playlists and individual video URLs
- ✅ Improved state tracking and duplicate prevention
- ✅ Rate limiting protection to prevent YouTube API blocks

---

Made on 2025-08-13.

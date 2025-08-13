# YouTube Subscriptions Transcript Summarizer

Fetch transcripts for the **latest videos from channels you're subscribed to** and save **Markdown summaries** (perfect for Joplin).

## What it does

- Authenticates with your Google account (OAuth, read‑only).
- Finds every channel you're subscribed to and each channel's **Uploads** playlist.
- Collects the most recent videos across all those channels (or specific playlists/URLs).
- Pulls transcripts (including auto‑generated when available) via `youtube-transcript-api`.
- Summarizes each transcript:
  - **Local** TextRank (fallback, no API keys required), or
  - **OpenAI** (recommended if `OPENAI_API_KEY` is set) for high-quality structured summaries.
- Writes clean **Markdown** files to a folder you can point Joplin at (e.g. a Syncthing folder).

## Features

- **Multiple modes**: Process subscriptions, specific playlists, or individual video URLs
- **Smart filtering**: Skip shorts, already processed videos, and videos from your watch history
- **IP blocking protection**: Graceful error handling with helpful troubleshooting messages
- **State tracking**: Remembers processed videos to avoid duplicates
- **High-quality summaries**: OpenAI generates structured summaries with TL;DR, key takeaways, and action items

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

## Troubleshooting

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
- **YT_PER_CHANNEL_LIMIT**: Max videos per channel (default: 3)
- **YT_EXCLUDE_SHORTS**: Skip YouTube Shorts (default: true)
- **OPENAI_API_KEY**: For high-quality structured summaries
- **OUTPUT_DIR**: Where to save markdown files (default: ./ToJoplin)

## Notes & tips

- **Transcripts**: Not every video has one. Auto‑generated transcripts are often available, but some channels disable transcripts entirely.
- **Quota**: The script uses efficient calls (Uploads playlists) but YouTube API has quotas; very large subscriptions might need paging or a smaller `YT_MAX_VIDEOS`.
- **Age filter**: Set `YT_MAX_AGE_DAYS` to avoid summarizing ancient videos.
- **Joplin**: Point Joplin's monitored folder at the same `OUTPUT_DIR`, or drop the folder into your Syncthing path (e.g. `Documents/ToJoplin`) so it imports automatically.
- **OpenAI**: Generates structured summaries with TL;DR, key takeaways, and suggested follow-up actions.
- **State tracking**: The script remembers processed videos in `yt_state.json` to avoid duplicates.

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
- ✅ Fixed IP blocking issues with updated transcript API  
- ✅ Enhanced OpenAI integration with structured summaries
- ✅ Added graceful error handling and troubleshooting
- ✅ Support for playlists and individual video URLs
- ✅ Improved state tracking and duplicate prevention

---

Made on 2025-08-13.

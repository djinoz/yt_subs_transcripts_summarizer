# YouTube Summarizer Scheduler Setup

This document explains how to set up the YouTube summarizer to run automatically every 4 hours using macOS launchctl.

## Files Created

- `com.youtube.summarizer.plist.example` - Example LaunchAgent configuration file
- `run_summarizer.sh.example` - Example shell script wrapper that runs the Python script
- `SCHEDULER_SETUP.md` - This instruction file

## Setup

1. **Copy and customize the example files:**
   ```bash
   cp com.youtube.summarizer.plist.example com.youtube.summarizer.plist
   cp run_summarizer.sh.example run_summarizer.sh
   ```

2. **Update the paths in both files:**
   - Edit `com.youtube.summarizer.plist` and replace `/path/to/your/project/yt_subs_transcripts_summarizer` with your actual project path
   - Edit `run_summarizer.sh` and replace `/path/to/your/project/yt_subs_transcripts_summarizer` with your actual project path

3. **Make the shell script executable:**
   ```bash
   chmod +x run_summarizer.sh
   ```

## Installation Steps

1. **Copy the plist file to LaunchAgents directory:**
   ```bash
   cp com.youtube.summarizer.plist ~/Library/LaunchAgents/
   ```

2. **Load the LaunchAgent:**
   ```bash
   launchctl load ~/Library/LaunchAgents/com.youtube.summarizer.plist
   ```

3. **Start the service immediately (optional):**
   ```bash
   launchctl start com.youtube.summarizer
   ```

## Verification

1. **Check if the service is loaded:**
   ```bash
   launchctl list | grep com.youtube.summarizer
   ```

2. **View logs:**
   ```bash
   # Standard output log
   tail -f /tmp/youtube_summarizer.log
   
   # Error log
   tail -f /tmp/youtube_summarizer_error.log
   ```

## Management Commands

- **Stop the service:**
  ```bash
  launchctl stop com.youtube.summarizer
  ```

- **Unload the service:**
  ```bash
  launchctl unload ~/Library/LaunchAgents/com.youtube.summarizer.plist
  ```

- **Reload after making changes:**
  ```bash
  launchctl unload ~/Library/LaunchAgents/com.youtube.summarizer.plist
  launchctl load ~/Library/LaunchAgents/com.youtube.summarizer.plist
  ```

## Schedule Details

- **Frequency:** Every 4 hours (14400 seconds)
- **Auto-start:** Yes (RunAtLoad = true)
- **Working Directory:** Current project directory
- **Logs:** `/tmp/youtube_summarizer.log` and `/tmp/youtube_summarizer_error.log`

## Troubleshooting

1. **Check if the service is running:**
   ```bash
   launchctl list com.youtube.summarizer
   ```

2. **View recent log entries:**
   ```bash
   tail -20 /tmp/youtube_summarizer.log
   tail -20 /tmp/youtube_summarizer_error.log
   ```

3. **Test the shell script manually:**
   ```bash
   ./run_summarizer.sh
   ```

4. **Common issues:**
   - Ensure `.env` file exists with all required environment variables
   - Check that `client_secret.json` and `token.pickle` files exist
   - Verify Python dependencies are installed: `pip3 install -r requirements.txt`
   - Make sure the shell script is executable: `chmod +x run_summarizer.sh`

## Security Note

The scheduler runs with your user permissions and has access to your YouTube account credentials and OpenAI API key. Ensure your system is properly secured.
import os
import sys
import subprocess
import json
from youtube_transcript_api import YouTubeTranscriptApi, IpBlocked

def check_block():
    """
    Simulates a single transcript fetch to check for IP blocks.
    Uses a known stable video (Google's 'What is a Large Language Model?')
    """
    # video_id = "5slybe_57Y0" 
    video_id = "rlCjvgMVzYY"
    print(f"--- Diagnostic: Checking YouTube Transcript Access for {video_id} ---")
    
    try:
        # 1. Use the actual library used by the summarizer
        api = YouTubeTranscriptApi()
        try:
            # We just need to list transcripts to check for IP block
            listing = api.list(video_id)
            print("✅ SUCCESS: Transcript access via youtube-transcript-api is CLEAR.")
            transcript = listing.find_transcript(['en']) 
            # In library version 1.2.2+, fetch() returns a Transcript object which 
            # contains a 'snippets' attribute.
            fetched_transcript = transcript.fetch()
            for snippet in fetched_transcript.snippets:
                print(f"      [proof-of-text]: {snippet.text[:30]}")
            return True
        except IpBlocked:
            print("❌ BLOCKED: YouTube is still blocking this IP (detected by library).")
            return False
        except Exception as e:
            # If it's not a block but another error, we might still be okay, 
            # but let's be cautious.
            print(f"⚠️ WARNING: Library error: {type(e).__name__}: {str(e)}")
            # Fallback to a metadata check via curl if library fails for other reasons
            pass

        # 2. Fallback/Verification via a simple curl to a metadata-like endpoint
        # This doesn't use yt-dlp, just checks if we get a 403 or a block page
        cmd = [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.stdout.strip() == "403":
             print("❌ BLOCKED: YouTube returned HTTP 403.")
             return False
        
        print("✅ SUCCESS: HTTP access to YouTube seems CLEAR.")
        return True
            
    except Exception as e:
        print(f"❌ DIAGNOSTIC FAILURE: {str(e)}")
        return False

if __name__ == "__main__":
    is_clear = check_block()
    if is_clear:
        sys.exit(0) # Clear
    else:
        sys.exit(1) # Blocked

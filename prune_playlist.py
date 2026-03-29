#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

from yt_subs_summarizer import get_youtube_service, resolve_playlist_id, _execute_with_backoff, exclude_shorts, load_config


def list_playlist_items(youtube, playlist_id: str):
    items = []
    page_token = None
    while True:
        req = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        )
        resp = _execute_with_backoff(req, f"playlistItems.list:prune:{playlist_id}")
        if not resp:
            break
        for it in resp.get("items", []):
            snippet = it.get("snippet", {})
            content = it.get("contentDetails", {})
            vid = content.get("videoId")
            if not vid:
                continue
            items.append({
                "playlistItemId": it.get("id"),
                "videoId": vid,
                "title": snippet.get("title"),
                "channelTitle": snippet.get("videoOwnerChannelTitle") or snippet.get("videoChannelTitle") or snippet.get("channelTitle"),
                "videoOwnerChannelTitle": snippet.get("videoOwnerChannelTitle") or snippet.get("videoChannelTitle") or snippet.get("channelTitle"),
                "publishedAt": content.get("videoPublishedAt") or snippet.get("publishedAt"),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def main():
    ap = argparse.ArgumentParser(description="Prune older items from a YouTube playlist, keeping only the newest N non-short videos.")
    ap.add_argument("--playlist", default="yt-summariser", help="Playlist name or ID")
    ap.add_argument("--keep", type=int, default=8, help="Keep newest N non-short videos")
    ap.add_argument("--include-shorts", action="store_true", help="Count shorts toward keep frontier")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting")
    args = ap.parse_args()

    cfg = load_config(None)
    youtube = get_youtube_service()
    resolved = resolve_playlist_id(youtube, args.playlist)
    if not resolved:
        print(f"Playlist not found: {args.playlist}", file=sys.stderr)
        sys.exit(2)
    playlist_id, playlist_title = resolved

    items = list_playlist_items(youtube, playlist_id)
    if not items:
        print("No playlist items found.")
        return

    frontier_source = items
    if not args.include_shorts:
        frontier_source = exclude_shorts(youtube, items, cfg["SHORTS_MAX_SECONDS"], cfg["LOG_LEVEL"], dryrun=True)

    keep_ids = {item["playlistItemId"] for item in frontier_source[:args.keep]}
    to_remove = [item for item in items if item["playlistItemId"] not in keep_ids]

    print(json.dumps({
        "playlist": playlist_title,
        "playlist_id": playlist_id,
        "total_items": len(items),
        "frontier_candidates": len(frontier_source),
        "keep": len(keep_ids),
        "remove": len(to_remove),
        "dry_run": args.dry_run,
    }, ensure_ascii=False))

    print("KEEP:")
    for i, item in enumerate([x for x in items if x["playlistItemId"] in keep_ids], 1):
        print(json.dumps({"n": i, "playlistItemId": item["playlistItemId"], "videoId": item["videoId"], "title": item["title"]}, ensure_ascii=False))

    print("REMOVE:")
    for i, item in enumerate(to_remove, 1):
        print(json.dumps({"n": i, "playlistItemId": item["playlistItemId"], "videoId": item["videoId"], "title": item["title"]}, ensure_ascii=False))

    if args.dry_run:
        return

    removed = 0
    for item in to_remove:
        req = youtube.playlistItems().delete(id=item["playlistItemId"])
        _execute_with_backoff(req, f"playlistItems.delete:{item['playlistItemId']}")
        removed += 1
    print(f"Removed {removed} items from {playlist_title}.")


if __name__ == "__main__":
    main()

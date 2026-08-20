import datetime as dt
import unittest

import yt_subs_summarizer as yt


class FakeRequest:
    def __init__(self, response, playlist_id=None, page_token=None):
        self._response = response
        self.playlist_id = playlist_id
        self.page_token = page_token

    def execute(self):
        return self._response


class FakeSubscriptions:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def list(self, **kwargs):
        self.calls += 1
        return FakeRequest(self.response)

    def list_next(self, request, response):
        return None


class FakeChannels:
    def __init__(self, by_channel_id):
        self.by_channel_id = by_channel_id
        self.calls = 0

    def list(self, **kwargs):
        self.calls += 1
        ids = kwargs["id"].split(",")
        items = []
        for channel_id in ids:
            channel = self.by_channel_id[channel_id]
            items.append({
                "id": channel_id,
                "snippet": {"title": channel["title"]},
                "contentDetails": {"relatedPlaylists": {"uploads": channel["uploads"]}},
            })
        return FakeRequest({"items": items})

    def list_next(self, request, response):
        return None


class FakePlaylistItems:
    def __init__(self, pages_by_playlist_id):
        self.pages_by_playlist_id = pages_by_playlist_id
        self.calls = []

    def list(self, **kwargs):
        playlist_id = kwargs["playlistId"]
        page_token = kwargs.get("pageToken")
        self.calls.append((playlist_id, page_token, kwargs.get("maxResults")))
        page = self.pages_by_playlist_id[playlist_id][page_token]
        return FakeRequest(page, playlist_id=playlist_id, page_token=page_token)

    def list_next(self, request, response):
        next_token = response.get("nextPageToken")
        if not next_token:
            return None
        playlist_id = getattr(request, "playlist_id", None)
        if playlist_id is None:
            return None
        if next_token not in self.pages_by_playlist_id.get(playlist_id, {}):
            return None
        next_page = self.pages_by_playlist_id[playlist_id][next_token]
        return FakeRequest(next_page, playlist_id=playlist_id, page_token=next_token)



class FakeYouTube:
    def __init__(self, subscriptions_response, channels_by_id, pages_by_playlist_id):
        self._subscriptions = FakeSubscriptions(subscriptions_response)
        self._channels = FakeChannels(channels_by_id)
        self._playlist_items = FakePlaylistItems(pages_by_playlist_id)

    def subscriptions(self):
        return self._subscriptions

    def channels(self):
        return self._channels

    def playlistItems(self):
        return self._playlist_items

    def search(self):
        raise AssertionError("search() should not be used in uploads-playlist mode")


class UploadsScanTests(unittest.TestCase):
    def _item(self, video_id, title, days_ago):
        published = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat()
        return {
            "snippet": {"title": title, "videoOwnerChannelTitle": "Owner"},
            "contentDetails": {"videoId": video_id, "videoPublishedAt": published},
        }

    def test_uploads_scan_pages_until_limit_or_age_cutoff(self):
        subs = {
            "items": [
                {"snippet": {"resourceId": {"channelId": "chan-a"}, "title": "Channel A"}},
            ]
        }
        channels = {
            "chan-a": {"title": "Channel A", "uploads": "uploads-a"},
        }
        pages = {
            "uploads-a": {
                None: {
                    "items": [
                        self._item("v1", "First", 1),
                        self._item("v2", "Second", 2),
                    ],
                    "nextPageToken": "page2",
                },
                "page2": {
                    "items": [
                        self._item("v3", "Third", 3),
                        self._item("v4", "Too old", 20),
                    ],
                },
            }
        }
        yt_service = FakeYouTube(subs, channels, pages)

        videos = yt.get_recent_subscription_videos_efficient(
            yt_service,
            max_videos=10,
            max_age_days=7,
            per_channel_limit=4,
        )

        self.assertEqual([v["videoId"] for v in videos], ["v1", "v2", "v3"])
        self.assertEqual(yt_service.subscriptions().calls, 1)
        self.assertEqual(yt_service.channels().calls, 1)

    def test_iter_recent_from_uploads_does_not_need_search(self):
        subs = {"items": []}
        channels = {}
        pages = {"uploads-a": {None: {"items": []}}}
        yt_service = FakeYouTube(subs, channels, pages)

        videos = yt.iter_recent_from_uploads(
            yt_service,
            [{"playlist_id": "uploads-a", "channel_title": "Channel A"}],
            per_channel_max_age_days=7,
            per_channel_limit=3,
            dryrun=True,
        )

        self.assertEqual(videos, [])


if __name__ == "__main__":
    unittest.main()

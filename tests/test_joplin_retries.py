import os
import unittest
import urllib.error
from unittest.mock import patch

import yt_subs_summarizer as mod


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class JoplinRetryTests(unittest.TestCase):
    def test_joplin_request_retries_transient_urlerror(self):
        calls = {"count": 0}

        def fake_urlopen(req, timeout=30):
            calls["count"] += 1
            if calls["count"] == 1:
                raise urllib.error.URLError("Connection refused")
            return _FakeResponse('{"ok": true}')

        with patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen), patch.object(mod.time, "sleep", return_value=None):
            os.environ["JOPLIN_API_TOKEN"] = "test-token"
            result = mod._joplin_request("GET", "/ping")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["count"], 2)

    def test_save_joplin_note_retries_after_transient_create_failure(self):
        os.environ["JOPLIN_IMPORT_FOLDER_ID"] = "folder123"
        os.environ["JOPLIN_IMPORT_TAGS"] = "yt_subs_transcripts_summarizer,youtube"
        os.environ["JOPLIN_IMPORT_IS_TODO"] = "0"

        video = {
            "videoId": "video123",
            "title": "Retryable Note",
            "publishedAt": "2026-08-25T00:00:00Z",
            "channelTitle": "Test Channel",
        }
        md = '# Retryable Note\n\n---\nvideo_id: "video123"\n'

        find_calls = {"count": 0}

        def fake_find_existing(video_arg, note_title, folder_id):
            find_calls["count"] += 1
            if find_calls["count"] == 1:
                return None
            return {
                "id": "note123",
                "title": note_title,
                "parent_id": folder_id,
                "body": md,
                "source_application": "yt_subs_transcripts_summarizer",
            }

        request_calls = []

        def fake_request(method, path, payload=None):
            request_calls.append((method, path, payload))
            if method == "POST" and path == "/notes":
                raise RuntimeError("Connection error.")
            if method == "PUT" and path == "/notes/note123":
                return {}
            if method == "GET" and path == "/tags?fields=id,title&limit=100&page=1":
                return {
                    "items": [
                        {"id": "tag1", "title": "yt_subs_transcripts_summarizer"},
                        {"id": "tag2", "title": "youtube"},
                    ],
                    "has_more": False,
                }
            if method == "GET" and path == "/notes/note123/tags?fields=id,title":
                return {"items": []}
            if method == "POST" and path == "/tags/tag1/notes":
                return {}
            if method == "POST" and path == "/tags/tag2/notes":
                return {}
            if method == "GET" and path == "/notes/note123?fields=id,title,parent_id,is_todo,body":
                return {
                    "id": "note123",
                    "title": "Retryable Note - 2026-08-25",
                    "parent_id": "folder123",
                    "is_todo": 0,
                    "body": "# Retryable Note\n\nbody",
                }
            raise AssertionError(f"Unexpected request: {(method, path, payload)}")

        with patch.object(mod, "_joplin_find_existing_note", side_effect=fake_find_existing), patch.object(mod, "_joplin_request", side_effect=fake_request), patch.object(mod.time, "sleep", return_value=None):
            note_id = mod.save_joplin_note(video, md, "Retryable Note - 2026-08-25")

        self.assertEqual(note_id, "note123")
        self.assertGreaterEqual(find_calls["count"], 2)
        self.assertEqual(request_calls[0][0:2], ("POST", "/notes"))
        self.assertIn(("PUT", "/notes/note123", {
            "title": "Retryable Note - 2026-08-25",
            "body": md,
            "parent_id": "folder123",
            "is_todo": 0,
            "source": "joplin",
            "source_application": "yt_subs_transcripts_summarizer",
        }), request_calls)
    def test_find_existing_note_reuses_empty_same_title_placeholder(self):
        video = {"videoId": "video123"}
        placeholder = {
            "id": "placeholder123",
            "title": "Retryable Note - 2026-08-25",
            "parent_id": "folder123",
            "body": "",
            "source_application": "",
        }
        calls = []

        def fake_request(method, path, payload=None):
            calls.append((method, path))
            if method == "GET" and path == "/folders/folder123/notes?fields=id,title,parent_id,source_application&limit=100&page=1":
                return {"items": [{"id": "placeholder123", "title": placeholder["title"]}], "has_more": False}
            if method == "GET" and path == "/notes/placeholder123?fields=id,title,parent_id,body,source_application":
                return placeholder
            raise AssertionError((method, path, payload))

        with patch.object(mod, "_joplin_request", side_effect=fake_request):
            found = mod._joplin_find_existing_note(video, placeholder["title"], "folder123")

        self.assertEqual(found, placeholder)

    def test_find_existing_note_matches_quoted_video_id(self):
        video = {"videoId": "video123"}
        note = {
            "id": "note123",
            "title": "Changed title",
            "parent_id": "folder123",
            "body": "---\nvideo_id: \"video123\"\n---\n",
            "source_application": "yt_subs_transcripts_summarizer",
        }

        def fake_request(method, path, payload=None):
            if method == "GET" and path == "/folders/folder123/notes?fields=id,title,parent_id,source_application&limit=100&page=1":
                return {"items": [{"id": "note123", "title": "Changed title"}], "has_more": False}
            if method == "GET" and path == "/notes/note123?fields=id,title,parent_id,body,source_application":
                return note
            raise AssertionError((method, path, payload))

        with patch.object(mod, "_joplin_request", side_effect=fake_request):
            found = mod._joplin_find_existing_note(video, "Original title", "folder123")

        self.assertEqual(found, note)


if __name__ == "__main__":
    unittest.main()

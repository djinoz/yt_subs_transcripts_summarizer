import json
import time
import unittest

from history_db import (
    HistoryDB,
    MODE_PLAYLIST,
    MODE_SUBSCRIPTION,
    MODE_URLS,
    STATUS_SUCCESS,
    STATUS_FAILED,
    ERROR_PERMANENT,
    ERROR_TEMPORARY,
    classify_transcript_failure,
)


class HistoryDBTests(unittest.TestCase):
    def test_migrates_legacy_json_to_subscription_history(self):
        with self.subTest('migration'):
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as td:
                tmp_path = Path(td)
                state_file = tmp_path / 'yt_state.json'
                now = time.time()
                legacy = {
                    'processed_timestamps': {
                        'video000001A': now - 100,
                    },
                    'processed_video_ids': ['video000001B'],
                    'video_errors': {
                        'video000001C': 'NO_TRANSCRIPT_FOUND',
                        'video000001D': 'RequestBlocked',
                    },
                }
                state_file.write_text(json.dumps(legacy), encoding='utf-8')

                db = HistoryDB(str(tmp_path / 'yt_history.db'), state_file=str(state_file))

                a = db.get_entry('video000001A', MODE_SUBSCRIPTION)
                b = db.get_entry('video000001B', MODE_SUBSCRIPTION)
                c = db.get_entry('video000001C', MODE_SUBSCRIPTION)
                d = db.get_entry('video000001D', MODE_SUBSCRIPTION)

                self.assertEqual(a['status'], STATUS_SUCCESS)
                self.assertEqual(b['status'], STATUS_SUCCESS)
                self.assertEqual(c['status'], STATUS_FAILED)
                self.assertEqual(c['error_type'], ERROR_PERMANENT)
                self.assertEqual(d['status'], STATUS_FAILED)
                self.assertEqual(d['error_type'], ERROR_TEMPORARY)

    def test_playlist_success_is_durable_but_subscription_expires_by_retention(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            db = HistoryDB(str(tmp_path / 'yt_history.db'))
            old_ts = time.time() - (400 * 86400)

            db.record_success('video000002A', MODE_PLAYLIST, processed_at=old_ts)
            db.record_success('video000002A', MODE_SUBSCRIPTION, processed_at=old_ts)

            skip_playlist, reason_playlist = db.should_skip('video000002A', MODE_PLAYLIST, success_retention_days=365)
            skip_sub, reason_sub = db.should_skip('video000002A', MODE_SUBSCRIPTION, success_retention_days=365)

            self.assertTrue(skip_playlist)
            self.assertEqual(reason_playlist, 'already_processed')
            self.assertFalse(skip_sub)
            self.assertIsNone(reason_sub)

    def test_permanent_failures_skip_but_temporary_failures_retry(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            db = HistoryDB(str(tmp_path / 'yt_history.db'))
            db.record_failure('video000003A', MODE_SUBSCRIPTION, ERROR_PERMANENT)
            db.record_failure('video000003B', MODE_SUBSCRIPTION, ERROR_TEMPORARY)

            self.assertEqual(db.should_skip('video000003A', MODE_SUBSCRIPTION, 365), (True, 'permanent_failure'))
            self.assertEqual(db.should_skip('video000003B', MODE_SUBSCRIPTION, 365), (False, None))

    def test_modes_are_independent_for_same_video(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            db = HistoryDB(str(tmp_path / 'yt_history.db'))
            db.record_success('video000004A', MODE_PLAYLIST)

            self.assertEqual(db.should_skip('video000004A', MODE_PLAYLIST, 365), (True, 'already_processed'))
            self.assertEqual(db.should_skip('video000004A', MODE_SUBSCRIPTION, 365), (False, None))
            self.assertEqual(db.should_skip('video000004A', MODE_URLS, 365), (False, None))

    def test_classify_transcript_failure_maps_permanent_and_temporary_cases(self):
        self.assertEqual(classify_transcript_failure('TranscriptsDisabled', ''), ('TRANSCRIPTS_DISABLED', ERROR_PERMANENT))
        self.assertEqual(classify_transcript_failure('NoTranscriptFound', ''), ('NO_TRANSCRIPT_FOUND', ERROR_PERMANENT))
        self.assertEqual(
            classify_transcript_failure('CouldNotRetrieveTranscript', 'video unavailable or deleted'),
            ('VIDEO_UNAVAILABLE_OR_DELETED', ERROR_PERMANENT),
        )
        self.assertEqual(
            classify_transcript_failure('CouldNotRetrieveTranscript', 'some transient backend issue'),
            ('TRANSCRIPT_FETCH_ERROR', ERROR_TEMPORARY),
        )


if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from stats_report import analyze_logs, fetch_db_stats
from history_db import HistoryDB, MODE_PLAYLIST, MODE_SUBSCRIPTION, ERROR_PERMANENT, ERROR_TEMPORARY


class StatsReportTests(unittest.TestCase):
    def test_fetch_db_stats_groups_modes_and_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            db = HistoryDB(str(td_path / 'yt_history.db'))
            db.record_success('video000001A', MODE_SUBSCRIPTION)
            db.record_failure('video000001B', MODE_SUBSCRIPTION, ERROR_TEMPORARY)
            db.record_failure('video000001C', MODE_PLAYLIST, ERROR_PERMANENT)

            stats = fetch_db_stats(td_path / 'yt_history.db', [MODE_SUBSCRIPTION, MODE_PLAYLIST], None, None)
            self.assertEqual(stats['total'], 3)
            self.assertEqual(stats['success'], 1)
            self.assertEqual(stats['failed_temporary'], 1)
            self.assertEqual(stats['failed_permanent'], 1)
            self.assertEqual(stats['by_mode'][MODE_SUBSCRIPTION]['success'], 1)
            self.assertEqual(stats['by_mode'][MODE_PLAYLIST]['failed_permanent'], 1)

    def test_analyze_logs_extracts_operational_counts(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            logs_dir = td_path / 'logs'
            logs_dir.mkdir()
            (logs_dir / 'vpn_run_20260329_120000.log').write_text(
                '\n'.join([
                    '[2026-03-29 12:00:00] After Shorts filter: kept 8/11',
                    '[2026-03-29 12:00:01] ERROR: VPN failed to establish. Checking OpenVPN log...',
                    '[2026-03-29 12:00:02] [QUOTA] videos.list:shorts_filter: YouTube API quota exhausted.',
                    '[2026-03-29 12:00:03] [warn] abc123def45 [RequestBlocked] — transient/unknown, will retry in future runs',
                    '[2026-03-29 12:00:04] [warn] failed to save/mark abc123def45: Ollama API call failed: timed out',
                ]),
                encoding='utf-8',
            )
            stats = analyze_logs(td_path, None, None)
            self.assertEqual(stats['log_files_scanned'], 1)
            self.assertEqual(stats['shorts_filtered'], 3)
            self.assertEqual(stats['vpn_failures'], 1)
            self.assertEqual(stats['yt_api_failures'], 1)
            self.assertEqual(stats['quota_exhausted_events'], 1)
            self.assertEqual(stats['transcript_fetch_failures'], 1)
            self.assertEqual(stats['summary_model_save_failures'], 1)


if __name__ == '__main__':
    unittest.main()

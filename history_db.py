import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

MODE_SUBSCRIPTION = "subscription"
MODE_PLAYLIST = "playlist"
MODE_URLS = "urls"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

ERROR_TEMPORARY = "temporary"
ERROR_PERMANENT = "permanent"

LEGACY_PERMANENT_ERRORS = {
    "TRANSCRIPTS_DISABLED",
    "TranscriptsDisabled",
    "VIDEO_UNAVAILABLE_OR_DELETED",
    "NO_TRANSCRIPT_FOUND",
    "NoTranscriptFound",
    "VIDEO_PRIVATE_OR_RESTRICTED",
}

LEGACY_TRANSIENT_ERRORS = {
    "TRANSCRIPT_FETCH_ERROR",
    "CouldNotRetrieveTranscript",
    "RequestBlocked",
    "IpBlocked",
    "AgeRestricted",
}


def _now_ts() -> float:
    return time.time()


class HistoryDB:
    def __init__(self, db_path: str, state_file: Optional[str] = None):
        self.db_path = db_path
        self.state_file = state_file
        self._init_db()
        self.migrate_from_legacy_json_if_needed()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history (
                    video_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    processed_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    error_type TEXT,
                    title TEXT,
                    channel TEXT,
                    PRIMARY KEY (video_id, mode)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_mode_status_processed_at ON history(mode, status, processed_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def migrate_from_legacy_json_if_needed(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'legacy_migration_done'").fetchone()
            if row and row[0] == "1":
                return

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_migration_done', '1')"
                )
                conn.commit()
            return

        processed_timestamps = data.get("processed_timestamps", {}) or {}
        old_list = data.get("processed_video_ids", []) or []
        video_errors = data.get("video_errors", {}) or {}
        now = _now_ts()

        with self._connect() as conn:
            for vid in old_list:
                processed_timestamps.setdefault(vid, now)

            for vid, ts in processed_timestamps.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO history(video_id, mode, processed_at, status, error_type, title, channel)
                    VALUES(?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (vid, MODE_SUBSCRIPTION, float(ts or now), STATUS_SUCCESS),
                )

            for vid, cause in video_errors.items():
                error_type = classify_legacy_error_type(cause)
                conn.execute(
                    """
                    INSERT INTO history(video_id, mode, processed_at, status, error_type, title, channel)
                    VALUES(?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(video_id, mode) DO UPDATE SET
                        processed_at=excluded.processed_at,
                        status=excluded.status,
                        error_type=excluded.error_type
                    """,
                    (vid, MODE_SUBSCRIPTION, now, STATUS_FAILED, error_type),
                )

            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_migration_done', '1')"
            )
            conn.commit()

    def record_success(self, video_id: str, mode: str, title: Optional[str] = None, channel: Optional[str] = None, processed_at: Optional[float] = None):
        ts = float(processed_at or _now_ts())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO history(video_id, mode, processed_at, status, error_type, title, channel)
                VALUES(?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(video_id, mode) DO UPDATE SET
                    processed_at=excluded.processed_at,
                    status=excluded.status,
                    error_type=NULL,
                    title=COALESCE(excluded.title, history.title),
                    channel=COALESCE(excluded.channel, history.channel)
                """,
                (video_id, mode, ts, STATUS_SUCCESS, title, channel),
            )
            conn.commit()

    def record_failure(self, video_id: str, mode: str, error_type: str, title: Optional[str] = None, channel: Optional[str] = None, processed_at: Optional[float] = None):
        ts = float(processed_at or _now_ts())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO history(video_id, mode, processed_at, status, error_type, title, channel)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, mode) DO UPDATE SET
                    processed_at=excluded.processed_at,
                    status=excluded.status,
                    error_type=excluded.error_type,
                    title=COALESCE(excluded.title, history.title),
                    channel=COALESCE(excluded.channel, history.channel)
                """,
                (video_id, mode, ts, STATUS_FAILED, error_type, title, channel),
            )
            conn.commit()

    def get_entry(self, video_id: str, mode: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT video_id, mode, processed_at, status, error_type, title, channel FROM history WHERE video_id=? AND mode=?",
                (video_id, mode),
            ).fetchone()
            return dict(row) if row else None

    def should_skip(self, video_id: str, mode: str, success_retention_days: int = 365, skip_permanent_failures: bool = True) -> Tuple[bool, Optional[str]]:
        entry = self.get_entry(video_id, mode)
        if not entry:
            return False, None

        if entry["status"] == STATUS_FAILED:
            if skip_permanent_failures and entry.get("error_type") == ERROR_PERMANENT:
                return True, "permanent_failure"
            return False, None

        if entry["status"] != STATUS_SUCCESS:
            return False, None

        if mode == MODE_PLAYLIST:
            return True, "already_processed"

        if success_retention_days <= 0:
            return False, None

        cutoff = _now_ts() - (success_retention_days * 86400)
        if float(entry["processed_at"]) > cutoff:
            return True, "already_processed"
        return False, None

    def prune_successes(self, mode: str, retention_days: int):
        if mode == MODE_PLAYLIST or retention_days <= 0:
            return
        cutoff = _now_ts() - (retention_days * 86400)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM history WHERE mode=? AND status=? AND processed_at <= ?",
                (mode, STATUS_SUCCESS, cutoff),
            )
            conn.commit()


def classify_legacy_error_type(cause: Optional[str]) -> str:
    if cause in LEGACY_PERMANENT_ERRORS:
        return ERROR_PERMANENT
    if cause in LEGACY_TRANSIENT_ERRORS:
        return ERROR_TEMPORARY
    return ERROR_TEMPORARY


def classify_transcript_failure(exc_type_name: str, exc_message: str) -> Tuple[str, str]:
    error_msg = (exc_message or "").lower()
    if exc_type_name == "TranscriptsDisabled":
        return "TRANSCRIPTS_DISABLED", ERROR_PERMANENT
    if exc_type_name == "NoTranscriptFound":
        return "NO_TRANSCRIPT_FOUND", ERROR_PERMANENT
    if exc_type_name == "CouldNotRetrieveTranscript":
        if any(x in error_msg for x in ["unavailable", "deleted", "removed", "not found"]):
            return "VIDEO_UNAVAILABLE_OR_DELETED", ERROR_PERMANENT
        if any(x in error_msg for x in ["private", "access", "permission"]):
            return "VIDEO_PRIVATE_OR_RESTRICTED", ERROR_PERMANENT
        return "TRANSCRIPT_FETCH_ERROR", ERROR_TEMPORARY
    return exc_type_name, ERROR_TEMPORARY

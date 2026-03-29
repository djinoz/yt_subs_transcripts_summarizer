#!/usr/bin/env python3
import argparse
import json
import sqlite3
import sys
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from history_db import (
    MODE_PLAYLIST,
    MODE_SUBSCRIPTION,
    MODE_URLS,
    STATUS_FAILED,
    STATUS_SUCCESS,
    ERROR_PERMANENT,
    ERROR_TEMPORARY,
)

LOG_PATTERNS = [
    'logs/vpn_run_*.log',
    'logs/playlist_queue_*.log',
    'logs/summarizer_*.log',
]


def parse_time_arg(value: Optional[str], default: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    if value.isdigit():
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f'invalid datetime: {value}') from e


def resolve_modes(mode_arg: str) -> List[str]:
    if mode_arg == 'all':
        return [MODE_SUBSCRIPTION, MODE_PLAYLIST, MODE_URLS]
    return [mode_arg]


def fetch_db_stats(db_path: Path, modes: List[str], start_ts: Optional[float], end_ts: Optional[float]) -> Dict:
    where = []
    params: List[object] = []
    if modes:
        where.append('mode IN (%s)' % ','.join('?' for _ in modes))
        params.extend(modes)
    if start_ts is not None:
        where.append('processed_at >= ?')
        params.append(start_ts)
    if end_ts is not None:
        where.append('processed_at <= ?')
        params.append(end_ts)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    out = {
        'total': 0,
        'success': 0,
        'failed_temporary': 0,
        'failed_permanent': 0,
        'failed_unknown': 0,
        'by_mode': {},
    }

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f'''
            SELECT mode, status, error_type, COUNT(*) AS c
            FROM history
            {where_sql}
            GROUP BY mode, status, error_type
            ORDER BY mode, status, error_type
            ''',
            params,
        ).fetchall()

    for row in rows:
        mode = row['mode']
        status = row['status']
        error_type = row['error_type']
        count = int(row['c'])
        bucket = out['by_mode'].setdefault(mode, {
            'total': 0,
            'success': 0,
            'failed_temporary': 0,
            'failed_permanent': 0,
            'failed_unknown': 0,
        })
        bucket['total'] += count
        out['total'] += count
        if status == STATUS_SUCCESS:
            bucket['success'] += count
            out['success'] += count
        elif status == STATUS_FAILED:
            if error_type == ERROR_TEMPORARY:
                bucket['failed_temporary'] += count
                out['failed_temporary'] += count
            elif error_type == ERROR_PERMANENT:
                bucket['failed_permanent'] += count
                out['failed_permanent'] += count
            else:
                bucket['failed_unknown'] += count
                out['failed_unknown'] += count
    return out


def parse_log_timestamp(line: str) -> Optional[dt.datetime]:
    if not line.startswith('['):
        return None
    end = line.find(']')
    if end <= 1:
        return None
    ts_text = line[1:end]
    try:
        return dt.datetime.strptime(ts_text, '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def line_in_window(line: str, start: Optional[dt.datetime], end: Optional[dt.datetime]) -> bool:
    ts = parse_log_timestamp(line)
    if ts is None:
        return True
    if start and ts < start:
        return False
    if end and ts > end:
        return False
    return True


def analyze_logs(repo_dir: Path, start: Optional[dt.datetime], end: Optional[dt.datetime]) -> Dict:
    stats = {
        'log_files_scanned': 0,
        'shorts_filtered': 0,
        'vpn_failures': 0,
        'yt_api_failures': 0,
        'transcript_fetch_failures': 0,
        'summary_model_save_failures': 0,
        'quota_exhausted_events': 0,
    }

    files: List[Path] = []
    for pattern in LOG_PATTERNS:
        files.extend(sorted(repo_dir.glob(pattern)))

    for path in files:
        try:
            lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
        except Exception:
            continue
        stats['log_files_scanned'] += 1
        for line in lines:
            if not line_in_window(line, start, end):
                continue
            if 'After Shorts filter: kept ' in line:
                try:
                    kept_part = line.split('kept ', 1)[1]
                    kept, total = kept_part.split('/', 1)
                    stats['shorts_filtered'] += max(0, int(total.strip()) - int(kept.strip()))
                except Exception:
                    pass
            if 'ERROR: VPN failed to establish' in line:
                stats['vpn_failures'] += 1
            if '[QUOTA]' in line or '[fail] subscriptions.list' in line or '[fail] channels.list' in line or '[fail] search.list' in line or '[fail] videos.list' in line or '[error] Efficient API failed' in line or '[error] Legacy API failed' in line:
                stats['yt_api_failures'] += 1
            if '[QUOTA]' in line:
                stats['quota_exhausted_events'] += 1
            if '[warn]' in line and ('TRANSCRIPT_FETCH_ERROR' in line or 'RequestBlocked' in line or 'transient/unknown' in line or 'treated as permanent' in line):
                stats['transcript_fetch_failures'] += 1
            if '[warn] failed to save/mark ' in line:
                stats['summary_model_save_failures'] += 1
    return stats


def maybe_write_pie_chart(data: Dict[str, int], out_path: Path) -> Optional[str]:
    try:
        import matplotlib.pyplot as plt  # type: ignore
    except Exception:
        return None

    labels = []
    values = []
    for key, value in data.items():
        if value > 0:
            labels.append(key.replace('_', ' '))
            values.append(value)
    if not values:
        labels = ['no data']
        values = [1]

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.pie(values, labels=labels, autopct='%1.1f%%', startangle=90)
    ax.axis('equal')
    ax.set_title('YouTube summarizer history breakdown')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    return str(out_path)


def render_text(report: Dict) -> str:
    lines = []
    lines.append('YouTube summarizer stats')
    lines.append(f"Window: {report['window']['start']} -> {report['window']['end']}")
    lines.append(f"Modes: {', '.join(report['modes'])}")
    lines.append('')
    db = report['db']
    lines.append('DB history')
    lines.append(f"- total: {db['total']}")
    lines.append(f"- success: {db['success']}")
    lines.append(f"- failed temporary: {db['failed_temporary']}")
    lines.append(f"- failed permanent: {db['failed_permanent']}")
    lines.append(f"- failed unknown: {db['failed_unknown']}")
    if db['by_mode']:
        lines.append('- by mode:')
        for mode, mode_stats in sorted(db['by_mode'].items()):
            lines.append(
                f"  - {mode}: total={mode_stats['total']} success={mode_stats['success']} temp={mode_stats['failed_temporary']} perm={mode_stats['failed_permanent']} unknown={mode_stats['failed_unknown']}"
            )
    lines.append('')
    log_stats = report['logs']
    lines.append('Log-derived operational stats')
    lines.append(f"- log files scanned: {log_stats['log_files_scanned']}")
    lines.append(f"- shorts filtered: {log_stats['shorts_filtered']}")
    lines.append(f"- vpn failures: {log_stats['vpn_failures']}")
    lines.append(f"- yt api failures: {log_stats['yt_api_failures']}")
    lines.append(f"- transcript fetch failures: {log_stats['transcript_fetch_failures']}")
    lines.append(f"- summary/model/save failures: {log_stats['summary_model_save_failures']}")
    lines.append(f"- quota exhausted events: {log_stats['quota_exhausted_events']}")
    if report.get('chart_path'):
        lines.append(f"- pie chart: {report['chart_path']}")
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description='Ad hoc stats for yt_subs_transcripts_summarizer history and logs.')
    parser.add_argument('--db', default='yt_history.db', help='Path to SQLite history DB')
    parser.add_argument('--mode', choices=['subscription', 'playlist', 'urls', 'all'], default='all')
    parser.add_argument('--since', help='Start datetime (ISO-8601 or unix timestamp)')
    parser.add_argument('--until', help='End datetime (ISO-8601 or unix timestamp)')
    parser.add_argument('--json', action='store_true', help='Emit JSON')
    parser.add_argument('--pie-chart', help='Optional output PNG path (requires matplotlib)')
    args = parser.parse_args()

    repo_dir = Path.cwd()
    db_path = (repo_dir / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db)
    if not db_path.exists():
        print(f'DB not found: {db_path}', file=sys.stderr)
        return 2

    start_dt = parse_time_arg(args.since)
    end_dt = parse_time_arg(args.until)
    start_ts = start_dt.timestamp() if start_dt else None
    end_ts = end_dt.timestamp() if end_dt else None
    modes = resolve_modes(args.mode)

    report = {
        'window': {
            'start': start_dt.isoformat() if start_dt else None,
            'end': end_dt.isoformat() if end_dt else None,
        },
        'modes': modes,
        'db': fetch_db_stats(db_path, modes, start_ts, end_ts),
        'logs': analyze_logs(repo_dir, start_dt, end_dt),
    }

    if args.pie_chart:
        chart_path = maybe_write_pie_chart(
            {
                'success': report['db']['success'],
                'failed_temporary': report['db']['failed_temporary'],
                'failed_permanent': report['db']['failed_permanent'],
                'failed_unknown': report['db']['failed_unknown'],
            },
            Path(args.pie_chart),
        )
        if chart_path:
            report['chart_path'] = chart_path
        else:
            report['chart_path'] = None
            report['chart_note'] = 'matplotlib not available; skipped pie chart generation'

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
        if report.get('chart_note'):
            print(f"\nNote: {report['chart_note']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

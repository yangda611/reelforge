"""项目级视频请求使用记录。

只要第三方平台返回 request/task id，就立刻记为 accepted。
后续再按 completed / downloaded / failed 更新状态。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from novelvideo.config import OUTPUT_DIR, STATE_DIR
from novelvideo.sqlite_pragmas import configure_sqlite_connection
from novelvideo.sqlite_schema import ensure_sqlite_schema


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS video_request_usage (
    request_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model_name TEXT,
    episode INTEGER,
    beat_num INTEGER,
    task_type TEXT,
    duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'accepted',
    cost_estimate REAL,
    accepted_at TEXT NOT NULL,
    completed_at TEXT,
    downloaded_at TEXT,
    updated_at TEXT NOT NULL,
    error_message TEXT,
    request_digest TEXT,
    poll_url TEXT,
    resumable INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_video_request_usage_episode
ON video_request_usage(episode, beat_num, accepted_at DESC);
"""

_SCHEMA_COMPONENT = "video_request_usage"
# MIGRATION CONTRACT: increment this whenever _SCHEMA_SQL changes.
_SCHEMA_VERSION = 2


def _ensure_video_request_usage_schema(schema_conn: sqlite3.Connection) -> None:
    schema_conn.executescript(_SCHEMA_SQL)
    existing_columns = {
        str(row[1])
        for row in schema_conn.execute("PRAGMA table_info(video_request_usage)").fetchall()
    }
    if "request_digest" not in existing_columns:
        schema_conn.execute(
            "ALTER TABLE video_request_usage ADD COLUMN request_digest TEXT"
        )
    if "poll_url" not in existing_columns:
        schema_conn.execute(
            "ALTER TABLE video_request_usage ADD COLUMN poll_url TEXT"
        )
    if "resumable" not in existing_columns:
        schema_conn.execute(
            "ALTER TABLE video_request_usage ADD COLUMN resumable INTEGER NOT NULL DEFAULT 0"
        )
    schema_conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_video_request_usage_resume
        ON video_request_usage(request_digest, accepted_at DESC)
        """
    )


def get_video_request_usage_db_path(project_output_dir: str | Path) -> Path:
    project_output_dir = Path(project_output_dir).resolve()
    output_root = Path(OUTPUT_DIR).resolve()
    state_root = Path(STATE_DIR).resolve()
    try:
        rel = project_output_dir.relative_to(output_root)
    except ValueError:
        return (project_output_dir / "data.db").resolve()
    if len(rel.parts) >= 2:
        # Always anchor on user/project, even if a subdirectory was passed.
        from novelvideo.utils.project_paths import ProjectPaths

        user, project = rel.parts[0], rel.parts[1]
        ProjectPaths(user, project).bootstrap_from_legacy_output()
        return (state_root / user / project / "data.db").resolve()
    return (project_output_dir / "data.db").resolve()


@contextmanager
def _connect(project_output_dir: str | Path):
    db_path = get_video_request_usage_db_path(project_output_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_sqlite_schema(
        db_path,
        component=_SCHEMA_COMPONENT,
        version=_SCHEMA_VERSION,
        initialize=_ensure_video_request_usage_schema,
    )
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    configure_sqlite_connection(conn, set_journal_mode=False)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_video_request(
    *,
    project_output_dir: str | Path,
    request_id: str,
    provider: str,
    model_name: str,
    episode: int | None,
    beat_num: int | None,
    task_type: str | None,
    duration_seconds: float | None,
    cost_estimate: float | None = None,
    request_digest: str | None = None,
    poll_url: str | None = None,
    resumable: bool = False,
) -> None:
    now = datetime.now().isoformat()
    with _connect(project_output_dir) as conn:
        conn.execute(
            """
            INSERT INTO video_request_usage (
                request_id, provider, model_name, episode, beat_num, task_type,
                duration_seconds, status, cost_estimate, accepted_at, updated_at,
                request_digest, poll_url, resumable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (
                request_id,
                provider,
                model_name,
                episode,
                beat_num,
                task_type or "",
                duration_seconds,
                cost_estimate,
                now,
                now,
                request_digest,
                poll_url,
                1 if resumable else 0,
            ),
        )


def update_video_request_status(
    *,
    project_output_dir: str | Path,
    request_id: str,
    status: str,
    error_message: str | None = None,
    resumable: bool | None = None,
) -> None:
    now = datetime.now().isoformat()
    completed_at = now if status in {"completed", "downloaded"} else None
    downloaded_at = now if status == "downloaded" else None
    if resumable is None:
        resumable_value = 0 if status in {"completed", "downloaded"} else None
    else:
        resumable_value = 1 if resumable else 0
    with _connect(project_output_dir) as conn:
        conn.execute(
            """
            UPDATE video_request_usage
            SET status = ?,
                updated_at = ?,
                completed_at = COALESCE(?, completed_at),
                downloaded_at = COALESCE(?, downloaded_at),
                error_message = COALESCE(?, error_message),
                resumable = COALESCE(?, resumable)
            WHERE request_id = ?
            """,
            (
                status,
                now,
                completed_at,
                downloaded_at,
                error_message,
                resumable_value,
                request_id,
            ),
        )


def find_resumable_video_request(
    *,
    project_output_dir: str | Path,
    request_digest: str,
    provider: str = "newapi",
    model_name: str | None = None,
    max_age_seconds: int = 24 * 3600,
) -> dict[str, str] | None:
    if not request_digest:
        return None
    params: list[object] = [request_digest, provider]
    model_clause = ""
    if model_name:
        model_clause = "AND model_name = ?"
        params.append(model_name)
    with _connect(project_output_dir) as conn:
        row = conn.execute(
            f"""
            SELECT request_id, poll_url, status, accepted_at
            FROM video_request_usage
            WHERE request_digest = ?
              AND provider = ?
              {model_clause}
              AND resumable = 1
              AND status IN ('accepted', 'interrupted', 'delivery_pending')
            ORDER BY accepted_at DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    if not row:
        return None
    accepted_at_raw = str(row[3] or "").strip()
    if max_age_seconds > 0 and accepted_at_raw:
        try:
            accepted_dt = datetime.fromisoformat(accepted_at_raw)
        except ValueError:
            accepted_dt = None
        if accepted_dt is not None and (datetime.now() - accepted_dt) > timedelta(
            seconds=max_age_seconds
        ):
            return None
    return {
        "request_id": str(row[0]),
        "poll_url": str(row[1] or ""),
        "status": str(row[2] or ""),
        "accepted_at": accepted_at_raw,
    }


def count_video_beat_attempts(
    *,
    project_output_dir: str | Path,
    episode: int | None,
    beat_num: int | None,
    task_types: tuple[str, ...] = ("single_video", "batch_video"),
) -> int:
    if episode is None or beat_num is None:
        return 0
    placeholders = ", ".join("?" for _ in task_types)
    with _connect(project_output_dir) as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM video_request_usage
            WHERE episode = ? AND beat_num = ? AND task_type IN ({placeholders})
            """,
            (episode, beat_num, *task_types),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def get_video_usage_summary(
    *,
    project_output_dir: str | Path,
    task_types: tuple[str, ...] | None = None,
    episode: int | None = None,
) -> dict:
    today = datetime.now().date().isoformat()
    where = []
    params: list[object] = []
    if task_types:
        placeholders = ", ".join("?" for _ in task_types)
        where.append(f"task_type IN ({placeholders})")
        params.extend(task_types)
    if episode is not None:
        where.append("episode = ?")
        params.append(episode)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    today_sql = f"{where_sql} {'AND' if where_sql else 'WHERE'} substr(accepted_at, 1, 10) = ?"

    with _connect(project_output_dir) as conn:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0)
            FROM video_request_usage
            {where_sql}
            """,
            tuple(params),
        ).fetchone()
        today_row = conn.execute(
            f"""
            SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0)
            FROM video_request_usage
            {today_sql}
            """,
            (*params, today),
        ).fetchone()

    return {
        "total_requests": int(total_row[0] or 0) if total_row else 0,
        "total_duration_seconds": float(total_row[1] or 0.0) if total_row else 0.0,
        "today_requests": int(today_row[0] or 0) if today_row else 0,
        "today_duration_seconds": float(today_row[1] or 0.0) if today_row else 0.0,
    }

"""Deterministic, project- and episode-isolated two-stage autopilot for mainline episodes.

Isolation & Concurrency Contract
--------------------------------
1. **Project isolation**:
   - All autopilot state for a project lives strictly inside ``ctx.state_dir / "autopilot_runs.db"``
     (SQLite WAL + busy_timeout=10000 via ``ensure_sqlite_schema``).
   - Project A and Project B share zero state tables, zero locks, and zero workers.
   - A tiny global index (``STATE_DIR / "autopilot_active_projects.db"``) records only
     ``(project_id, state_dir)`` pointers for server-restart recovery; runtime updates
     never touch the global index.

2. **Episode isolation & concurrency within the same project**:
   - Each ``(project_id, episode)`` has its own DB row (``UNIQUE(project_id, episode)``),
     lease, and independent ``asyncio.Task`` worker.
   - Episode 1 waiting at ``awaiting_asset_review`` or paused on an error never blocks
     Episode 2 or Episode 3.
   - Project-global prerequisites (``build_characters``, ``build_scenes``, ``build_episodes``)
     and shared project asset images (``character_portrait``, ``identity_image``,
     ``scene_reference_asset``, ``prop_reference_asset``) use canonical ``episode=0`` scopes
     so concurrent episodes atomically deduplicate via ``reserve_task_for_project`` and await
     the same task without duplicate generation or double billing.
   - Only ``identity_planner`` (which updates ``characters.identities`` rows) is serialized
     per ``project_id`` so concurrent episodes never overwrite each other's newly discovered
     identities and instead reuse them. Different projects never block each other.
   - Cancelling Episode N cancels ONLY Episode N's worker and tasks strictly owned by
     Episode N (``task.episode == N``); it NEVER cancels shared ``episode=0`` tasks or
     other episodes' tasks.
   - Queue/lane admission limits (``ProjectTaskLimitExceeded``, etc.) act as backpressure:
     workers wait and retry submitting smoothly instead of failing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from novelvideo.i18n_message import lmsg
from novelvideo.project_context import ProjectContext
from novelvideo.sqlite_pragmas import configure_sqlite_connection
from novelvideo.sqlite_schema import ensure_sqlite_schema
from novelvideo.task_backend.limits import (
    ChannelTaskLimitExceeded,
    GlobalLaneQueueLimitExceeded,
    ProjectTaskLimitExceeded,
    ProjectUserTaskLimitExceeded,
    UserTaskLimitExceeded,
)
from novelvideo.task_state import (
    ACTIVE_PROJECT_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskState,
    get_task_manager,
)

logger = logging.getLogger("novelvideo.autopilot")

AUTOPILOT_SCHEMA_COMPONENT = "episode_autopilot"
AUTOPILOT_SCHEMA_VERSION = 1
INDEX_SCHEMA_COMPONENT = "episode_autopilot_index"
INDEX_SCHEMA_VERSION = 1

ACTIVE_AUTOPILOT_STATUSES = frozenset({"phase1_running", "phase2_running"})
STOPPABLE_AUTOPILOT_STATUSES = frozenset(
    {"phase1_running", "awaiting_asset_review", "phase2_running", "paused"}
)

POLL_SECONDS = 1.0
BACKPRESSURE_SECONDS = 2.0
LEASE_SECONDS = 45.0
RETRY_BASE_SECONDS = 4.0
RETRY_MAX_SECONDS = 30.0

_LANE_LIMIT_EXCEPTIONS = (
    ProjectTaskLimitExceeded,
    ProjectUserTaskLimitExceeded,
    GlobalLaneQueueLimitExceeded,
    ChannelTaskLimitExceeded,
    UserTaskLimitExceeded,
)

_FATAL_ACCOUNT_MARKERS = (
    "insufficient_credits",
    "insufficient credits",
    "billing_rule_not_configured",
    "余额不足",
    "积分不足",
    "点数不足",
    "额度耗尽",
    "quota has been exhausted",
    "quota exceeded",
)

_EXTRA_RETRYABLE_MARKERS = (
    "database is locked",
    "sqlite3.operationalerror",
    "temporarily unavailable",
    "rate limit",
    "too many requests",
    "connection reset",
    "connection aborted",
    "read timed out",
    "connect timeout",
    "timed out",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _state_root() -> Path:
    configured = os.environ.get("NOVELVIDEO_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    from novelvideo.config import STATE_DIR

    return Path(STATE_DIR)


def _serialize_context(ctx: ProjectContext) -> str:
    data = asdict(ctx)
    for key in ("output_dir", "state_dir", "runtime_dir"):
        data[key] = str(data[key])
    data["requester_principals"] = [list(item) for item in ctx.requester_principals]
    return json.dumps(data, ensure_ascii=False)


def _deserialize_context(raw: str) -> ProjectContext:
    data = json.loads(raw)
    for key in ("output_dir", "state_dir", "runtime_dir"):
        data[key] = Path(data[key])
    data["requester_principals"] = tuple(
        tuple(item) for item in (data.get("requester_principals") or [])
    )
    return ProjectContext(**data)


def classify_autopilot_error(error: str | None) -> tuple[str, bool, bool]:
    """Return ``(category, retryable, fatal_for_run)`` for a failed task or error.

    ``fatal_for_run=True`` means continuing other items in the same episode is futile
    (e.g. account authentication failure, exhausted credits/quota).
    """
    raw = str(error or "").strip()
    lower = raw.lower()
    if not lower:
        return "unknown", False, False

    if any(marker in lower for marker in _FATAL_ACCOUNT_MARKERS):
        category = (
            "insufficient_credits"
            if ("credit" in lower or "余额" in lower or "积分" in lower or "点数" in lower)
            else "quota_exhausted"
        )
        return category, False, True

    from novelvideo.freezone.workflow_runs import classify_workflow_error

    category, retryable = classify_workflow_error(raw)
    if category in {"authentication", "quota_exhausted"}:
        return category, False, True
    if category in {"content_policy", "invalid_request", "model_unavailable", "artifact_missing"}:
        return category, False, False
    if retryable:
        return category, True, False
    if any(marker in lower for marker in _EXTRA_RETRYABLE_MARKERS):
        return "transient_upstream", True, False
    return category, False, False


@dataclass(frozen=True)
class AutopilotOptions:
    mode: str = "two_stage"  # two_stage | full_auto
    video_backend: str | None = None
    resolution: str | None = None
    ratio: str | None = None
    tail_anchor: bool = False
    max_segment_seconds: int = 30
    max_retries: int = 2
    max_concurrent_videos: int = 4
    max_concurrent_assets: int = 6
    add_subtitles: bool = False
    add_bgm: bool = False
    use_director_render: bool = False
    force_regenerate_videos: bool = False
    target_beat_numbers: tuple[int, ...] = ()
    skip_compose: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target_beat_numbers"] = list(self.target_beat_numbers)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AutopilotOptions":
        data = dict(raw or {})
        mode = str(data.get("mode") or "two_stage").strip()
        if mode not in {"two_stage", "full_auto"}:
            mode = "two_stage"
        targets = tuple(
            int(n) for n in (data.get("target_beat_numbers") or []) if int(n) > 0
        )
        return cls(
            mode=mode,
            video_backend=(
                str(data["video_backend"]).strip()
                if data.get("video_backend")
                else None
            ),
            resolution=(
                str(data["resolution"]).strip() if data.get("resolution") else None
            ),
            ratio=str(data["ratio"]).strip() if data.get("ratio") else None,
            tail_anchor=bool(data.get("tail_anchor", False)),
            max_segment_seconds=max(5, min(60, int(data.get("max_segment_seconds") or 30))),
            max_retries=max(0, min(5, int(data.get("max_retries", 2)))),
            max_concurrent_videos=max(
                1, min(10, int(data.get("max_concurrent_videos") or 4))
            ),
            max_concurrent_assets=max(
                1, min(10, int(data.get("max_concurrent_assets") or 6))
            ),
            add_subtitles=bool(data.get("add_subtitles", False)),
            add_bgm=bool(data.get("add_bgm", False)),
            use_director_render=bool(data.get("use_director_render", False)),
            force_regenerate_videos=bool(data.get("force_regenerate_videos", False)),
            target_beat_numbers=targets,
            skip_compose=bool(data.get("skip_compose", False)),
        )

    def with_overrides(self, overrides: dict[str, Any] | None) -> "AutopilotOptions":
        if not overrides:
            return self
        merged = self.to_dict()
        for key, value in overrides.items():
            if value is not None and key in merged:
                merged[key] = value
        return AutopilotOptions.from_dict(merged)


@dataclass(frozen=True)
class EpisodeAutopilotRun:
    run_id: str
    project_id: str
    episode: int
    status: str
    phase: str
    current_step: str
    mode: str
    options_json: str
    progress_json: str
    context_json: str
    last_error: str
    created_at: str
    updated_at: str

    @property
    def options(self) -> AutopilotOptions:
        try:
            parsed = json.loads(self.options_json or "{}")
        except json.JSONDecodeError:
            parsed = {}
        return AutopilotOptions.from_dict(parsed)

    @property
    def progress(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.progress_json or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    @property
    def context(self) -> ProjectContext:
        return _deserialize_context(self.context_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "episode": self.episode,
            "status": self.status,
            "phase": self.phase,
            "current_step": self.current_step,
            "mode": self.mode,
            "options": self.options.to_dict(),
            "progress": self.progress,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProjectAutopilotStore:
    """Strictly project-scoped SQLite store living at ``<state_dir>/autopilot_runs.db``."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "autopilot_runs.db"

    def _ensure_schema(self) -> None:
        def initialize(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS episode_autopilot_runs (
                  run_id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  episode INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  phase TEXT NOT NULL,
                  current_step TEXT NOT NULL,
                  mode TEXT NOT NULL DEFAULT 'two_stage',
                  options_json TEXT NOT NULL DEFAULT '{}',
                  progress_json TEXT NOT NULL DEFAULT '{}',
                  context_json TEXT NOT NULL,
                  last_error TEXT NOT NULL DEFAULT '',
                  owner_token TEXT NOT NULL DEFAULT '',
                  lease_expires_at REAL NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(project_id, episode)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_autopilot_project_status "
                "ON episode_autopilot_runs(project_id, status)"
            )

        ensure_sqlite_schema(
            self.db_path,
            component=AUTOPILOT_SCHEMA_COMPONENT,
            version=AUTOPILOT_SCHEMA_VERSION,
            initialize=initialize,
        )

    def _connect(self) -> sqlite3.Connection:
        self._ensure_schema()
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        configure_sqlite_connection(conn, set_journal_mode=False)
        return conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> EpisodeAutopilotRun | None:
        if row is None:
            return None
        return EpisodeAutopilotRun(
            run_id=str(row["run_id"]),
            project_id=str(row["project_id"]),
            episode=int(row["episode"]),
            status=str(row["status"]),
            phase=str(row["phase"]),
            current_step=str(row["current_step"]),
            mode=str(row["mode"] or "two_stage"),
            options_json=str(row["options_json"] or "{}"),
            progress_json=str(row["progress_json"] or "{}"),
            context_json=str(row["context_json"]),
            last_error=str(row["last_error"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def upsert(self, run: EpisodeAutopilotRun) -> EpisodeAutopilotRun:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO episode_autopilot_runs(
                  run_id, project_id, episode, status, phase, current_step, mode,
                  options_json, progress_json, context_json, last_error,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, episode) DO UPDATE SET
                  run_id=excluded.run_id,
                  status=excluded.status,
                  phase=excluded.phase,
                  current_step=excluded.current_step,
                  mode=excluded.mode,
                  options_json=excluded.options_json,
                  progress_json=excluded.progress_json,
                  context_json=excluded.context_json,
                  last_error=excluded.last_error,
                  updated_at=excluded.updated_at,
                  owner_token=CASE
                    WHEN episode_autopilot_runs.run_id != excluded.run_id THEN ''
                    ELSE episode_autopilot_runs.owner_token
                  END,
                  lease_expires_at=CASE
                    WHEN episode_autopilot_runs.run_id != excluded.run_id THEN 0
                    ELSE episode_autopilot_runs.lease_expires_at
                  END
                """,
                (
                    run.run_id,
                    run.project_id,
                    run.episode,
                    run.status,
                    run.phase,
                    run.current_step,
                    run.mode,
                    run.options_json,
                    run.progress_json,
                    run.context_json,
                    run.last_error,
                    run.created_at,
                    run.updated_at,
                ),
            )
            conn.commit()
            return run
        finally:
            conn.close()

    def get(self, project_id: str, episode: int) -> EpisodeAutopilotRun | None:
        if not self.db_path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM episode_autopilot_runs WHERE project_id=? AND episode=?",
                (project_id, int(episode)),
            ).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def get_by_run_id(self, run_id: str) -> EpisodeAutopilotRun | None:
        if not self.db_path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM episode_autopilot_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def list_project(self, project_id: str) -> list[EpisodeAutopilotRun]:
        if not self.db_path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM episode_autopilot_runs WHERE project_id=? ORDER BY episode ASC",
                (project_id,),
            ).fetchall()
            return [run for row in rows if (run := self._row(row)) is not None]
        finally:
            conn.close()

    def active_runs(self) -> list[EpisodeAutopilotRun]:
        if not self.db_path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM episode_autopilot_runs "
                "WHERE status IN ('phase1_running', 'phase2_running') "
                "ORDER BY episode ASC"
            ).fetchall()
            return [run for row in rows if (run := self._row(row)) is not None]
        finally:
            conn.close()

    def update_fields(
        self,
        run_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        current_step: str | None = None,
        options: AutopilotOptions | None = None,
        progress: dict[str, Any] | None = None,
        last_error: str | None = None,
        only_if_active: bool = False,
    ) -> EpisodeAutopilotRun | None:
        conn = self._connect()
        try:
            sets: list[str] = ["updated_at=?"]
            params: list[Any] = [_utc_now()]
            if status is not None:
                sets.append("status=?")
                params.append(status)
            if phase is not None:
                sets.append("phase=?")
                params.append(phase)
            if current_step is not None:
                sets.append("current_step=?")
                params.append(current_step)
            if options is not None:
                sets.append("options_json=?")
                params.append(json.dumps(options.to_dict(), ensure_ascii=False))
                sets.append("mode=?")
                params.append(options.mode)
            if progress is not None:
                sets.append("progress_json=?")
                params.append(json.dumps(progress, ensure_ascii=False))
            if last_error is not None:
                sets.append("last_error=?")
                params.append(last_error)

            where = "WHERE run_id=?"
            params.append(run_id)
            if only_if_active:
                where += " AND status IN ('phase1_running', 'phase2_running')"

            conn.execute(
                f"UPDATE episode_autopilot_runs SET {', '.join(sets)} {where}",
                tuple(params),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM episode_autopilot_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def claim_lease(
        self,
        run_id: str,
        owner_token: str,
        *,
        lease_seconds: float = LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        current = _now_ts() if now is None else float(now)
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE episode_autopilot_runs
                SET owner_token=?, lease_expires_at=?
                WHERE run_id=?
                  AND status IN ('phase1_running', 'phase2_running')
                  AND (owner_token='' OR owner_token=? OR lease_expires_at<=?)
                """,
                (owner_token, current + lease_seconds, run_id, owner_token, current),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def renew_lease(
        self,
        run_id: str,
        owner_token: str,
        *,
        lease_seconds: float = LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        current = _now_ts() if now is None else float(now)
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE episode_autopilot_runs
                SET lease_expires_at=?
                WHERE run_id=?
                  AND status IN ('phase1_running', 'phase2_running')
                  AND owner_token=?
                  AND lease_expires_at>?
                """,
                (current + lease_seconds, run_id, owner_token, current),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def release_lease(self, run_id: str, owner_token: str) -> None:
        if not self.db_path.exists():
            return
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE episode_autopilot_runs
                SET owner_token='', lease_expires_at=0
                WHERE run_id=? AND owner_token=?
                """,
                (run_id, owner_token),
            )
            conn.commit()
        finally:
            conn.close()


class ActiveAutopilotProjectIndex:
    """Minimal pointer index solely for crash recovery on API startup.

    Never stores run progress or episode state — each project's own
    ``state_dir/autopilot_runs.db`` is the sole source of truth.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._explicit_path = db_path

    @property
    def db_path(self) -> Path:
        return self._explicit_path or (_state_root() / "autopilot_active_projects.db")

    def _connect(self) -> sqlite3.Connection:
        def initialize(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_autopilot_projects (
                  project_id TEXT PRIMARY KEY,
                  state_dir TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )

        ensure_sqlite_schema(
            self.db_path,
            component=INDEX_SCHEMA_COMPONENT,
            version=INDEX_SCHEMA_VERSION,
            initialize=initialize,
        )
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        configure_sqlite_connection(conn, set_journal_mode=False)
        return conn

    def register(self, project_id: str, state_dir: Path) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO active_autopilot_projects(project_id, state_dir, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                  state_dir=excluded.state_dir,
                  updated_at=excluded.updated_at
                """,
                (project_id, str(state_dir), _utc_now()),
            )
            conn.commit()
        finally:
            conn.close()

    def unregister(self, project_id: str) -> None:
        if not self.db_path.exists():
            return
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM active_autopilot_projects WHERE project_id=?",
                (project_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def list_projects(self) -> list[tuple[str, Path]]:
        if not self.db_path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT project_id, state_dir FROM active_autopilot_projects"
            ).fetchall()
            return [(str(r["project_id"]), Path(str(r["state_dir"]))) for r in rows]
        finally:
            conn.close()


# ── Tail-anchor frame extraction & binding ───────────────────────────────────


def _find_returned_last_frame(
    output_dir: Path,
    episode: int,
    beat_num: int,
    task_result: dict[str, Any] | None = None,
) -> Path | None:
    if isinstance(task_result, dict):
        reported = str(task_result.get("last_frame_path") or "").strip()
        if reported:
            candidate = Path(reported)
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
    base = (
        Path(output_dir)
        / "videos"
        / "beats"
        / f"ep{int(episode):03d}"
        / "returned_last_frames"
        / f"beat_{int(beat_num):02d}"
    )
    for suffix in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = base.with_suffix(suffix)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _extract_video_last_frame_ffmpeg(video_path: Path, dest_path: Path) -> Path | None:
    if not video_path.exists() or video_path.stat().st_size <= 0:
        return None
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(f".tmp_{dest_path.name}")
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-sseof",
                "-0.15",
                "-i",
                str(video_path),
                "-update",
                "1",
                "-frames:v",
                "1",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
            tmp_path.replace(dest_path)
            return dest_path
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffmpeg last-frame extraction failed for %s: %s", video_path, exc)
    finally:
        if tmp_path.exists():
            with suppress(OSError):
                tmp_path.unlink()
    return None


async def resolve_or_extract_beat_last_frame(
    *,
    output_dir: Path,
    episode: int,
    beat_num: int,
    task_result: dict[str, Any] | None = None,
) -> Path | None:
    """Resolve provider-returned last frame, falling back to ffmpeg extraction."""
    found = _find_returned_last_frame(output_dir, episode, beat_num, task_result)
    if found is not None:
        return found
    from novelvideo.utils.path_resolver import PathResolver

    video_path = PathResolver(str(output_dir), episode).video(beat_num)
    fallback_dest = (
        Path(output_dir)
        / "videos"
        / "beats"
        / f"ep{int(episode):03d}"
        / "returned_last_frames"
        / f"beat_{int(beat_num):02d}.png"
    )
    return await asyncio.to_thread(_extract_video_last_frame_ffmpeg, video_path, fallback_dest)


def _write_tail_frame_as_next_first_frame(source_path: Path, target_path: Path) -> Path:
    """Atomically write ``source_path`` into ``target_path`` (PNG)."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".tmp_{uuid.uuid4().hex}_{target_path.name}")
    try:
        if source_path.suffix.lower() == ".png":
            shutil.copy2(source_path, tmp_path)
        else:
            from PIL import Image

            with Image.open(source_path) as img:
                img.convert("RGB").save(tmp_path, format="PNG")
        tmp_path.replace(target_path)
        return target_path
    finally:
        if tmp_path.exists():
            with suppress(OSError):
                tmp_path.unlink()


async def bind_tail_frame_to_next_beat(
    *,
    store: Any,
    output_dir: Path,
    state_dir: Path,
    episode: int,
    next_beat: dict[str, Any],
    tail_frame_path: Path,
) -> Path:
    """Bind Beat N's tail frame as Beat N+1's opening anchor (@图片1)."""
    from novelvideo.seedance2_i2v.models import (
        default_seedance2_ratio_for_state_dir,
        dump_seedance2_config,
        parse_seedance2_config,
    )
    from novelvideo.utils.path_resolver import PathResolver

    next_beat_num = int(next_beat.get("beat_number") or 0)
    paths = PathResolver(str(output_dir), episode)
    dest_frame = paths.frame(next_beat_num)
    await asyncio.to_thread(_write_tail_frame_as_next_first_frame, tail_frame_path, dest_frame)

    default_ratio = default_seedance2_ratio_for_state_dir(Path(state_dir))
    cfg = parse_seedance2_config(
        next_beat.get("seedance2_config_json"),
        default_ratio=default_ratio,
    )
    cfg.s9_include_first_frame = True
    cfg.s9_tail_anchor = True
    updated_json = dump_seedance2_config(cfg)
    next_beat["seedance2_config_json"] = updated_json
    if hasattr(store, "update_beat_asset"):
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=next_beat_num,
            seedance2_config_json=updated_json,
        )
    return dest_frame


# ── Internal control exceptions ──────────────────────────────────────────────


class _RunInterrupted(Exception):
    """Raised inside a worker when the run was paused, cancelled, or lost its lease."""


class _RunPausedError(Exception):
    """Raised when the run should transition cleanly to ``paused`` with a user-visible reason."""

    def __init__(
        self,
        message: str,
        *,
        step: str | None = None,
        failed_items: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.step = step
        self.failed_items = list(failed_items or [])


# ── Coordinator ──────────────────────────────────────────────────────────────


class EpisodeAutopilotCoordinator:
    """Deterministic two-stage autopilot coordinator with strict project & episode isolation."""

    def __init__(
        self,
        *,
        index: ActiveAutopilotProjectIndex | None = None,
        poll_seconds: float | None = None,
        backpressure_seconds: float | None = None,
        retry_base_seconds: float | None = None,
    ) -> None:
        self._index = index or ActiveAutopilotProjectIndex()
        self._poll_seconds = poll_seconds
        self._backpressure_seconds = backpressure_seconds
        self._retry_base_seconds = retry_base_seconds
        self._workers: dict[tuple[str, int], asyncio.Task] = {}
        self._identity_locks: dict[str, asyncio.Lock] = {}
        self._owner_tokens: dict[str, str] = {}

    def _poll_interval(self) -> float:
        return POLL_SECONDS if self._poll_seconds is None else self._poll_seconds

    def _backpressure_interval(self) -> float:
        return (
            BACKPRESSURE_SECONDS
            if self._backpressure_seconds is None
            else self._backpressure_seconds
        )

    def _retry_delay(self, attempt: int) -> float:
        base = RETRY_BASE_SECONDS if self._retry_base_seconds is None else self._retry_base_seconds
        return min(RETRY_MAX_SECONDS, base * (2 ** max(0, attempt)))

    @staticmethod
    def _project_store(state_dir: Path) -> ProjectAutopilotStore:
        return ProjectAutopilotStore(Path(state_dir))

    def _identity_lock(self, project_id: str) -> asyncio.Lock:
        lock = self._identity_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._identity_locks[project_id] = lock
        return lock

    # ── Public API ───────────────────────────────────────────────────────────

    def get_run(self, ctx: ProjectContext, episode: int) -> EpisodeAutopilotRun | None:
        return self._project_store(ctx.state_dir).get(ctx.project_id, int(episode))

    def list_runs(self, ctx: ProjectContext) -> list[EpisodeAutopilotRun]:
        return self._project_store(ctx.state_dir).list_project(ctx.project_id)

    async def start_episode(
        self,
        ctx: ProjectContext,
        *,
        episode: int,
        options: AutopilotOptions | None = None,
        start_phase: str = "phase1",
    ) -> EpisodeAutopilotRun:
        episode_num = int(episode)
        if episode_num <= 0:
            raise ValueError("episode must be greater than 0")
        opts = options or AutopilotOptions()
        store = self._project_store(ctx.state_dir)

        existing = store.get(ctx.project_id, episode_num)
        if existing is not None and existing.status in ACTIVE_AUTOPILOT_STATUSES:
            # Already running: ensure worker is attached and return idempotently.
            self._ensure_worker(existing)
            return existing

        now = _utc_now()
        if start_phase == "phase2":
            status = "phase2_running"
            phase = "phase2"
            step = "script_generation"
        else:
            status = "phase1_running"
            phase = "phase1"
            step = "project_bootstrap"

        run = EpisodeAutopilotRun(
            run_id=uuid.uuid4().hex,
            project_id=ctx.project_id,
            episode=episode_num,
            status=status,
            phase=phase,
            current_step=step,
            mode=opts.mode,
            options_json=json.dumps(opts.to_dict(), ensure_ascii=False),
            progress_json=json.dumps(
                {
                    "message": str(
                        lmsg(
                            "episode.autopilot.progress.startingPhase1",
                            "正在启动两段式托管...",
                        )
                        if phase == "phase1"
                        else lmsg(
                            "episode.autopilot.progress.startingPhase2",
                            "正在启动成片制作...",
                        )
                    ),
                    "failed_items": [],
                    "active_tasks": [],
                },
                ensure_ascii=False,
            ),
            context_json=_serialize_context(ctx),
            last_error="",
            created_at=now,
            updated_at=now,
        )
        store.upsert(run)
        self._index.register(ctx.project_id, ctx.state_dir)
        self._ensure_worker(run)
        return run

    async def batch_start(
        self,
        ctx: ProjectContext,
        *,
        episodes: list[int],
        options: AutopilotOptions | None = None,
        start_phase: str = "phase1",
    ) -> list[EpisodeAutopilotRun]:
        seen: set[int] = set()
        ordered: list[int] = []
        for ep in episodes:
            num = int(ep)
            if num > 0 and num not in seen:
                seen.add(num)
                ordered.append(num)
        runs: list[EpisodeAutopilotRun] = []
        for ep_num in ordered:
            run = await self.start_episode(
                ctx,
                episode=ep_num,
                options=options,
                start_phase=start_phase,
            )
            runs.append(run)
        return runs

    async def continue_episode(
        self,
        ctx: ProjectContext,
        *,
        episode: int,
        overrides: dict[str, Any] | None = None,
    ) -> EpisodeAutopilotRun:
        episode_num = int(episode)
        store = self._project_store(ctx.state_dir)
        existing = store.get(ctx.project_id, episode_num)
        if existing is None:
            opts = AutopilotOptions.from_dict(overrides)
            return await self.start_episode(
                ctx,
                episode=episode_num,
                options=opts,
                start_phase="phase2",
            )

        if existing.status in ACTIVE_AUTOPILOT_STATUSES:
            self._ensure_worker(existing)
            return existing

        merged_opts = existing.options.with_overrides(overrides)
        progress = dict(existing.progress)
        progress["failed_items"] = []

        if existing.status == "awaiting_asset_review":
            next_status = "phase2_running"
            next_phase = "phase2"
            next_step = "script_generation"
            progress["message"] = "资产验收通过，开始生成脚本与成片..."
        elif existing.status in {"paused", "failed", "cancelled"}:
            if existing.phase == "phase2":
                next_status = "phase2_running"
                next_phase = "phase2"
                next_step = existing.current_step or "script_generation"
                progress["message"] = "正在恢复第二阶段（脚本/视频/合成）..."
            else:
                next_status = "phase1_running"
                next_phase = "phase1"
                next_step = "project_bootstrap"
                progress["message"] = "正在恢复第一阶段（资产规划与补图）..."
        elif existing.status == "completed":
            next_status = "phase2_running"
            next_phase = "phase2"
            next_step = "script_generation"
            progress["message"] = "正在重新检查并补齐第二阶段..."
        else:
            next_status = "phase1_running"
            next_phase = "phase1"
            next_step = "project_bootstrap"

        updated = store.update_fields(
            existing.run_id,
            status=next_status,
            phase=next_phase,
            current_step=next_step,
            options=merged_opts,
            progress=progress,
            last_error="",
        )
        assert updated is not None
        self._index.register(ctx.project_id, ctx.state_dir)
        self._ensure_worker(updated)
        return updated

    async def pause_episode(
        self,
        ctx: ProjectContext,
        *,
        episode: int,
        reason: str = "",
    ) -> EpisodeAutopilotRun | None:
        episode_num = int(episode)
        store = self._project_store(ctx.state_dir)
        existing = store.get(ctx.project_id, episode_num)
        if existing is None:
            return None
        if existing.status not in ACTIVE_AUTOPILOT_STATUSES:
            return existing

        progress = dict(existing.progress)
        progress["message"] = reason or "已手动暂停"
        updated = store.update_fields(
            existing.run_id,
            status="paused",
            progress=progress,
            last_error=reason,
        )
        self._stop_worker(ctx.project_id, episode_num)
        self._cleanup_project_index_if_idle(ctx.project_id, ctx.state_dir)
        return updated

    async def cancel_episode(
        self,
        ctx: ProjectContext,
        *,
        episode: int,
        cancel_episode_tasks: bool = True,
    ) -> EpisodeAutopilotRun | None:
        """Cancel ONLY this episode's autopilot and strictly episode-owned tasks.

        Shared project-level tasks (``task.episode == 0``) and tasks belonging to
        other episodes (``task.episode != episode``) are NEVER cancelled.
        """
        episode_num = int(episode)
        store = self._project_store(ctx.state_dir)
        existing = store.get(ctx.project_id, episode_num)
        if existing is None:
            return None

        progress = dict(existing.progress)
        progress["message"] = "已取消本集托管"
        progress["active_tasks"] = []
        updated = store.update_fields(
            existing.run_id,
            status="cancelled",
            progress=progress,
            last_error="",
        )
        self._stop_worker(ctx.project_id, episode_num)

        if cancel_episode_tasks:
            from novelvideo.task_backend.client import cancel_project_task

            mgr = get_task_manager()
            tasks = await asyncio.to_thread(mgr.list_tasks_for_project, ctx)
            for task in tasks:
                if (
                    int(task.episode or 0) == episode_num
                    and task.status in ACTIVE_PROJECT_TASK_STATUSES
                ):
                    with suppress(Exception):
                        await cancel_project_task(ctx, task)

        self._cleanup_project_index_if_idle(ctx.project_id, ctx.state_dir)
        return updated

    async def resume_active_runs(self) -> int:
        """Resume any active autopilot runs across registered projects after API restart."""
        resumed = 0
        for project_id, state_dir in self._index.list_projects():
            if not state_dir.exists():
                self._index.unregister(project_id)
                continue
            store = self._project_store(state_dir)
            active = store.active_runs()
            if not active:
                self._index.unregister(project_id)
                continue
            for run in active:
                self._ensure_worker(run)
                resumed += 1
        return resumed

    async def shutdown(self) -> None:
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    # ── Worker lifecycle ─────────────────────────────────────────────────────

    def _cleanup_project_index_if_idle(self, project_id: str, state_dir: Path) -> None:
        store = self._project_store(state_dir)
        if not store.active_runs():
            self._index.unregister(project_id)

    def _ensure_worker(self, run: EpisodeAutopilotRun) -> None:
        key = (run.project_id, run.episode)
        existing = self._workers.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_worker(run.project_id, run.episode, run.run_id, run.context.state_dir),
            name=f"episode-autopilot:{run.project_id}:ep{run.episode}",
        )
        self._workers[key] = task

        def _cleanup(done: asyncio.Task) -> None:
            if self._workers.get(key) is done:
                self._workers.pop(key, None)

        task.add_done_callback(_cleanup)

    def _stop_worker(self, project_id: str, episode: int) -> None:
        worker = self._workers.pop((project_id, int(episode)), None)
        if worker is not None and not worker.done():
            worker.cancel()

    async def _run_worker(
        self,
        project_id: str,
        episode: int,
        run_id: str,
        state_dir: Path,
    ) -> None:
        store = self._project_store(state_dir)
        owner_token = uuid.uuid4().hex
        if not store.claim_lease(run_id, owner_token):
            return
        self._owner_tokens[run_id] = owner_token
        try:
            run = store.get_by_run_id(run_id)
            if run is None or run.status not in ACTIVE_AUTOPILOT_STATUSES:
                return

            if run.status == "phase1_running":
                await self._run_phase1(store, run, owner_token)
                run = store.get_by_run_id(run_id)
                if run is None or run.status != "phase2_running":
                    return

            if run.status == "phase2_running":
                await self._run_phase2(store, run, owner_token)
        except (_RunInterrupted, asyncio.CancelledError):
            logger.info(
                "autopilot worker interrupted: project=%s ep=%s run=%s",
                project_id,
                episode,
                run_id,
            )
        except _RunPausedError as exc:
            current = store.get_by_run_id(run_id)
            if current is not None and current.status in ACTIVE_AUTOPILOT_STATUSES:
                progress = dict(current.progress)
                progress["message"] = exc.message
                if exc.failed_items:
                    progress["failed_items"] = exc.failed_items
                progress["active_tasks"] = []
                store.update_fields(
                    run_id,
                    status="paused",
                    current_step=exc.step or current.current_step,
                    progress=progress,
                    last_error=exc.message,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "autopilot worker failed: project=%s ep=%s run=%s",
                project_id,
                episode,
                run_id,
            )
            current = store.get_by_run_id(run_id)
            if current is not None and current.status in ACTIVE_AUTOPILOT_STATUSES:
                progress = dict(current.progress)
                progress["message"] = f"托管执行异常：{exc}"
                progress["active_tasks"] = []
                store.update_fields(
                    run_id,
                    status="paused",
                    progress=progress,
                    last_error=str(exc),
                )
        finally:
            self._owner_tokens.pop(run_id, None)
            store.release_lease(run_id, owner_token)
            self._cleanup_project_index_if_idle(project_id, state_dir)

    def _assert_active(
        self,
        store: ProjectAutopilotStore,
        run_id: str,
        owner_token: str,
    ) -> EpisodeAutopilotRun:
        run = store.get_by_run_id(run_id)
        if run is None or run.status not in ACTIVE_AUTOPILOT_STATUSES:
            raise _RunInterrupted()
        if not store.renew_lease(run_id, owner_token):
            raise _RunInterrupted()
        return run

    def _update_progress(
        self,
        store: ProjectAutopilotStore,
        run_id: str,
        owner_token: str,
        *,
        step: str | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> EpisodeAutopilotRun:
        run = self._assert_active(store, run_id, owner_token)
        progress = dict(run.progress)
        if message is not None:
            progress["message"] = message
        if extra:
            progress.update(extra)
        updated = store.update_fields(
            run_id,
            current_step=step or run.current_step,
            progress=progress,
            only_if_active=True,
        )
        if updated is None or updated.status not in ACTIVE_AUTOPILOT_STATUSES:
            raise _RunInterrupted()
        return updated

    # ── Task enqueue (with backpressure) & wait helpers ──────────────────────

    async def _enqueue_with_backpressure(
        self,
        store: ProjectAutopilotStore,
        run_id: str,
        owner_token: str,
        enqueue_fn: Callable[[], Awaitable[Any]],
        *,
        waiting_message: str,
    ) -> Any:
        notified = False
        while True:
            self._assert_active(store, run_id, owner_token)
            try:
                return await enqueue_fn()
            except _LANE_LIMIT_EXCEPTIONS:
                if not notified:
                    self._update_progress(
                        store,
                        run_id,
                        owner_token,
                        message=waiting_message,
                    )
                    notified = True
                await asyncio.sleep(self._backpressure_interval())

    async def _await_task(
        self,
        store: ProjectAutopilotStore,
        run_id: str,
        owner_token: str,
        ctx: ProjectContext,
        *,
        task_type: str,
        episode: int,
        beat_num: int | None = None,
        scope: str | None = None,
        expected_task_id: str | None = None,
    ) -> TaskState:
        mgr = get_task_manager()
        while True:
            self._assert_active(store, run_id, owner_token)
            state = await asyncio.to_thread(
                mgr.get_task_for_project,
                ctx,
                task_type,
                episode,
                beat_num=beat_num,
                scope=scope,
            )
            if state is None:
                await asyncio.sleep(self._poll_interval())
                continue
            if (
                expected_task_id
                and state.task_id != expected_task_id
                and state.status in ACTIVE_PROJECT_TASK_STATUSES
            ):
                # Another run superseded this canonical slot; keep watching it.
                expected_task_id = state.task_id
            if state.status in TERMINAL_TASK_STATUSES:
                return state
            await asyncio.sleep(self._poll_interval())

    async def _run_task_with_retry(
        self,
        store: ProjectAutopilotStore,
        run_id: str,
        owner_token: str,
        ctx: ProjectContext,
        *,
        task_type: str,
        queue_kind: str,
        episode: int,
        beat_num: int | None = None,
        scope: str | None = None,
        payload: dict[str, Any] | None = None,
        label: str,
        max_retries: int,
    ) -> TaskState:
        from novelvideo.ports import get_task_backend

        backend = get_task_backend()
        attempt = 0
        while True:
            self._assert_active(store, run_id, owner_token)
            queued = await self._enqueue_with_backpressure(
                store,
                run_id,
                owner_token,
                lambda: backend.enqueue_project_task(
                    ctx,
                    product_surface="mainline",
                    task_type=task_type,
                    queue_kind=queue_kind,
                    episode=episode,
                    beat_num=beat_num,
                    scope=scope,
                    payload=payload or {},
                ),
                waiting_message=f"{label} 等待队列空位...",
            )
            terminal = await self._await_task(
                store,
                run_id,
                owner_token,
                ctx,
                task_type=task_type,
                episode=episode,
                beat_num=beat_num,
                scope=scope,
                expected_task_id=queued.task_state.task_id,
            )
            if terminal.status == "completed":
                return terminal
            if terminal.status == "cancelled":
                raise _RunPausedError(f"{label} 已被取消")

            err = str(terminal.error or f"{label} 执行失败")
            _cat, retryable, _fatal = classify_autopilot_error(err)
            if retryable and attempt < max_retries:
                delay = self._retry_delay(attempt)
                attempt += 1
                self._update_progress(
                    store,
                    run_id,
                    owner_token,
                    message=f"{label} 遇到瞬时错误，{int(delay)}秒后第 {attempt}/{max_retries} 次重试...",
                )
                await asyncio.sleep(delay)
                continue
            return terminal

    # ── SQLite store helper ──────────────────────────────────────────────────

    @staticmethod
    async def _open_project_sqlite(ctx: ProjectContext):
        from novelvideo.api.deps import make_sqlite_store_for_context

        return await make_sqlite_store_for_context(ctx)

    # ── Phase 1: Cold Start -> Asset Planning -> Missing Reference Images ────

    async def _run_phase1(
        self,
        store: ProjectAutopilotStore,
        run: EpisodeAutopilotRun,
        owner_token: str,
    ) -> None:
        opts = run.options
        episode_num = run.episode

        # Step 1.1: Project Cold-Start Prerequisites
        self._update_progress(
            store,
            run.run_id,
            owner_token,
            step="project_bootstrap",
            message="检查项目冷启动前置（小说导入、角色/场景/分集目录）...",
        )
        await self._ensure_project_bootstrap(store, run, owner_token)

        # Step 1.2: Episode Asset Planning (identity + scene + prop)
        self._update_progress(
            store,
            run.run_id,
            owner_token,
            step="asset_planning",
            message=f"第 {episode_num} 集资产规划中（身份 / 场景 / 道具）...",
        )
        await self._ensure_episode_asset_plans(store, run, owner_token)

        # Step 1.3: Missing Reference Images (Portraits -> Identities/Scenes/Props)
        self._update_progress(
            store,
            run.run_id,
            owner_token,
            step="asset_images",
            message=f"检查并补齐第 {episode_num} 集缺失参考图...",
        )
        asset_summary, failed_assets = await self._ensure_episode_asset_images(
            store, run, owner_token
        )

        # Step 1.4: Two-stage review checkpoint (or proceed in full_auto mode)
        self._assert_active(store, run.run_id, owner_token)
        if opts.mode == "two_stage":
            progress = dict(
                (store.get_by_run_id(run.run_id) or run).progress
            )
            progress["asset_summary"] = asset_summary
            progress["failed_items"] = failed_assets
            progress["active_tasks"] = []
            if failed_assets:
                progress["message"] = (
                    f"第 {episode_num} 集资产阶段完成（有 {len(failed_assets)} 项参考图待处理），"
                    "请检查资产后点击「继续到成片」"
                )
            else:
                progress["message"] = (
                    f"第 {episode_num} 集资产规划与参考图已就绪，请验收角色/场景/道具后点击「继续到成片」"
                )
            store.update_fields(
                run.run_id,
                status="awaiting_asset_review",
                phase="review",
                current_step="asset_review",
                progress=progress,
                last_error=failed_assets[0]["error"] if failed_assets else "",
                only_if_active=True,
            )
            return

        if failed_assets:
            raise _RunPausedError(
                f"第 {episode_num} 集有 {len(failed_assets)} 项参考图生成失败，已暂停等待处理",
                step="asset_images",
                failed_items=failed_assets,
            )

        progress = dict((store.get_by_run_id(run.run_id) or run).progress)
        progress["asset_summary"] = asset_summary
        progress["failed_items"] = []
        progress["message"] = "资产就绪，自动进入第二阶段（脚本与成片）..."
        store.update_fields(
            run.run_id,
            status="phase2_running",
            phase="phase2",
            current_step="script_generation",
            progress=progress,
            only_if_active=True,
        )

    async def _ensure_project_bootstrap(
        self,
        store: ProjectAutopilotStore,
        run: EpisodeAutopilotRun,
        owner_token: str,
    ) -> None:
        from novelvideo.knowledge_pipeline import is_structured_pipeline
        from novelvideo.novel_source import has_imported_novel

        ctx = run.context
        opts = run.options
        mgr = get_task_manager()

        # If ingest_fast is already in progress, await it first.
        ingest_task = await asyncio.to_thread(
            mgr.get_task_for_project, ctx, "ingest_fast", 0
        )
        if ingest_task is not None and ingest_task.status in ACTIVE_PROJECT_TASK_STATUSES:
            self._update_progress(
                store,
                run.run_id,
                owner_token,
                step="project_bootstrap",
                message="等待小说导入任务完成...",
            )
            done = await self._await_task(
                store,
                run.run_id,
                owner_token,
                ctx,
                task_type="ingest_fast",
                episode=0,
                expected_task_id=ingest_task.task_id,
            )
            if done.status != "completed":
                raise _RunPausedError(
                    f"小说导入失败：{done.error or done.status}",
                    step="project_bootstrap",
                )

        sql = await self._open_project_sqlite(ctx)
        try:
            characters = list(sql.get_all_characters() or [])
            episodes = list(sql.get_all_episodes() or [])
            scenes = list(sql.get_all_scenes() or []) if hasattr(sql, "get_all_scenes") else []
        finally:
            await sql.close()

        novel_imported = has_imported_novel(ctx.output_dir)
        if not novel_imported and (not characters or not episodes):
            raise _RunPausedError(
                "项目尚未导入小说或剧本正文，请先在导入页上传并完成导入",
                step="project_bootstrap",
            )

        # Wait for any active build_characters / build_scenes / build_episodes,
        # and start missing project-level catalog builds in parallel (all episode=0 deduped!).
        bootstrap_coros: list[Awaitable[tuple[str, TaskState]]] = []

        async def _ensure_catalog(
            task_type: str,
            label: str,
            payload: dict[str, Any] | None = None,
            *,
            need_start: bool,
        ) -> tuple[str, TaskState | None]:
            active = await asyncio.to_thread(
                mgr.get_task_for_project, ctx, task_type, 0
            )
            if active is not None and active.status in ACTIVE_PROJECT_TASK_STATUSES:
                done = await self._await_task(
                    store,
                    run.run_id,
                    owner_token,
                    ctx,
                    task_type=task_type,
                    episode=0,
                    expected_task_id=active.task_id,
                )
                return label, done
            if not need_start:
                return label, None
            done = await self._run_task_with_retry(
                store,
                run.run_id,
                owner_token,
                ctx,
                task_type=task_type,
                queue_kind="default",
                episode=0,
                payload=payload or {},
                label=label,
                max_retries=opts.max_retries,
            )
            return label, done

        planning_mode = "chapters" if is_structured_pipeline(ctx.state_dir) else "ai"
        bootstrap_coros.append(
            _ensure_catalog(
                "build_characters",
                "角色提取",
                need_start=(not characters and novel_imported),
            )
        )
        bootstrap_coros.append(
            _ensure_catalog(
                "build_episodes",
                "分集规划",
                payload={"config": {"planning_mode": planning_mode}},
                need_start=(not episodes and novel_imported),
            )
        )
        bootstrap_coros.append(
            _ensure_catalog(
                "build_scenes",
                "场景提取",
                need_start=(not scenes and novel_imported),
            )
        )

        results = await asyncio.gather(*bootstrap_coros)
        for label, terminal in results:
            if terminal is not None and terminal.status != "completed":
                raise _RunPausedError(
                    f"{label}失败：{terminal.error or terminal.status}",
                    step="project_bootstrap",
                )

        # Verify target episode exists now.
        sql = await self._open_project_sqlite(ctx)
        try:
            ep_obj = sql.get_episode(run.episode)
            if ep_obj is None:
                raise _RunPausedError(
                    f"第 {run.episode} 集不存在，请检查分集规划结果",
                    step="project_bootstrap",
                )
        finally:
            await sql.close()

    async def _ensure_episode_asset_plans(
        self,
        store: ProjectAutopilotStore,
        run: EpisodeAutopilotRun,
        owner_token: str,
    ) -> None:
        ctx = run.context
        opts = run.options
        ep_num = run.episode

        sql = await self._open_project_sqlite(ctx)
        try:
            ep_obj = sql.get_episode(ep_num)
            if ep_obj is None:
                raise _RunPausedError(f"第 {ep_num} 集不存在", step="asset_planning")
            has_identities = bool(getattr(ep_obj, "identity_ids", None))
            has_scenes = bool(getattr(ep_obj, "scene_menu", None))
            has_props = bool(getattr(ep_obj, "prop_menu", None))
        finally:
            await sql.close()

        async def _plan_identities() -> tuple[str, TaskState | None]:
            if has_identities:
                return "身份规划", None
            # Serialize ONLY identity_planner per project_id so concurrent episodes
            # never clobber characters.identities and reuse newly created identities.
            async with self._identity_lock(ctx.project_id):
                self._assert_active(store, run.run_id, owner_token)
                # Also wait if an external HTTP call already started identity_planner
                # for another episode in the same project.
                mgr = get_task_manager()
                while True:
                    self._assert_active(store, run.run_id, owner_token)
                    tasks = await asyncio.to_thread(mgr.list_tasks_for_project, ctx)
                    other_active = next(
                        (
                            t
                            for t in tasks
                            if t.task_type == "identity_planner"
                            and int(t.episode or 0) != ep_num
                            and t.status in ACTIVE_PROJECT_TASK_STATUSES
                        ),
                        None,
                    )
                    if other_active is None:
                        break
                    await asyncio.sleep(self._poll_interval())

                terminal = await self._run_task_with_retry(
                    store,
                    run.run_id,
                    owner_token,
                    ctx,
                    task_type="identity_planner",
                    queue_kind="default",
                    episode=ep_num,
                    payload={"episode": ep_num},
                    label=f"第 {ep_num} 集身份规划",
                    max_retries=opts.max_retries,
                )
                return "身份规划", terminal

        async def _plan_scenes() -> tuple[str, TaskState | None]:
            if has_scenes:
                return "场景规划", None
            terminal = await self._run_task_with_retry(
                store,
                run.run_id,
                owner_token,
                ctx,
                task_type="episode_scene_planner",
                queue_kind="default",
                episode=ep_num,
                scope=f"scene_run_ep{ep_num:03d}",
                payload={"episode": ep_num, "asset_kind": "scene"},
                label=f"第 {ep_num} 集场景规划",
                max_retries=opts.max_retries,
            )
            return "场景规划", terminal

        async def _plan_props() -> tuple[str, TaskState | None]:
            if has_props:
                return "道具规划", None
            terminal = await self._run_task_with_retry(
                store,
                run.run_id,
                owner_token,
                ctx,
                task_type="episode_prop_planner",
                queue_kind="default",
                episode=ep_num,
                scope=f"prop_run_ep{ep_num:03d}",
                payload={"episode": ep_num, "asset_kind": "prop"},
                label=f"第 {ep_num} 集道具规划",
                max_retries=opts.max_retries,
            )
            return "道具规划", terminal

        results = await asyncio.gather(_plan_identities(), _plan_scenes(), _plan_props())
        failures: list[dict[str, Any]] = []
        for label, terminal in results:
            if terminal is not None and terminal.status != "completed":
                err = str(terminal.error or f"{label}失败")
                cat, retryable, _fatal = classify_autopilot_error(err)
                failures.append(
                    {
                        "kind": "asset_plan",
                        "label": label,
                        "error": err,
                        "category": cat,
                        "retryable": retryable,
                    }
                )
        if failures:
            raise _RunPausedError(
                f"第 {ep_num} 集资产规划失败：{failures[0]['label']} - {failures[0]['error']}",
                step="asset_planning",
                failed_items=failures,
            )

    async def _ensure_episode_asset_images(
        self,
        store: ProjectAutopilotStore,
        run: EpisodeAutopilotRun,
        owner_token: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from novelvideo.api.routes.characters import (
            _character_image_billing_metadata,
            _resolve_character_image_model,
        )
        from novelvideo.api.routes.model_credits import _fixed_image_billing_params
        from novelvideo.api.routes.scenes import _scene_reference_billing_metadata
        from novelvideo.generators.nanobanana_prop import (
            _prop_reference_image_source,
            resolve_prop_reference_image_model,
        )
        from novelvideo.project_config import load_project_config_from_state_dir
        from novelvideo.task_scopes import (
            prop_reference_asset_scope,
            scene_reference_asset_scope,
        )
        from novelvideo.utils.path_resolver import (
            canonical_prop_reference_path,
            compute_identity_path,
            compute_portrait_path,
            compute_scene_master_path,
        )

        ctx = run.context
        opts = run.options
        ep_num = run.episode
        output_dir = Path(ctx.output_dir)
        project_config = load_project_config_from_state_dir(
            ctx.state_dir,
            username=ctx.owner_username,
            project=ctx.project_name,
        )
        visual_style = str(
            project_config.get("visual_style") or "chinese_period_drama"
        )

        sql = await self._open_project_sqlite(ctx)
        try:
            ep_obj = sql.get_episode(ep_num)
            if ep_obj is None:
                raise _RunPausedError(f"第 {ep_num} 集不存在", step="asset_images")
            characters = list(sql.get_all_characters() or [])
            scenes = list(sql.get_all_scenes() or []) if hasattr(sql, "get_all_scenes") else []
            props = list(sql.get_all_props() or []) if hasattr(sql, "get_all_props") else []
        finally:
            await sql.close()

        episode_identity_ids = set(getattr(ep_obj, "identity_ids", []) or [])
        scene_menu = list(getattr(ep_obj, "scene_menu", []) or [])
        prop_menu = list(getattr(ep_obj, "prop_menu", []) or [])

        # Collect episode's required characters & identities
        required_identities: list[tuple[Any, Any]] = []
        required_characters: dict[str, Any] = {}
        for char in characters:
            for ident in getattr(char, "identities", []) or []:
                ident_id = str(getattr(ident, "identity_id", "") or "")
                if ident_id and ident_id in episode_identity_ids:
                    required_identities.append((char, ident))
                    required_characters[char.name] = char

        # Collect episode's required scenes
        scene_by_name = {s.name: s for s in scenes if getattr(s, "name", None)}
        required_scenes: dict[str, Any] = {}
        for item in scene_menu:
            sid = str(
                (item.get("scene_id") if isinstance(item, dict) else getattr(item, "scene_id", ""))
                or ""
            ).strip()
            vid = str(
                (item.get("variant_id") if isinstance(item, dict) else getattr(item, "variant_id", ""))
                or ""
            ).strip()
            candidates = [f"{sid}_{vid}"] if (sid and vid) else []
            if sid:
                candidates.append(sid)
            for cand in candidates:
                if cand in scene_by_name:
                    required_scenes[cand] = scene_by_name[cand]
                    break

        # Collect episode's required global props
        prop_by_name = {p.name: p for p in props if getattr(p, "name", None)}
        required_props: dict[str, Any] = {}
        for item in prop_menu:
            pid = str(
                (
                    (item.get("prop_id") or item.get("name"))
                    if isinstance(item, dict)
                    else (getattr(item, "prop_id", "") or getattr(item, "name", ""))
                )
                or ""
            ).strip()
            if pid and pid in prop_by_name:
                required_props[pid] = prop_by_name[pid]

        failed_items: list[dict[str, Any]] = []
        fatal_error: str | None = None
        sem = asyncio.Semaphore(opts.max_concurrent_assets)

        async def _run_bounded_asset(
            *,
            kind: str,
            name: str,
            task_type: str,
            scope: str,
            payload: dict[str, Any],
        ) -> None:
            nonlocal fatal_error
            if fatal_error is not None:
                return
            async with sem:
                if fatal_error is not None:
                    return
                terminal = await self._run_task_with_retry(
                    store,
                    run.run_id,
                    owner_token,
                    ctx,
                    task_type=task_type,
                    queue_kind="default",
                    episode=0,
                    scope=scope,
                    payload=payload,
                    label=f"{kind}「{name}」",
                    max_retries=opts.max_retries,
                )
                if terminal.status != "completed":
                    err = str(terminal.error or f"{kind}「{name}」生成失败")
                    cat, retryable, is_fatal = classify_autopilot_error(err)
                    failed_items.append(
                        {
                            "kind": kind,
                            "name": name,
                            "error": err,
                            "category": cat,
                            "retryable": retryable,
                        }
                    )
                    if is_fatal:
                        fatal_error = err

        # Step 1.3a: Missing Character Portraits first (identity images depend on portrait.png)
        char_model = _resolve_character_image_model(
            ctx.owner_username, ctx.project_name, None
        )
        portrait_billing = _character_image_billing_metadata(char_model)
        missing_portraits = [
            char
            for char in required_characters.values()
            if not compute_portrait_path(output_dir, char.name)
            and str(getattr(char, "face_prompt", "") or "").strip()
        ]
        if missing_portraits:
            self._update_progress(
                store,
                run.run_id,
                owner_token,
                step="asset_images",
                message=f"正在并发补齐 {len(missing_portraits)} 张角色肖像...",
            )
            await asyncio.gather(
                *(
                    _run_bounded_asset(
                        kind="character_portrait",
                        name=char.name,
                        task_type="character_portrait",
                        scope=f"character:{char.name}:portrait",
                        payload={
                            "mode": "portrait",
                            "task_type": "character_portrait",
                            "character_name": char.name,
                            "style": visual_style,
                            "model": char_model,
                            "scope": f"character:{char.name}:portrait",
                            "output_dir": str(output_dir),
                            "billing": portrait_billing,
                        },
                    )
                    for char in missing_portraits
                )
            )
            if fatal_error is not None:
                raise _RunPausedError(
                    f"参考图生成因账号/配额错误中止：{fatal_error}",
                    step="asset_images",
                    failed_items=failed_items,
                )

        # Step 1.3b: Missing Identity Images + Scene Masters + Prop 3-View References in parallel
        identity_billing = _character_image_billing_metadata(
            char_model, image_role="identity"
        )
        scene_billing = _scene_reference_billing_metadata("")
        _, selected_prop_model = _prop_reference_image_source("")
        prop_pricing_model = selected_prop_model or resolve_prop_reference_image_model()
        prop_billing = {
            "pricing_kind": "image",
            "pricing_model": prop_pricing_model,
            "pricing_params": _fixed_image_billing_params(
                "prop_reference", model=prop_pricing_model
            ),
        }

        image_coros: list[Awaitable[None]] = []
        for char, ident in required_identities:
            ident_name = str(getattr(ident, "identity_name", "") or ident.identity_id)
            if compute_identity_path(output_dir, char.name, ident_name):
                continue
            if not compute_portrait_path(output_dir, char.name):
                failed_items.append(
                    {
                        "kind": "identity_image",
                        "name": f"{char.name} · {ident_name}",
                        "error": f"角色「{char.name}」缺少 Portrait（面部特写），无法生成身份图",
                        "category": "artifact_missing",
                        "retryable": False,
                    }
                )
                continue
            scope = f"character:{char.name}:identity:{ident_name}"
            image_coros.append(
                _run_bounded_asset(
                    kind="identity_image",
                    name=f"{char.name} · {ident_name}",
                    task_type="identity_image",
                    scope=scope,
                    payload={
                        "mode": "identity_image",
                        "task_type": "identity_image",
                        "character_name": char.name,
                        "identity_id": ident.identity_id,
                        "identity_name": ident_name,
                        "style": visual_style,
                        "model": char_model,
                        "scope": scope,
                        "output_dir": str(output_dir),
                        "billing": identity_billing,
                    },
                )
            )

        for scene_name in required_scenes:
            if compute_scene_master_path(output_dir, scene_name):
                continue
            scope = scene_reference_asset_scope(scene_name, "master")
            image_coros.append(
                _run_bounded_asset(
                    kind="scene_master",
                    name=scene_name,
                    task_type="scene_reference_asset",
                    scope=scope,
                    payload={
                        "scene_name": scene_name,
                        "kind": "master",
                        "model": "",
                        "style": visual_style,
                        "output_dir": str(output_dir),
                        "billing": scene_billing,
                    },
                )
            )

        for prop_name in required_props:
            if canonical_prop_reference_path(output_dir, prop_name).exists():
                continue
            scope = prop_reference_asset_scope(prop_name)
            image_coros.append(
                _run_bounded_asset(
                    kind="prop_reference",
                    name=prop_name,
                    task_type="prop_reference_asset",
                    scope=scope,
                    payload={
                        "prop_name": prop_name,
                        "style": visual_style,
                        "model": "",
                        "output_dir": str(output_dir),
                        "billing": prop_billing,
                    },
                )
            )

        if image_coros:
            self._update_progress(
                store,
                run.run_id,
                owner_token,
                step="asset_images",
                message=f"正在并发补齐 {len(image_coros)} 张身份/场景/道具参考图...",
            )
            await asyncio.gather(*image_coros)
            if fatal_error is not None:
                raise _RunPausedError(
                    f"参考图生成因账号/配额错误中止：{fatal_error}",
                    step="asset_images",
                    failed_items=failed_items,
                )

        # Compute live asset readiness summary for human review checkpoint
        portraits_ready = sum(
            1
            for char in required_characters.values()
            if compute_portrait_path(output_dir, char.name)
        )
        identities_ready = sum(
            1
            for char, ident in required_identities
            if compute_identity_path(
                output_dir,
                char.name,
                str(getattr(ident, "identity_name", "") or ident.identity_id),
            )
        )
        scenes_ready = sum(
            1
            for scene_name in required_scenes
            if compute_scene_master_path(output_dir, scene_name)
        )
        props_ready = sum(
            1
            for prop_name in required_props
            if canonical_prop_reference_path(output_dir, prop_name).exists()
        )
        summary = {
            "portraits": {"ready": portraits_ready, "total": len(required_characters)},
            "identities": {"ready": identities_ready, "total": len(required_identities)},
            "scenes": {"ready": scenes_ready, "total": len(required_scenes)},
            "props": {"ready": props_ready, "total": len(required_props)},
            "failed_count": len(failed_items),
        }
        return summary, failed_items

    # ── Phase 2: S9 Script -> Prompts -> Batch/Chain Videos -> Compose ───────

    async def _run_phase2(
        self,
        store: ProjectAutopilotStore,
        run: EpisodeAutopilotRun,
        owner_token: str,
    ) -> None:
        from novelvideo.api.routes.generation import (
            _is_custom_prompt_panel_backend,
            _is_grok_video_backend,
            _is_happyhorse_backend,
            _is_seedance2_backend,
            enqueue_single_beat_video_for_context,
        )
        from novelvideo.api.schemas import SingleVideoRequest
        from novelvideo.project_config import (
            default_aspect_ratio_for_spine_template,
            load_project_config_from_state_dir,
        )
        from novelvideo.seedance2_i2v.models import parse_seedance2_config
        from novelvideo.utils.path_resolver import PathResolver

        ctx = run.context
        opts = run.options
        ep_num = run.episode
        output_dir = Path(ctx.output_dir)

        project_config = load_project_config_from_state_dir(
            ctx.state_dir,
            username=ctx.owner_username,
            project=ctx.project_name,
        )
        video_backend = (
            str(opts.video_backend or "").strip()
            or str(project_config.get("video_backend") or "").strip()
            or "newapi_seedance-1.0-pro-fast"
        )
        spine_template = str(project_config.get("spine_template") or "drama")
        aspect_ratio = (
            str(opts.ratio or "").strip()
            or str(project_config.get("aspect_ratio") or "").strip()
            or default_aspect_ratio_for_spine_template(spine_template)
        )

        # Step 2.1: S9 Script Generation
        sql = await self._open_project_sqlite(ctx)
        try:
            beats = sorted(
                await sql.get_beats_as_dicts(ep_num),
                key=lambda b: int(b.get("beat_number") or 0),
            )
            ep_obj = sql.get_episode(ep_num)
            has_identity_plan = bool(getattr(ep_obj, "identity_ids", None))
        finally:
            await sql.close()

        has_script = bool(beats) and all(
            bool(
                str(b.get("narration_segment") or "").strip()
                or str(b.get("narration") or "").strip()
                or str(b.get("visual_description") or "").strip()
            )
            for b in beats
        )

        if not has_script:
            if not has_identity_plan:
                raise _RunPausedError(
                    f"第 {ep_num} 集尚未规划角色身份，请先完成第一阶段资产规划",
                    step="script_generation",
                )
            self._update_progress(
                store,
                run.run_id,
                owner_token,
                step="script_generation",
                message=f"正在生成第 {ep_num} 集 S9 分镜脚本...",
            )
            terminal = await self._run_task_with_retry(
                store,
                run.run_id,
                owner_token,
                ctx,
                task_type="script_writer",
                queue_kind="default",
                episode=ep_num,
                payload={
                    "episode": ep_num,
                    "config": {
                        "generation_mode": "s9",
                        "max_segment_seconds": opts.max_segment_seconds,
                        "tail_anchor": opts.tail_anchor,
                    },
                    "output_dir": str(output_dir),
                },
                label=f"第 {ep_num} 集脚本生成",
                max_retries=opts.max_retries,
            )
            if terminal.status != "completed":
                raise _RunPausedError(
                    f"第 {ep_num} 集脚本生成失败：{terminal.error or terminal.status}",
                    step="script_generation",
                )
            sql = await self._open_project_sqlite(ctx)
            try:
                beats = sorted(
                    await sql.get_beats_as_dicts(ep_num),
                    key=lambda b: int(b.get("beat_number") or 0),
                )
            finally:
                await sql.close()

        if not beats:
            raise _RunPausedError(
                f"第 {ep_num} 集脚本未产出任何分镜",
                step="script_generation",
            )

        # Step 2.2: Prompt Batch Generation (for Seedance2 / custom prompt panel backends)
        needs_prompt_panel = (
            _is_seedance2_backend(video_backend)
            or _is_happyhorse_backend(video_backend)
            or _is_grok_video_backend(video_backend)
            or _is_custom_prompt_panel_backend(video_backend)
        )
        if needs_prompt_panel:
            missing_prompt_beats = [
                int(b.get("beat_number") or 0)
                for b in beats
                if int(b.get("beat_number") or 0) > 0
                and not str(
                    parse_seedance2_config(b.get("seedance2_config_json")).final_prompt or ""
                ).strip()
            ]
            if missing_prompt_beats:
                self._update_progress(
                    store,
                    run.run_id,
                    owner_token,
                    step="prompt_generation",
                    message=f"正在补全 {len(missing_prompt_beats)} 个分镜的视频提示词...",
                )
                terminal = await self._run_task_with_retry(
                    store,
                    run.run_id,
                    owner_token,
                    ctx,
                    task_type="seedance2_prompt_batch",
                    queue_kind="default",
                    episode=ep_num,
                    payload={
                        "episode": ep_num,
                        "beat_numbers": missing_prompt_beats,
                        "only_missing": True,
                        "s9_tail_anchor": opts.tail_anchor,
                        "output_dir": str(output_dir),
                    },
                    label=f"第 {ep_num} 集视频提示词生成",
                    max_retries=opts.max_retries,
                )
                if terminal.status != "completed":
                    raise _RunPausedError(
                        f"第 {ep_num} 集视频提示词生成失败：{terminal.error or terminal.status}",
                        step="prompt_generation",
                    )
                sql = await self._open_project_sqlite(ctx)
                try:
                    beats = sorted(
                        await sql.get_beats_as_dicts(ep_num),
                        key=lambda b: int(b.get("beat_number") or 0),
                    )
                finally:
                    await sql.close()

        # Step 2.3: Batch Video Generation (Parallel OR Tail-Anchor Chain)
        paths = PathResolver(str(output_dir), ep_num)
        target_set = set(opts.target_beat_numbers)
        ordered_beats = [
            b
            for b in beats
            if int(b.get("beat_number") or 0) > 0
            and (not target_set or int(b.get("beat_number") or 0) in target_set)
        ]
        pending_beats = [
            b
            for b in ordered_beats
            if opts.force_regenerate_videos
            or not paths.video(int(b.get("beat_number") or 0)).exists()
        ]
        total_beats = len(ordered_beats)
        completed_count = total_beats - len(pending_beats)
        failed_beats: list[dict[str, Any]] = []
        fatal_error: str | None = None

        self._update_progress(
            store,
            run.run_id,
            owner_token,
            step="video_generation",
            message=(
                f"第 {ep_num} 集视频生成中（{completed_count}/{total_beats}"
                + ("，尾帧锚点接力模式" if opts.tail_anchor else "")
                + "）..."
            ),
            extra={
                "video_progress": {
                    "completed": completed_count,
                    "total": total_beats,
                    "failed": 0,
                    "tail_anchor": opts.tail_anchor,
                }
            },
        )

        async def _generate_one_beat(
            beat_num: int, *, return_last_frame: bool | None = None
        ) -> tuple[bool, TaskState | None, str]:
            attempt = 0
            while True:
                self._assert_active(store, run.run_id, owner_token)
                req_kwargs: dict[str, Any] = {
                    "video_backend": video_backend,
                    "use_director_render": opts.use_director_render,
                }
                if opts.resolution:
                    req_kwargs["resolution"] = opts.resolution
                if opts.ratio:
                    req_kwargs["ratio"] = opts.ratio
                if return_last_frame is not None:
                    req_kwargs["return_last_frame"] = return_last_frame
                single_req = SingleVideoRequest(**req_kwargs)

                async def _dispatch() -> dict[str, Any]:
                    beat_sql = await self._open_project_sqlite(ctx)
                    try:
                        return await enqueue_single_beat_video_for_context(
                            ctx=ctx,
                            username=ctx.owner_username,
                            project_name=ctx.project_name,
                            output_dir=str(output_dir),
                            state_dir=ctx.state_dir,
                            store=beat_sql,
                            episode_num=ep_num,
                            beat_num=beat_num,
                            body=single_req,
                        )
                    finally:
                        await beat_sql.close()

                res = await self._enqueue_with_backpressure(
                    store,
                    run.run_id,
                    owner_token,
                    _dispatch,
                    waiting_message=f"Beat {beat_num} 等待视频队列空位...",
                )
                if not res.get("ok"):
                    err = str(res.get("error") or f"Beat {beat_num} 视频入队预检失败")
                    return False, None, err

                terminal = await self._await_task(
                    store,
                    run.run_id,
                    owner_token,
                    ctx,
                    task_type="single_video",
                    episode=ep_num,
                    beat_num=beat_num,
                    expected_task_id=res.get("task_id"),
                )
                if terminal.status == "completed":
                    return True, terminal, ""
                if terminal.status == "cancelled":
                    raise _RunPausedError(f"Beat {beat_num} 视频任务已被取消", step="video_generation")

                err = str(terminal.error or f"Beat {beat_num} 视频生成失败")
                _cat, retryable, _fatal = classify_autopilot_error(err)
                if retryable and attempt < opts.max_retries:
                    delay = self._retry_delay(attempt)
                    attempt += 1
                    self._update_progress(
                        store,
                        run.run_id,
                        owner_token,
                        step="video_generation",
                        message=f"Beat {beat_num} 瞬时失败，{int(delay)}秒后第 {attempt}/{opts.max_retries} 次重试...",
                    )
                    await asyncio.sleep(delay)
                    continue
                return False, terminal, err

        if pending_beats:
            if opts.tail_anchor:
                # Sequential tail-anchor chain: Beat k -> extract/bind last frame -> Beat k+1
                beat_by_num = {int(b.get("beat_number") or 0): b for b in ordered_beats}
                ordered_nums = [int(b.get("beat_number") or 0) for b in ordered_beats]
                pending_set = {int(b.get("beat_number") or 0) for b in pending_beats}

                prev_tail_frame: Path | None = None
                for idx, beat_num in enumerate(ordered_nums):
                    is_last = idx + 1 == len(ordered_nums)
                    if beat_num not in pending_set:
                        # Already generated: grab its tail frame so the next pending beat can chain!
                        if not is_last:
                            prev_tail_frame = await resolve_or_extract_beat_last_frame(
                                output_dir=output_dir,
                                episode=ep_num,
                                beat_num=beat_num,
                            )
                        continue

                    if prev_tail_frame is not None and idx > 0:
                        chain_sql = await self._open_project_sqlite(ctx)
                        try:
                            await bind_tail_frame_to_next_beat(
                                store=chain_sql,
                                output_dir=output_dir,
                                state_dir=ctx.state_dir,
                                episode=ep_num,
                                next_beat=beat_by_num[beat_num],
                                tail_frame_path=prev_tail_frame,
                            )
                        finally:
                            await chain_sql.close()

                    ok, terminal, err = await _generate_one_beat(
                        beat_num,
                        return_last_frame=(not is_last),
                    )
                    if not ok:
                        cat, retryable, _is_fatal = classify_autopilot_error(err)
                        failed_beats.append(
                            {
                                "kind": "single_video",
                                "beat_number": beat_num,
                                "error": err,
                                "category": cat,
                                "retryable": retryable,
                            }
                        )
                        # In tail-anchor chain mode, Beat k+1 depends on Beat k's tail frame;
                        # stop the chain cleanly at Beat k so user fixes Beat k and resumes.
                        break

                    completed_count += 1
                    if not is_last:
                        prev_tail_frame = await resolve_or_extract_beat_last_frame(
                            output_dir=output_dir,
                            episode=ep_num,
                            beat_num=beat_num,
                            task_result=(terminal.result if terminal else None),
                        )
                    self._update_progress(
                        store,
                        run.run_id,
                        owner_token,
                        step="video_generation",
                        message=f"尾帧接力完成 Beat {beat_num}（{completed_count}/{total_beats}）",
                        extra={
                            "video_progress": {
                                "completed": completed_count,
                                "total": total_beats,
                                "failed": len(failed_beats),
                                "tail_anchor": True,
                            }
                        },
                    )
            else:
                # Standard parallel mode with bounded concurrency + beat failure isolation
                sem = asyncio.Semaphore(opts.max_concurrent_videos)
                progress_lock = asyncio.Lock()

                async def _parallel_worker(beat_num: int) -> None:
                    nonlocal completed_count, fatal_error
                    if fatal_error is not None:
                        return
                    async with sem:
                        if fatal_error is not None:
                            return
                        ok, _terminal, err = await _generate_one_beat(beat_num)
                        async with progress_lock:
                            if ok:
                                completed_count += 1
                            else:
                                cat, retryable, is_fatal = classify_autopilot_error(err)
                                failed_beats.append(
                                    {
                                        "kind": "single_video",
                                        "beat_number": beat_num,
                                        "error": err,
                                        "category": cat,
                                        "retryable": retryable,
                                    }
                                )
                                if is_fatal:
                                    fatal_error = err
                            self._update_progress(
                                store,
                                run.run_id,
                                owner_token,
                                step="video_generation",
                                message=(
                                    f"第 {ep_num} 集视频生成中（已完成 {completed_count}/{total_beats}"
                                    + (f"，失败 {len(failed_beats)}" if failed_beats else "")
                                    + "）..."
                                ),
                                extra={
                                    "video_progress": {
                                        "completed": completed_count,
                                        "total": total_beats,
                                        "failed": len(failed_beats),
                                        "tail_anchor": False,
                                    }
                                },
                            )

                await asyncio.gather(
                    *(
                        _parallel_worker(int(b.get("beat_number") or 0))
                        for b in pending_beats
                    )
                )

        if failed_beats:
            failed_nums = ", ".join(
                f"Beat {item['beat_number']}" for item in failed_beats
            )
            reason = (
                f"第 {ep_num} 集视频生成因账号/额度错误中止：{fatal_error}"
                if fatal_error
                else f"第 {ep_num} 集以下分镜视频生成失败（已隔离，其余已完成）：{failed_nums}"
            )
            raise _RunPausedError(
                reason,
                step="video_generation",
                failed_items=failed_beats,
            )

        if opts.skip_compose:
            progress = dict((store.get_by_run_id(run.run_id) or run).progress)
            progress["message"] = f"第 {ep_num} 集批量视频生成完成（{completed_count}/{total_beats}）"
            progress["failed_items"] = []
            progress["active_tasks"] = []
            store.update_fields(
                run.run_id,
                status="completed",
                phase="done",
                current_step="done",
                progress=progress,
                last_error="",
                only_if_active=True,
            )
            return

        # Step 2.4: Compose Episode Final Video
        self._update_progress(
            store,
            run.run_id,
            owner_token,
            step="compose",
            message=f"第 {ep_num} 集全部分镜视频已就绪，正在合成成片...",
        )
        sql = await self._open_project_sqlite(ctx)
        try:
            compose_beats = sorted(
                await sql.get_beats_as_dicts(ep_num),
                key=lambda b: int(b.get("beat_number") or 0),
            )
        finally:
            await sql.close()

        compose_resolution = "1280x720" if aspect_ratio == "16:9" else "720x1280"
        terminal = await self._run_task_with_retry(
            store,
            run.run_id,
            owner_token,
            ctx,
            task_type="compose_episode",
            queue_kind="ffmpeg",
            episode=ep_num,
            payload={
                "beats": compose_beats,
                "episode": ep_num,
                "output_dir": str(output_dir),
                "resolution": compose_resolution,
                "add_subtitles": opts.add_subtitles,
                "add_bgm": opts.add_bgm,
            },
            label=f"第 {ep_num} 集成片合成",
            max_retries=opts.max_retries,
        )
        if terminal.status != "completed":
            raise _RunPausedError(
                f"第 {ep_num} 集成片合成失败：{terminal.error or terminal.status}",
                step="compose",
            )

        final_video = output_dir / "videos" / "episodes" / f"ep{ep_num:03d}_final.mp4"
        progress = dict((store.get_by_run_id(run.run_id) or run).progress)
        progress["message"] = f"第 {ep_num} 集成片已合成完成"
        progress["failed_items"] = []
        progress["active_tasks"] = []
        progress["final_video_path"] = str(final_video)
        store.update_fields(
            run.run_id,
            status="completed",
            phase="done",
            current_step="done",
            progress=progress,
            last_error="",
            only_if_active=True,
        )


episode_autopilot_coordinator = EpisodeAutopilotCoordinator()

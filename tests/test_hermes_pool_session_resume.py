import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from novelvideo.ports.auth_contract import AgentSessionToken

pytestmark = pytest.mark.m08


class _FakeAuthService:
    def __init__(self) -> None:
        self.created = 0
        self.updated: list[tuple[str, dict]] = []
        self.revoked: list[str] = []

    async def create_agent_session(self, **kwargs):
        self.created += 1
        return AgentSessionToken(
            value=f"token-{self.created}",
            session_id=f"agent-session-{self.created}",
            user=str(kwargs["username"]),
            scopes=tuple(kwargs["scopes"]),
            exp=int(time.time()) + 3600,
            worker_id=str(kwargs["worker_id"]),
            agent_kind=str(kwargs["agent_kind"]),
        )

    async def revoke_agent_session(self, raw_token: str) -> bool:
        self.revoked.append(raw_token)
        return True

    async def update_agent_session_scope(self, raw_token: str, **kwargs) -> bool:
        self.updated.append((raw_token, kwargs))
        return True


class _FakeThread:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def warm(self) -> None:
        pass

    @property
    def is_closed(self) -> bool:
        return self.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "draining"])
async def test_reaper_keeps_in_flight_turn_even_after_idle_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    from novelvideo.chat import hermes_pool

    pool, _calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    try:
        thread = await pool.get_for_user("alice")
        pool._cleanup_task.cancel()
        await asyncio.gather(pool._cleanup_task, return_exceptions=True)
        slot = pool._slots["alice"]
        slot.active_turns = 1
        slot.state = state
        slot.last_used = time.time() - pool._idle_kill_secs - 1
        cycles = 0

        async def one_reaper_cycle(_seconds):
            nonlocal cycles
            cycles += 1
            if cycles > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(hermes_pool.asyncio, "sleep", one_reaper_cycle)
        await pool._reaper_loop()
        assert pool._slots["alice"] is slot
        assert not thread.closed
        assert fake_auth.revoked == []
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_finish_turn_starts_idle_clock_at_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novelvideo.chat import hermes_pool

    pool, _calls, _fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    try:
        await pool.get_for_user("alice")
        slot = pool._slots["alice"]
        await pool._begin_turn(slot)
        completed_at = slot.last_used + 100
        monkeypatch.setattr(hermes_pool.time, "time", lambda: completed_at)
        await pool._finish_turn(slot)
        assert slot.active_turns == 0
        assert slot.last_used == completed_at
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_lru_evicts_only_idle_workers_and_refuses_when_all_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novelvideo.chat.hermes_pool import HermesDrainingError

    pool, _calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    try:
        busy = await pool.get_for_user("alice")
        idle = await pool.get_for_user("bob")
        pool._max_workers = 2
        active_slot = pool._slots["alice"]
        await pool._begin_turn(active_slot)
        active_slot.last_used = 0
        await pool._evict_lru_if_full()
        assert not busy.closed
        assert idle.closed
        assert fake_auth.revoked == ["token-2"]

        pool._max_workers = 1
        with pytest.raises(HermesDrainingError):
            await pool._evict_lru_if_full()
        assert pool._slots["alice"] is active_slot
        assert not busy.closed
        assert fake_auth.revoked == ["token-2"]
    finally:
        await pool.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["scope", "gateway", "dirty"])
async def test_rotation_waits_for_active_turn_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    from novelvideo.chat.hermes_pool import HermesDrainingError

    pool, calls, fake_auth, gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    try:
        thread = await pool.get_for_user("alice", project_id="project_a", scope_kind="project")
        slot = pool._slots["alice"]
        await pool._begin_turn(slot)
        project = "project_b" if reason == "scope" else "project_a"
        if reason == "gateway":
            gateway["fingerprint"] = "gateway-2"
        if reason == "dirty":
            pool.mark_user_profile_dirty("alice")

        with pytest.raises(HermesDrainingError):
            await pool.get_for_user("alice", project_id=project, scope_kind="project")
        assert pool._slots["alice"] is slot
        assert not thread.closed
        assert fake_auth.revoked == []
        assert calls == [("start", None)]
        if reason == "dirty":
            assert "alice" in pool._dirty_profiles

        await pool._finish_turn(slot)
        await pool.get_for_user("alice", project_id=project, scope_kind="project")
        assert thread.closed
        assert "alice" not in pool._dirty_profiles
        assert len(calls) == 2
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_prewarm_other_scope_does_not_interrupt_active_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    try:
        thread = await pool.get_for_user("alice", project_id="project_a", scope_kind="project")
        slot = pool._slots["alice"]
        await pool._begin_turn(slot)
        await pool.prewarm("alice", project_id="project_b", scope_kind="project")
        assert pool._slots["alice"] is slot
        assert not thread.closed
        assert calls == [("start", None)]
        assert fake_auth.revoked == []
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_close_user_thread_requires_exact_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, _calls, fake_auth, _gateway = _patch_fake_hermes_pool(
        tmp_path, monkeypatch
    )
    thread = await pool.get_for_user(
        "alice", scope_kind="project", project_id="project_a"
    )

    assert await pool.close_user_thread("alice", "main", "other-session") is False
    assert thread.closed is False
    assert await pool.close_user_thread("alice", "main", thread.id) is True
    assert thread.closed is True
    assert fake_auth.revoked == ["token-1"]


def test_hermes_session_pointer_is_durable_in_runtime_home(tmp_path: Path) -> None:
    from novelvideo.chat.hermes_pool import HermesPool

    home = tmp_path / "project-state" / "agents" / "hermes" / "director"
    home.mkdir(parents=True)
    HermesPool._persist_session_id(
        home,
        "project",
        "project_a",
        "main",
        None,
        "session-123",
    )

    restarted_pool = HermesPool(max_workers=1)
    assert restarted_pool._persisted_session_id_for(
        home, "project", "project_a", "main", None
    ) == "session-123"

    restarted_pool._forget_session(
        "alice",
        "project",
        "project_a",
        "main",
        None,
        home=home,
    )
    assert restarted_pool._persisted_session_id_for(
        home, "project", "project_a", "main", None
    ) is None


def _write_fake_hermes_cli(path: Path) -> None:
    """A stand-in CLI. The version it reports no longer matters.

    It used to have to echo whatever `.hermes-version` held, because the pool
    refused to spawn on a mismatch. That gate is gone: a version string cannot
    tell the fork from stock — the fork keeps upstream's version — so it was
    checking the weaker property while costing an edit on every alignment.
    What the pool checks now is behaviour, in `require_hermes_fork`.
    """
    path.write_text(
        "#!/bin/sh\necho 'Hermes Agent v0.0.0-test'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _patch_fake_hermes_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from novelvideo.chat import hermes_pool
    from novelvideo.ports import registry

    calls: list[tuple[str, str | None]] = []
    started_count = 0
    fake_auth = _FakeAuthService()
    gateway = {"fingerprint": "gateway-1"}
    fake_cli = tmp_path / "hermes"
    _write_fake_hermes_cli(fake_cli)

    class FakeHermesSdkClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def thread_start(self) -> _FakeThread:
            nonlocal started_count
            started_count += 1
            calls.append(("start", None))
            return _FakeThread(f"session-{started_count}")

        def thread_resume(self, session_id: str) -> _FakeThread:
            calls.append(("resume", session_id))
            return _FakeThread(session_id)

    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("auth_session", fake_auth)
    monkeypatch.setattr(hermes_pool, "_hermes_cli_path", lambda: fake_cli)
    monkeypatch.setattr(hermes_pool, "require_hermes_fork", lambda _path: None)
    monkeypatch.setattr(
        hermes_pool,
        "ensure_user_hermes_workspace",
        lambda _user, profile="director", **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        hermes_pool,
        "gateway_origin_fingerprint",
        lambda: gateway["fingerprint"],
    )
    monkeypatch.setattr(hermes_pool, "require_hermes_fork", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hermes_pool, "HermesSdkClient", FakeHermesSdkClient)

    pool = hermes_pool.HermesPool(max_workers=5)

    async def fake_project_env(*_args, **_kwargs):
        return {"DRAMACLAW_PROJECT_STATE_DIR": str(tmp_path / "project-state")}

    monkeypatch.setattr(pool, "_project_env", fake_project_env)
    return pool, calls, fake_auth, gateway


def test_hermes_worker_receives_effective_newapi_key_without_mutating_host_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.chat import hermes_pool

    monkeypatch.delenv("NEWAPI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        hermes_pool,
        "effective_gateway_credentials",
        lambda: ("worker-only-key", "https://newapi.example/v1"),
    )
    token = AgentSessionToken(
        value="agent-token",
        session_id="agent-session",
        user="alice",
        scopes=("projects:read",),
        exp=int(time.time()) + 3600,
        worker_id="worker-1",
        agent_kind="hermes",
    )

    pool = hermes_pool.HermesPool(max_workers=1)
    env = pool._build_env(
        tmp_path,
        "alice",
        token,
        project_id=None,
    )

    # The real gateway key is delivered per turn through ACP metadata; the
    # long-lived worker only receives a non-secret placeholder.
    assert env["NEWAPI_API_KEY"] == "dramaclaw-per-turn-placeholder"
    assert env["OPENAI_API_KEY"] == "dramaclaw-per-turn-placeholder"
    assert "NEWAPI_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ


def test_hermes_worker_defaults_to_ce_edition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novelvideo.chat import hermes_pool

    monkeypatch.delenv("ST_EDITION", raising=False)
    # 即便宿主进程有控制面 DSN,也绝不能泄漏进 worker 子进程环境。
    monkeypatch.setenv("ST_CONTROL_PLANE_DSN", "postgres://cp/db")
    monkeypatch.setattr(hermes_pool, "effective_gateway_credentials", lambda: ("", ""))
    token = AgentSessionToken(
        value="agent-token",
        session_id="agent-session",
        user="alice",
        scopes=("projects:read",),
        exp=int(time.time()) + 3600,
        worker_id="worker-1",
        agent_kind="hermes",
    )

    pool = hermes_pool.HermesPool(max_workers=1)
    env = pool._build_env(
        tmp_path,
        "alice",
        token,
        project_id=None,
    )

    assert env["ST_EDITION"] == "ce"
    assert "ST_CONTROL_PLANE_DSN" not in env


def test_hermes_worker_receives_novelvideo_data_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novelvideo.chat import hermes_pool

    monkeypatch.setattr(hermes_pool, "effective_gateway_credentials", lambda: ("", ""))
    token = AgentSessionToken(
        value="agent-token",
        session_id="agent-session",
        user="alice",
        scopes=("projects:read",),
        exp=int(time.time()) + 3600,
        worker_id="worker-1",
        agent_kind="hermes",
    )

    pool = hermes_pool.HermesPool(max_workers=1)
    env = pool._build_env(
        tmp_path,
        "alice",
        token,
        project_id=None,
    )

    assert env["NOVELVIDEO_OUTPUT_DIR"] == str(hermes_pool.config.OUTPUT_DIR)
    assert env["NOVELVIDEO_STATE_DIR"] == str(hermes_pool.config.STATE_DIR)
    assert env["NOVELVIDEO_RUNTIME_DIR"] == str(hermes_pool.config.RUNTIME_DIR)


def test_freezone_hermes_worker_disables_skill_manage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.chat import hermes_pool

    monkeypatch.setattr(hermes_pool, "effective_gateway_credentials", lambda: ("", ""))
    token = AgentSessionToken(
        value="agent-token",
        session_id="agent-session",
        user="alice",
        scopes=("projects:read",),
        exp=int(time.time()) + 3600,
        worker_id="worker-1",
        agent_kind="hermes-freezone",
    )

    pool = hermes_pool.HermesPool(max_workers=1)
    env = pool._build_env(
        tmp_path,
        "alice",
        token,
        agent_profile="freezone",
        project_id=None,
    )

    assert env["DRAMACLAW_DISABLE_HERMES_SKILL_MANAGE"] == "1"
    assert env["DRAMACLAW_HERMES_TOOL_DENY"] == ",".join(
        hermes_pool.FREEZONE_HERMES_TOOL_DENY
    )
    assert "terminal" in env["DRAMACLAW_HERMES_TOOL_DENY"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / ".dramaclaw-python")


@pytest.mark.asyncio
async def test_hermes_pool_uses_separate_sessions_per_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)

    try:
        first = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        second = await pool.get_for_user("alice", scope_kind="project", project_id="project_b")
        third = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        fourth = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
    finally:
        await pool.close_all()

    assert first.id == "session-1"
    assert second.id == "session-2"
    assert third.id == "session-1"
    assert fourth.id == "session-1"
    assert calls == [("start", None), ("start", None), ("resume", "session-1")]
    assert fake_auth.created == 3
    assert fake_auth.updated == [
        ("token-3", {"scope_kind": "project", "project_id": "project_a"}),
    ]
    assert fake_auth.revoked == ["token-1", "token-2", "token-3"]


@pytest.mark.asyncio
async def test_hermes_pool_uses_separate_sessions_per_agent_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)

    try:
        main = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        freezone = await pool.get_for_user(
            "alice",
            agent_profile="freezone",
            tool_mode="freezone_canvas",
            scope_kind="project",
            project_id="project_a",
        )
        main_again = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        freezone_again = await pool.get_for_user(
            "alice",
            agent_profile="freezone",
            tool_mode="freezone_canvas",
            scope_kind="project",
            project_id="project_a",
        )
    finally:
        await pool.close_all()

    assert main.id == "session-1"
    assert freezone.id == "session-2"
    assert main_again.id == "session-1"
    assert freezone_again.id == "session-2"
    assert calls == [("start", None), ("start", None)]
    assert sorted(pool._session_ids["alice"]) == [
        ("freezone", "project", "project_a", None),
        ("main", "project", "project_a", None),
    ]
    assert fake_auth.created == 2


@pytest.mark.asyncio
async def test_hermes_pool_freezone_profile_uses_isolated_home_and_canvas_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.chat import hermes_pool
    from novelvideo.ports import registry

    fake_auth = _FakeAuthService()
    fake_cli = tmp_path / "hermes"
    _write_fake_hermes_cli(fake_cli)
    workspaces: list[tuple[str, str, str | None]] = []
    client_kwargs: list[dict] = []

    class FakeHermesSdkClient:
        def __init__(self, **kwargs) -> None:
            client_kwargs.append(kwargs)

        def thread_start(self) -> _FakeThread:
            return _FakeThread("session-freezone")

        def thread_resume(self, session_id: str) -> _FakeThread:
            return _FakeThread(session_id)

    def fake_workspace(
        username: str,
        *,
        profile: str = "director",
        project_state_dir=None,
    ) -> Path:
        workspaces.append((username, profile, project_state_dir))
        home = Path(project_state_dir) / "agents" / "hermes" / profile
        (home / "tmp").mkdir(parents=True, exist_ok=True)
        return home

    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("auth_session", fake_auth)
    monkeypatch.setattr(hermes_pool, "_hermes_cli_path", lambda: fake_cli)
    monkeypatch.setattr(hermes_pool, "require_hermes_fork", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hermes_pool, "ensure_user_hermes_workspace", fake_workspace)
    monkeypatch.setattr(hermes_pool, "HermesSdkClient", FakeHermesSdkClient)

    pool = hermes_pool.HermesPool(max_workers=5)

    async def fake_project_env(*_args, **_kwargs):
        return {"DRAMACLAW_PROJECT_STATE_DIR": str(tmp_path / "project-state")}

    monkeypatch.setattr(pool, "_project_env", fake_project_env)

    try:
        thread = await pool.get_for_user(
            "alice",
            agent_profile="freezone",
            tool_mode="freezone_canvas",
            scope_kind="project",
            project_id="project_a",
            canvas_id="canvas_123",
        )
    finally:
        await pool.close_all()

    assert thread.id == "session-freezone"
    project_state = tmp_path / "project-state"
    expected_home = project_state / "agents" / "hermes" / "freezone"
    assert workspaces == [("alice", "freezone", str(project_state))]
    assert client_kwargs[0]["cwd"] == expected_home
    env = client_kwargs[0]["env"]
    assert env["HERMES_HOME"] == str(expected_home)
    assert env["DRAMACLAW_CANVAS_ID"] == "canvas_123"
    assert env["SUPERTALE_CANVAS_ID"] == "canvas_123"
    assert env["DRAMACLAW_CHAT_SURFACE"] == "freezone"
    persisted_sessions = json.loads(
        (expected_home / "dramaclaw_sessions.json").read_text(encoding="utf-8")
    )
    assert persisted_sessions == {
        '["freezone","project","project_a","canvas_123"]': "session-freezone"
    }


@pytest.mark.asyncio
async def test_hermes_pool_resumes_current_project_session_when_renewing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)

    try:
        first = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        pool._slots["alice"].token = AgentSessionToken(
            value="expired-token",
            session_id="expired-agent-session",
            user="alice",
            scopes=("projects:read",),
            exp=0,
            worker_id="expired-worker",
            agent_kind="hermes",
        )
        second = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
    finally:
        await pool.close_all()

    assert first.id == "session-1"
    assert second.id == "session-1"
    assert calls == [("start", None), ("resume", "session-1")]
    assert fake_auth.created == 2
    assert fake_auth.revoked == ["expired-token", "token-2"]


@pytest.mark.asyncio
async def test_hermes_pool_starts_fresh_session_when_resume_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.chat import hermes_pool
    from novelvideo.ports import registry

    calls: list[tuple[str, str | None]] = []
    fake_auth = _FakeAuthService()
    fake_cli = tmp_path / "hermes"
    _write_fake_hermes_cli(fake_cli)

    class FakeHermesSdkClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def thread_start(self) -> _FakeThread:
            calls.append(("start", None))
            return _FakeThread("fresh-session")

        def thread_resume(self, session_id: str) -> _FakeThread:
            calls.append(("resume", session_id))
            raise RuntimeError("session stale-session not found")

    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("auth_session", fake_auth)
    monkeypatch.setattr(hermes_pool, "_hermes_cli_path", lambda: fake_cli)
    monkeypatch.setattr(
        hermes_pool,
        "ensure_user_hermes_workspace",
        lambda _user, profile="director", **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        hermes_pool,
        "gateway_origin_fingerprint",
        lambda: "gateway-1",
    )
    monkeypatch.setattr(hermes_pool, "require_hermes_fork", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hermes_pool, "HermesSdkClient", FakeHermesSdkClient)

    pool = hermes_pool.HermesPool(max_workers=5)
    pool._session_ids = {
        "alice": {
            ("main", "project", "project_a", None): "stale-session",
        },
    }

    async def fake_project_env(*_args, **_kwargs):
        return {"DRAMACLAW_PROJECT_STATE_DIR": str(tmp_path / "project-state")}

    monkeypatch.setattr(pool, "_project_env", fake_project_env)

    try:
        thread = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
    finally:
        await pool.close_all()

    assert thread.id == "fresh-session"
    assert calls == [("resume", "stale-session"), ("start", None)]
    assert pool._session_ids["alice"][("main", "project", "project_a", None)] == "fresh-session"
    assert fake_auth.created == 1


@pytest.mark.asyncio
async def test_hermes_pool_surfaces_error_when_resume_and_fresh_start_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.chat import hermes_pool
    from novelvideo.ports import registry

    calls: list[tuple[str, str | None]] = []
    fake_auth = _FakeAuthService()
    fake_cli = tmp_path / "hermes"
    _write_fake_hermes_cli(fake_cli)

    class FakeHermesSdkClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def thread_start(self) -> _FakeThread:
            calls.append(("start", None))
            raise RuntimeError("No LLM provider configured")

        def thread_resume(self, session_id: str) -> _FakeThread:
            calls.append(("resume", session_id))
            raise RuntimeError("session stale-session not found")

    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("auth_session", fake_auth)
    monkeypatch.setattr(hermes_pool, "_hermes_cli_path", lambda: fake_cli)
    monkeypatch.setattr(
        hermes_pool,
        "ensure_user_hermes_workspace",
        lambda _user, profile="director", **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        hermes_pool,
        "gateway_origin_fingerprint",
        lambda: "gateway-1",
    )
    monkeypatch.setattr(hermes_pool, "require_hermes_fork", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hermes_pool, "HermesSdkClient", FakeHermesSdkClient)

    pool = hermes_pool.HermesPool(max_workers=5)
    pool._session_ids = {
        "alice": {
            ("main", "project", "project_a", None): "stale-session",
        },
    }

    async def fake_project_env(*_args, **_kwargs):
        return {"DRAMACLAW_PROJECT_STATE_DIR": str(tmp_path / "project-state")}

    monkeypatch.setattr(pool, "_project_env", fake_project_env)

    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        await pool.get_for_user("alice", scope_kind="project", project_id="project_a")

    assert calls == [("resume", "stale-session"), ("start", None)]
    assert "alice" not in pool._session_ids


@pytest.mark.asyncio
async def test_hermes_pool_does_not_restore_stale_session_after_rotation_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.chat import hermes_pool
    from novelvideo.ports import registry

    calls: list[tuple[str, str | None]] = []
    fake_auth = _FakeAuthService()
    fake_cli = tmp_path / "hermes"
    _write_fake_hermes_cli(fake_cli)
    starts = 0

    class FakeHermesSdkClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def thread_start(self) -> _FakeThread:
            nonlocal starts
            starts += 1
            session_id = "stale-session" if starts == 1 else "fresh-session"
            calls.append(("start", None))
            return _FakeThread(session_id)

        def thread_resume(self, session_id: str) -> _FakeThread:
            calls.append(("resume", session_id))
            raise RuntimeError(f"session {session_id} not found")

    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("auth_session", fake_auth)
    monkeypatch.setattr(hermes_pool, "_hermes_cli_path", lambda: fake_cli)
    monkeypatch.setattr(
        hermes_pool,
        "ensure_user_hermes_workspace",
        lambda _user, profile="director", **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        hermes_pool,
        "gateway_origin_fingerprint",
        lambda: "gateway-1",
    )
    monkeypatch.setattr(hermes_pool, "require_hermes_fork", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hermes_pool, "HermesSdkClient", FakeHermesSdkClient)

    pool = hermes_pool.HermesPool(max_workers=5)

    async def fake_project_env(*_args, **_kwargs):
        return {"DRAMACLAW_PROJECT_STATE_DIR": str(tmp_path / "project-state")}

    monkeypatch.setattr(pool, "_project_env", fake_project_env)

    try:
        first = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        pool._slots["alice"].token = AgentSessionToken(
            value="expired-token",
            session_id="expired-agent-session",
            user="alice",
            scopes=("projects:read",),
            exp=0,
            worker_id="expired-worker",
            agent_kind="hermes",
        )
        second = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
    finally:
        await pool.close_all()

    assert first.id == "stale-session"
    assert second.id == "fresh-session"
    assert calls == [("start", None), ("resume", "stale-session"), ("start", None)]
    assert pool._session_ids["alice"][("main", "project", "project_a", None)] == "fresh-session"


@pytest.mark.asyncio
async def test_hermes_pool_reset_for_user_forgets_cached_session_and_starts_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)

    try:
        first = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        pool._remember_session(pool._slots["alice"])
        second = await pool.reset_for_user(
            "alice",
            scope_kind="project",
            project_id="project_a",
        )
    finally:
        await pool.close_all()

    assert first.id == "session-1"
    assert second.id == "session-2"
    assert calls == [("start", None), ("start", None)]
    assert fake_auth.created == 2
    assert fake_auth.revoked == ["token-1", "token-2"]


@pytest.mark.asyncio
async def test_hermes_pool_restarts_dirty_freezone_profile_on_next_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)

    try:
        first = await pool.get_for_user(
            "alice",
            agent_profile="freezone:agent-1",
            tool_mode="freezone_canvas",
            scope_kind="project",
            project_id="project_a",
            surface="freezone",
            canvas_id="canvas_a",
        )
        pool.mark_user_profile_dirty("alice", "freezone:agent-1")
        second = await pool.get_for_user(
            "alice",
            agent_profile="freezone:agent-1",
            tool_mode="freezone_canvas",
            scope_kind="project",
            project_id="project_a",
            surface="freezone",
            canvas_id="canvas_a",
        )
    finally:
        await pool.close_all()

    assert second is not first
    assert second.id == "session-2"
    assert calls == [("start", None), ("start", None)]
    assert fake_auth.created == 2
    assert fake_auth.revoked == ["token-1", "token-2"]


@pytest.mark.asyncio
async def test_hermes_pool_rotates_closed_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)

    try:
        first = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
        await first.close()
        second = await pool.get_for_user("alice", scope_kind="project", project_id="project_a")
    finally:
        await pool.close_all()

    assert first.closed is True
    assert second is not first
    assert second.id == "session-1"
    assert calls == [("start", None), ("resume", "session-1")]
    assert fake_auth.created == 2
    assert fake_auth.revoked == ["token-1", "token-2"]


@pytest.mark.asyncio
async def test_hermes_pool_rotates_and_resumes_when_gateway_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)

    try:
        first = await pool.get_for_user(
            "alice", scope_kind="project", project_id="project_a"
        )
        gateway["fingerprint"] = "gateway-2"
        second = await pool.get_for_user(
            "alice", scope_kind="project", project_id="project_a"
        )
    finally:
        await pool.close_all()

    assert second is not first
    assert second.id == first.id == "session-1"
    assert calls == [("start", None), ("resume", "session-1")]
    assert fake_auth.created == 2
    assert fake_auth.revoked == ["token-1", "token-2"]


@pytest.mark.asyncio
async def test_hermes_pool_clear_scope_retires_idle_slot_and_next_turn_starts_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    workspace = _use_single_hermes_workspace(tmp_path, monkeypatch)

    try:
        first = await pool.get_for_user(
            "alice", scope_kind="project", project_id="project_a"
        )
        pool._remember_session(pool._slots["alice"])
        await pool.clear_scope(
            "alice",
            scope_kind="project",
            project_id="project_a",
            agent_profile="main",
            canvas_id=None,
            project_state_dir=workspace,
        )
        assert "alice" not in pool._slots
        # The detached pointer is dropped from disk, not just from memory.
        assert json.loads(
            (workspace / "dramaclaw_sessions.json").read_text(encoding="utf-8")
        ) == {}
        second = await pool.get_for_user(
            "alice", scope_kind="project", project_id="project_a"
        )
    finally:
        await pool.close_all()

    # The retired worker is closed, its agent session revoked, and the next
    # turn starts a brand-new Hermes session instead of resuming the old one.
    assert first.closed is True
    assert second.id == "session-2"
    assert calls == [("start", None), ("start", None)]
    assert fake_auth.created == 2
    assert fake_auth.revoked == ["token-1", "token-2"]


@pytest.mark.asyncio
async def test_hermes_pool_clear_scope_leaves_other_canvas_and_profile_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the selected canvas loses its session pointer.

    Freezone canvases of one agent share a single worker slot, so switching
    canvases rotates that worker regardless of clear. What clear owns is the
    session pointer: the selected canvas must start over while its sibling
    canvas and the sibling agent resume their existing sessions.
    """
    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    workspace = _use_single_hermes_workspace(tmp_path, monkeypatch)

    async def open_turn(agent: str, canvas: str):
        return await pool.get_for_user(
            "alice",
            agent_profile=f"freezone:{agent}",
            tool_mode="freezone_canvas",
            surface="freezone",
            scope_kind="project",
            project_id="project_a",
            canvas_id=canvas,
        )

    try:
        canvas_one = await open_turn("main", "canvas-1")
        await open_turn("main", "canvas-2")
        other_agent = await open_turn("other", "canvas-1")
        for slot in pool._slots.values():
            pool._remember_session(slot)
        await pool.clear_scope(
            "alice",
            scope_kind="project",
            project_id="project_a",
            agent_profile="freezone:main",
            canvas_id="canvas-1",
            project_state_dir=workspace,
        )
        after_clear = dict(pool._session_ids["alice"])
        selected = await open_turn("main", "canvas-1")
        sibling = await open_turn("main", "canvas-2")
        sibling_agent = await open_turn("other", "canvas-1")
        resumed = (selected.id, sibling.id, sibling_agent.id)
    finally:
        await pool.close_all()

    # canvas-1's pointer is gone; canvas-2 and the other agent keep theirs.
    assert after_clear == {
        ("freezone:main", "project", "project_a", "canvas-2"): "session-2",
        ("freezone:other", "project", "project_a", "canvas-1"): "session-3",
    }
    assert ("freezone:main", "project", "project_a", "canvas-1") not in after_clear
    # The cleared canvas starts fresh; the untouched ones resume.
    assert resumed == ("session-4", "session-2", "session-3")
    # The retired session is never resumed again.
    assert ("resume", "session-1") not in calls
    assert ("resume", "session-2") in calls
    assert canvas_one.closed is True
    assert other_agent.id == "session-3"
    # Every retired worker's agent session was revoked; none leaked.
    assert sorted(fake_auth.revoked) == sorted(
        f"token-{n}" for n in range(1, fake_auth.created + 1)
    )


@pytest.mark.asyncio
async def test_hermes_pool_clear_scope_refuses_while_a_turn_is_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.chat.session_registry import CHAT_RUN_BUSY_MESSAGE

    pool, calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    workspace = _use_single_hermes_workspace(tmp_path, monkeypatch)

    try:
        thread = await pool.get_for_user(
            "alice", scope_kind="project", project_id="project_a"
        )
        slot = pool._slots["alice"]
        pool._remember_session(slot)
        slot.active_turns = 1
        with pytest.raises(RuntimeError, match=CHAT_RUN_BUSY_MESSAGE):
            await pool.clear_scope(
                "alice",
                scope_kind="project",
                project_id="project_a",
                agent_profile="main",
                canvas_id=None,
                project_state_dir=workspace,
            )
        # Nothing was retired or forgotten by the refused clear.
        assert pool._slots["alice"] is slot
        assert thread.closed is False
        assert slot.active_turns == 1
        assert fake_auth.revoked == []
        assert pool._persisted_session_id_for(
            workspace, "project", "project_a", "main", None
        ) == "session-1"
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_hermes_pool_clear_scope_keeps_transcript_when_index_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _calls, fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    workspace = _use_single_hermes_workspace(tmp_path, monkeypatch)
    index = workspace / "dramaclaw_sessions.json"
    index.write_text("{not json", encoding="utf-8")

    try:
        thread = await pool.get_for_user(
            "alice", scope_kind="project", project_id="project_a"
        )
        slot = pool._slots["alice"]
        with pytest.raises(ValueError):
            await pool.clear_scope(
                "alice",
                scope_kind="project",
                project_id="project_a",
                agent_profile="main",
                canvas_id=None,
                project_state_dir=workspace,
            )
        # The worker is untouched and the unreadable index was not overwritten,
        # so the caller can still fall back to the on-disk transcript.
        assert pool._slots["alice"] is slot
        assert thread.closed is False
        assert fake_auth.revoked == []
        assert index.read_text(encoding="utf-8") == "{not json"
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_hermes_pool_clear_scope_does_not_touch_other_profiles_memory_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _calls, _fake_auth, _gateway = _patch_fake_hermes_pool(tmp_path, monkeypatch)
    workspace = _use_single_hermes_workspace(tmp_path, monkeypatch)

    try:
        director = await pool.get_for_user(
            "alice", scope_kind="project", project_id="project_a"
        )
        freezone = await pool.get_for_user(
            "alice",
            agent_profile="freezone:main",
            tool_mode="freezone_canvas",
            surface="freezone",
            scope_kind="project",
            project_id="project_a",
            canvas_id="canvas-1",
        )
        pool._remember_session(pool._slots["alice:freezone:main"])
        await pool.clear_scope(
            "alice",
            scope_kind="project",
            project_id="project_a",
            agent_profile="main",
            canvas_id=None,
            project_state_dir=workspace,
        )
        retired = (director.closed, freezone.closed)
        pointers = dict(pool._session_ids.get("alice") or {})
        freezone_id = freezone.id
    finally:
        await pool.close_all()

    assert retired == (True, False)
    assert pointers == {
        ("freezone:main", "project", "project_a", "canvas-1"): freezone_id
    }


def _use_single_hermes_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point spawn, remember and clear at one Hermes home.

    ``_patch_fake_hermes_pool`` hands every worker ``tmp_path`` as its home while
    ``clear_scope`` resolves the durable session index through
    ``hermes_workspace_path``.  Pinning both at one directory lets the tests
    assert against the index file the pool actually wrote.
    """
    from novelvideo.chat import hermes_pool

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        hermes_pool, "ensure_user_hermes_workspace", lambda *_args, **_kwargs: workspace
    )
    monkeypatch.setattr(
        hermes_pool, "hermes_workspace_path", lambda *_args, **_kwargs: workspace
    )
    return workspace

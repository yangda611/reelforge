from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from novelvideo.api.routes import chat as chat_routes
from novelvideo.chat import service as chat_service
from novelvideo.chat.store import ChatScope, ChatStore
from novelvideo.project_context import ProjectContext


@pytest.mark.asyncio
async def test_clear_messages_preserves_other_canvas_agent_and_settings(tmp_path):
    store = ChatStore()
    first = ChatScope(
        kind="project",
        id="project-1",
        surface="freezone",
        canvas_id="canvas-1",
        agent_id="main",
        state_dir=str(tmp_path),
    )
    other_agent = ChatScope(
        kind="project",
        id="project-1",
        surface="freezone",
        canvas_id="canvas-1",
        agent_id="other",
        state_dir=str(tmp_path),
    )
    await store.append_message_async("alice", first, "user", "旧消息", turn_id="turn-1")
    await store.append_message_async("alice", other_agent, "user", "保留消息")
    db = await store.connect_async("alice", first)
    try:
        await db.execute(
            "INSERT INTO chat_ui_events(turn_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("turn-1", "test", "{}", "2026-01-01T00:00:00Z"),
        )
        await db.execute(
            "INSERT INTO chat_settings(key, value, updated_at) VALUES (?, ?, ?)",
            ("theme", "dark", "2026-01-01T00:00:00Z"),
        )
        await db.commit()
    finally:
        await db.close()

    assert await store.clear_messages_async("alice", first) == 1
    assert await store.list_messages_async("alice", first) == []
    assert len(await store.list_messages_async("alice", other_agent)) == 1
    db = await store.connect_async("alice", first)
    try:
        events = await (
            await db.execute("SELECT COUNT(*) FROM chat_ui_events")
        ).fetchone()
        setting = await (
            await db.execute("SELECT value FROM chat_settings WHERE key='theme'")
        ).fetchone()
        assert events[0] == 0
        assert setting[0] == "dark"
    finally:
        await db.close()


def test_reset_codex_scope_thread_preserves_other_scopes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "_active_codex_turns_path",
        lambda _username: tmp_path / "active_codex_turns.json",
    )
    chat_service._set_codex_thread_id(
        "alice",
        "project-1",
        "thread-1",
        agent_profile="freezone:main",
        canvas_id="canvas-1",
        project_state_dir=tmp_path,
    )
    chat_service._set_codex_thread_id(
        "alice",
        "project-1",
        "thread-2",
        agent_profile="freezone:other",
        canvas_id="canvas-1",
        project_state_dir=tmp_path,
    )

    chat_service.reset_codex_scope_thread(
        "alice",
        "project-1",
        agent_profile="freezone:main",
        canvas_id="canvas-1",
        project_state_dir=tmp_path,
    )

    assert (
        chat_service._get_codex_thread_id(
            "alice",
            "project-1",
            agent_profile="freezone:main",
            canvas_id="canvas-1",
            project_state_dir=tmp_path,
        )
        is None
    )
    assert (
        chat_service._get_codex_thread_id(
            "alice",
            "project-1",
            agent_profile="freezone:other",
            canvas_id="canvas-1",
            project_state_dir=tmp_path,
        )
        == "thread-2"
    )


@pytest.mark.asyncio
async def test_archive_codex_scope_thread_releases_only_selected_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        chat_service,
        "_active_codex_turns_path",
        lambda _username: tmp_path / "active_codex_turns.json",
    )
    selected_key = chat_service._codex_scope_key(
        "project-1", agent_profile="freezone:main", canvas_id="canvas-1"
    )
    chat_service._set_codex_thread_id(
        "alice",
        "project-1",
        "thread-selected",
        agent_profile="freezone:main",
        canvas_id="canvas-1",
        project_state_dir=tmp_path,
    )
    chat_service._set_codex_thread_id(
        "alice",
        "project-1",
        "thread-other",
        agent_profile="freezone:other",
        canvas_id="canvas-1",
        project_state_dir=tmp_path,
    )
    chat_service._set_active_codex_turn(
        "alice", selected_key, ("thread-selected", "turn-1")
    )
    calls = []
    monkeypatch.setattr(
        chat_service,
        "_control_codex_thread",
        lambda operation, thread_id, turn_id=None: calls.append(
            (operation, thread_id, turn_id)
        )
        or True,
    )

    count = await chat_service.archive_codex_scope_thread(
        "alice",
        "project-1",
        agent_profile="freezone:main",
        canvas_id="canvas-1",
        project_state_dir=tmp_path,
    )

    assert count == 1
    assert calls == [("archive", "thread-selected", None)]
    assert (
        chat_service._get_codex_thread_id(
            "alice",
            "project-1",
            agent_profile="freezone:main",
            canvas_id="canvas-1",
            project_state_dir=tmp_path,
        )
        is None
    )
    assert (
        chat_service._get_codex_thread_id(
            "alice",
            "project-1",
            agent_profile="freezone:other",
            canvas_id="canvas-1",
            project_state_dir=tmp_path,
        )
        == "thread-other"
    )
    assert selected_key not in chat_service._load_active_codex_turns("alice")


@pytest.mark.asyncio
async def test_archive_codex_scope_thread_preserves_newer_active_turn(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        chat_service,
        "_active_codex_turns_path",
        lambda _username: tmp_path / "active_codex_turns.json",
    )
    scope_key = chat_service._codex_scope_key(
        "project-1", agent_profile="freezone:main", canvas_id="canvas-1"
    )
    chat_service._set_codex_thread_id(
        "alice",
        "project-1",
        "thread-old",
        agent_profile="freezone:main",
        canvas_id="canvas-1",
        project_state_dir=tmp_path,
    )

    def archive(_operation, _thread_id, _turn_id=None):
        chat_service._set_codex_thread_id(
            "alice",
            "project-1",
            "thread-new",
            agent_profile="freezone:main",
            canvas_id="canvas-1",
            project_state_dir=tmp_path,
        )
        chat_service._set_active_codex_turn(
            "alice", scope_key, ("thread-new", "turn-new")
        )
        return True

    monkeypatch.setattr(chat_service, "_control_codex_thread", archive)

    assert (
        await chat_service.archive_codex_scope_thread(
            "alice",
            "project-1",
            agent_profile="freezone:main",
            canvas_id="canvas-1",
            project_state_dir=tmp_path,
        )
        == 1
    )
    assert chat_service._load_active_codex_turns("alice")[scope_key] == {
        "thread_id": "thread-new",
        "turn_id": "turn-new",
    }
    assert (
        chat_service._get_codex_thread_id(
            "alice",
            "project-1",
            agent_profile="freezone:main",
            canvas_id="canvas-1",
            project_state_dir=tmp_path,
        )
        == "thread-new"
    )


@pytest.mark.asyncio
async def test_hermes_clear_detaches_cold_canvas_session_only(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(chat_service, "get_chat_backend_name", lambda: "hermes")
    monkeypatch.setenv("ST_EDITION", "ce")
    monkeypatch.delenv("ST_CONTROL_PLANE_DSN", raising=False)
    monkeypatch.setattr(
        chat_service, "_chat_run_lock_path", lambda *_: tmp_path / "chat.lock"
    )

    async def resolve(*, user, project_id, required_role):
        assert required_role == "editor"
        return SimpleNamespace(
            state_dir=tmp_path,
            project_name="project-1",
            project_id="project-1",
            requester_user_id="alice",
            requester_username="alice",
            requester_principals=(),
            is_home_node=True,
        )

    monkeypatch.setattr(chat_routes, "resolve_project_context", resolve)
    first = ChatScope(
        kind="project", id="project-1", surface="freezone", canvas_id="canvas-1",
        agent_id="main", state_dir=str(tmp_path),
    )
    other = ChatScope(
        kind="project", id="project-1", surface="freezone", canvas_id="canvas-2",
        agent_id="main", state_dir=str(tmp_path),
    )
    store = chat_routes.chat_store
    await store.append_message_async("alice", first, "user", "old")
    await store.append_message_async("alice", other, "user", "keep")
    index = tmp_path / "agents/hermes/freezone/dramaclaw_sessions.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    selected = '["freezone:main","project","project-1","canvas-1"]'
    sibling = '["freezone:main","project","project-1","canvas-2"]'
    index.write_text(json.dumps({selected: "old-session", sibling: "keep-session"}))

    result = await chat_routes.clear_chat_scope(
        chat_routes.ClearChatRequest(scope=first.to_dict()), user={"username": "alice"}
    )

    assert result == {"ok": True, "data": {"cleared_messages": 1, "archived_threads": 0}}
    assert await store.list_messages_async("alice", first) == []
    assert len(await store.list_messages_async("alice", other)) == 1
    assert json.loads(index.read_text()) == {sibling: "keep-session"}


@pytest.fixture
def hermes_clear_project(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_backend_name", lambda: "hermes")
    monkeypatch.setenv("ST_EDITION", "ce")
    monkeypatch.delenv("ST_CONTROL_PLANE_DSN", raising=False)
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path))
    return ProjectContext(
        project_id="project-1",
        project_name="project-1",
        owner_type="user",
        owner_id="alice",
        owner_username="alice",
        requester_user_id="alice",
        requester_username="alice",
        requester_principals=(("user", "alice"),),
        effective_role="editor",
        home_node_id="node-1",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_fields, user_fields, project_fields, status, expected_detail",
    [
        pytest.param(
            {},
            {},
            {"is_home_node": False},
            409,
            {
                "code": "project_not_on_this_node",
                "project_id": "project-1",
                "home_node_id": "node-1",
            },
            id="non-home-node",
        ),
        pytest.param(
            {},
            {},
            {"project_id": "project-2"},
            400,
            "project scope mismatch",
            id="project-mismatch",
        ),
        pytest.param(
            {},
            {"credential_kind": "agent_session"},
            None,
            403,
            "agent sessions cannot clear chat history",
            id="agent-session",
        ),
        pytest.param(
            {"canvasId": "invalid.canvas"},
            {},
            None,
            400,
            "invalid canvas id",
            id="invalid-canvas",
        ),
    ],
)
async def test_hermes_clear_route_rejects_before_acquiring_lease(
    monkeypatch,
    hermes_clear_project,
    scope_fields,
    user_fields,
    project_fields,
    status,
    expected_detail,
):
    async def resolve(*, user, project_id, required_role):
        if project_fields is None:
            pytest.fail("request must be rejected before project lookup")
        assert project_id == "project-1"
        assert required_role == "editor"
        return replace(hermes_clear_project, **project_fields)

    monkeypatch.setattr(chat_routes, "resolve_project_context", resolve)
    monkeypatch.setattr(
        chat_service,
        "_acquire_chat_run_lock",
        lambda *_: pytest.fail("rejected clear must not acquire a lease"),
    )

    with pytest.raises(HTTPException) as error:
        await chat_routes.clear_chat_scope(
            chat_routes.ClearChatRequest(
                scope={
                    "kind": "project",
                    "id": "project-1",
                    "surface": "freezone",
                    **scope_fields,
                }
            ),
            user={"username": "alice", **user_fields},
        )

    assert error.value.status_code == status
    if isinstance(expected_detail, dict):
        assert isinstance(error.value.detail, dict)
        assert {key: error.value.detail[key] for key in expected_detail} == expected_detail
    else:
        assert error.value.detail == expected_detail


@pytest.mark.asyncio
async def test_hermes_clear_route_implicit_and_explicit_default_share_lease(
    monkeypatch, hermes_clear_project,
):
    from novelvideo.chat.hermes_pool import pool as hermes_pool

    async def resolve(*, user, project_id, required_role):
        return hermes_clear_project

    async def unexpected_detach(*_args, **_kwargs):
        pytest.fail("clear must not detach a session while its lease is held")

    monkeypatch.setattr(chat_routes, "resolve_project_context", resolve)
    monkeypatch.setattr(hermes_pool, "clear_scope", unexpected_detach)
    implicit = {"kind": "project", "id": "project-1", "surface": "freezone"}
    explicit = {**implicit, "canvasId": "default"}
    lease_key = "freezone:project-1:canvas:default:agent:main"
    lease_id = chat_service._acquire_chat_run_lock("alice", lease_key)
    try:
        for scope in (implicit, explicit):
            with pytest.raises(HTTPException) as error:
                await chat_routes.clear_chat_scope(
                    chat_routes.ClearChatRequest(scope=scope),
                    user={"username": "alice"},
                )
            assert error.value.status_code == 409
            assert error.value.detail == "当前对话仍在进行，请等待完成或先停止后再清空"
    finally:
        chat_service._release_chat_run_lock("alice", lease_key, lease_id)


@pytest.mark.asyncio
async def test_clear_route_rejects_active_turn_before_mutating(monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_backend_name", lambda: "codex")

    def locked(*_args):
        raise RuntimeError("busy")

    monkeypatch.setattr(chat_service, "_acquire_chat_run_lock", locked)
    called = False

    async def archive(*_args, **_kwargs):
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(chat_service, "archive_codex_scope_thread", archive)
    with pytest.raises(HTTPException) as error:
        await chat_routes.clear_chat_scope(
            chat_routes.ClearChatRequest(scope={"kind": "home"}),
            user={"username": "alice"},
        )
    assert error.value.status_code == 409
    assert not called


@pytest.mark.asyncio
async def test_clear_route_requires_editor_before_mutating(monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_backend_name", lambda: "codex")
    roles = []

    async def resolve(*, user, project_id, required_role):
        roles.append(required_role)
        raise HTTPException(403, "forbidden")

    monkeypatch.setattr(chat_routes, "resolve_project_context", resolve)
    monkeypatch.setattr(
        chat_service, "_acquire_chat_run_lock", lambda *_: pytest.fail("mutated")
    )
    with pytest.raises(HTTPException) as error:
        await chat_routes.clear_chat_scope(
            chat_routes.ClearChatRequest(scope={"kind": "project", "id": "project-1"}),
            user={"username": "alice"},
        )
    assert error.value.status_code == 403
    assert roles == ["editor"]


@pytest.mark.asyncio
async def test_clear_route_holds_freezone_lock_until_clear_finishes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(chat_service, "get_chat_backend_name", lambda: "codex")

    async def resolve(*, user, project_id, required_role):
        assert required_role == "editor"
        return SimpleNamespace(
            state_dir=tmp_path,
            project_name="project-1",
            project_id="project-1",
            requester_user_id="alice",
            requester_username="alice",
            requester_principals=(),
        )

    monkeypatch.setattr(chat_routes, "resolve_project_context", resolve)
    acquired = []
    released = []
    monkeypatch.setattr(
        chat_service,
        "_acquire_chat_run_lock",
        lambda username, project: acquired.append((username, project)) or "lock-1",
    )
    monkeypatch.setattr(
        chat_service,
        "_release_chat_run_lock",
        lambda username, project, lock_id: released.append(
            (username, project, lock_id)
        ),
    )
    monkeypatch.setattr(chat_service, "_heartbeat_chat_run_lock", lambda *_: True)

    async def archive(*_args, **_kwargs):
        assert acquired == [("alice", "freezone:project-1:canvas:canvas-1:agent:main")]
        assert released == []
        return 1

    monkeypatch.setattr(chat_service, "archive_codex_scope_thread", archive)

    async def clear(username, scope):
        assert acquired == [("alice", "freezone:project-1:canvas:canvas-1:agent:main")]
        assert released == []
        return 2

    monkeypatch.setattr(chat_routes.chat_store, "clear_messages_async", clear)
    result = await chat_routes.clear_chat_scope(
        chat_routes.ClearChatRequest(
            scope={
                "kind": "project",
                "id": "project-1",
                "surface": "freezone",
                "canvasId": "canvas-1",
                "agentId": "main",
            }
        ),
        user={"username": "alice"},
    )
    assert result["data"]["cleared_messages"] == 2
    assert result["data"]["archived_threads"] == 1
    assert released == [("alice", acquired[0][1], "lock-1")]


@pytest.mark.asyncio
async def test_clear_route_does_not_delete_messages_after_losing_lock(monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_backend_name", lambda: "codex")
    monkeypatch.setattr(chat_service, "_acquire_chat_run_lock", lambda *_: "lock-1")
    monkeypatch.setattr(chat_service, "_release_chat_run_lock", lambda *_: None)
    monkeypatch.setattr(chat_service, "_heartbeat_chat_run_lock", lambda *_: False)

    async def archive(*_args, **_kwargs):
        return 1

    async def clear(*_args, **_kwargs):
        pytest.fail("messages must not be cleared after lock ownership is lost")

    monkeypatch.setattr(chat_service, "archive_codex_scope_thread", archive)
    monkeypatch.setattr(chat_routes.chat_store, "clear_messages_async", clear)

    with pytest.raises(HTTPException) as error:
        await chat_routes.clear_chat_scope(
            chat_routes.ClearChatRequest(scope={"kind": "home"}),
            user={"username": "alice"},
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_payload",
    [
        {"kind": "project", "id": "project-1", "surface": "freezone"},
        {"kind": "freezone", "id": "project-1"},
    ],
)
async def test_clear_route_resets_default_canvas_thread(
    tmp_path, monkeypatch, scope_payload
):
    monkeypatch.setattr(chat_service, "get_chat_backend_name", lambda: "codex")
    monkeypatch.setattr(
        chat_service,
        "_active_codex_turns_path",
        lambda _username: tmp_path / "active_codex_turns.json",
    )

    async def resolve(*, user, project_id, required_role):
        assert required_role == "editor"
        return SimpleNamespace(
            state_dir=tmp_path,
            project_name="project-1",
            project_id="project-1",
            requester_user_id="alice",
            requester_username="alice",
            requester_principals=(),
        )

    monkeypatch.setattr(chat_routes, "resolve_project_context", resolve)
    monkeypatch.setattr(chat_service, "_acquire_chat_run_lock", lambda *_: "lock-1")
    monkeypatch.setattr(chat_service, "_release_chat_run_lock", lambda *_: None)
    monkeypatch.setattr(chat_service, "_heartbeat_chat_run_lock", lambda *_: True)
    monkeypatch.setattr(chat_service, "_control_codex_thread", lambda *_args: True)

    async def clear(_username, _scope):
        return 1

    monkeypatch.setattr(chat_routes.chat_store, "clear_messages_async", clear)
    chat_service._set_codex_thread_id(
        "alice",
        "project-1",
        "old-thread",
        agent_profile="freezone:main",
        canvas_id="default",
        project_state_dir=tmp_path,
    )

    await chat_routes.clear_chat_scope(
        chat_routes.ClearChatRequest(scope=scope_payload),
        user={"username": "alice"},
    )

    assert (
        chat_service._get_codex_thread_id(
            "alice",
            "project-1",
            agent_profile="freezone:main",
            canvas_id="default",
            project_state_dir=tmp_path,
        )
        is None
    )

import base64
import io
import zipfile
from types import SimpleNamespace

import pytest

from novelvideo.freezone import agent_config_store
from novelvideo.freezone.native_skill_store import (
    delete_user_native_skill,
    get_user_native_skill,
    install_native_skill,
    list_user_native_skills,
    parse_native_skill_package,
    read_user_native_skill_file,
)


def _zip_b64(entries: dict[str, bytes | str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return base64.b64encode(buf.getvalue()).decode()


def test_native_skill_install_preserves_nested_zip_losslessly(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_config_store, "OUTPUT_DIR", tmp_path)
    skill_md = (
        "---\n"
        "name: yd-cinematic-storyboard\n"
        "description: Complex lossless storyboard skill\n"
        "---\n\n"
        "# YD分镜skills\n\n保留完整细节与确认卡。\n"
    ).encode("utf-8")
    acting_md = "# Acting\n七层戏剧驱动细节。\n".encode("utf-8")
    preview_html = "<html><body>preview</body></html>".encode("utf-8")
    encoded = _zip_b64({
        "outer/inner/SKILL.md": skill_md,
        "outer/inner/references/acting.md": acting_md,
        "outer/inner/assets/preview.html": preview_html,
        "outer/inner/scripts/render_preview.py": b"print('hi')",
    })

    manifest = install_native_skill(username="alice", name="yd.zip", content_base64=encoded)
    assert manifest["id"] == "yd-cinematic-storyboard"
    assert manifest["file_count"] == 4
    assert any("scripts/render_preview.py" in w for w in manifest["warnings"])

    installed_dir = agent_config_store.OUTPUT_DIR / "alice" / "_account" / "freezone" / "native_skills" / "yd-cinematic-storyboard"
    assert (installed_dir / "SKILL.md").read_bytes() == skill_md
    assert (installed_dir / "references" / "acting.md").read_bytes() == acting_md
    assert (installed_dir / "assets" / "preview.html").read_bytes() == preview_html
    assert len(list_user_native_skills("alice")) == 1
    assert get_user_native_skill("alice", "yd-cinematic-storyboard")["id"] == "yd-cinematic-storyboard"
    read_ref = read_user_native_skill_file(
        username="alice",
        skill_id="yd-cinematic-storyboard",
        file_path="references/acting.md",
    )
    assert read_ref["content"] == acting_md.decode("utf-8")
    with pytest.raises(ValueError, match="invalid native skill file path"):
        read_user_native_skill_file(
            username="alice",
            skill_id="yd-cinematic-storyboard",
            file_path="../secret.txt",
        )

    assert delete_user_native_skill(username="alice", skill_id="yd-cinematic-storyboard") is True
    assert list_user_native_skills("alice") == []


def test_native_skill_rejects_traversal_and_synthesizes_frontmatter_when_missing():
    bad = _zip_b64({"../SKILL.md": "bad"})
    with pytest.raises(ValueError, match="Unsafe"):
        parse_native_skill_package("bad.zip", bad)

    plain_md = base64.b64encode("# 写实场景\n保持实拍质感。\n".encode("utf-8")).decode()
    parsed = parse_native_skill_package("realist-scene.md", plain_md)
    assert parsed["skill_id"] == "realist-scene"
    skill_bytes = dict(parsed["files"])["SKILL.md"].decode("utf-8")
    assert skill_bytes.startswith("---\n")
    assert "name: realist-scene" in skill_bytes
    assert "保持实拍质感。" in skill_bytes


def test_hermes_workspace_materializes_and_cleans_up_native_skills(tmp_path, monkeypatch):
    from novelvideo.chat import hermes_workspace

    monkeypatch.setattr(agent_config_store, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(hermes_workspace, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(hermes_workspace, "_materialize_skill_links", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_workspace, "_materialize_plugin_links", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_workspace, "_sync_freezone_workflow_skills", lambda *a, **kw: None)

    encoded = _zip_b64({
        "pkg/SKILL.md": "---\nname: yd-storyboard\ndescription: YD\n---\n\n# YD\n",
        "pkg/references/acting.md": "Full acting guide",
    })
    install_native_skill(username="alice", name="yd.zip", content_base64=encoded)

    home = hermes_workspace.ensure_user_hermes_workspace("alice", profile="freezone")
    assert (home / "skills" / "yd-storyboard" / "SKILL.md").is_file()
    assert (home / "skills" / "yd-storyboard" / "references" / "acting.md").read_text(encoding="utf-8") == "Full acting guide"

    listed = hermes_workspace.list_freezone_hermes_workflow_skills("alice")
    assert [item["id"] for item in listed] == ["yd-storyboard"]
    assert listed[0]["source"] == "hermes_native_markdown_skill"

    delete_user_native_skill(username="alice", skill_id="yd-storyboard")
    hermes_workspace.sync_freezone_hermes_workflow_skills("alice")
    assert not (home / "skills" / "yd-storyboard").exists()
    assert hermes_workspace.list_freezone_hermes_workflow_skills("alice") == []


def test_chat_service_injects_native_skill_instruction_only_when_mentioned(tmp_path, monkeypatch):
    from novelvideo.chat import service as chat_service

    monkeypatch.setattr(agent_config_store, "OUTPUT_DIR", tmp_path)
    encoded = _zip_b64({
        "pkg/SKILL.md": "---\nname: yd-storyboard\ndescription: YD\n---\n\n# YD\n\nFollow gate.md first.\n",
        "pkg/references/gate.md": "Confirmation gate",
    })
    install_native_skill(username="alice", name="yd.zip", content_base64=encoded)

    assert chat_service._freezone_native_skill_context("alice", "普通分镜需求") == ""
    hint = chat_service._freezone_native_skill_context("alice", "/yd-storyboard 帮我做分镜")
    assert "[FREEZONE_NATIVE_MARKDOWN_SKILL]" in hint
    assert "Follow gate.md first." in hint
    assert "references/gate.md" in hint
    assert 'freezone_read_native_skill_file(skill_id="yd-storyboard"' in hint


@pytest.mark.asyncio
async def test_api_native_submit(tmp_path, monkeypatch):
    from novelvideo.api.routes import freezone as freezone_routes
    from novelvideo.api.routes import skill_imports as import_routes
    from novelvideo.chat import hermes_workspace

    monkeypatch.setattr(agent_config_store, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(hermes_workspace, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(hermes_workspace, "_materialize_skill_links", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_workspace, "_materialize_plugin_links", lambda *a, **kw: None)

    async def scope(project, user):
        return SimpleNamespace(), tmp_path / "state", user["username"]

    monkeypatch.setattr(import_routes, "scope", scope)
    monkeypatch.setattr(
        import_routes,
        "get_task_backend",
        lambda: (_ for _ in ()).throw(AssertionError("native mode must not enqueue a conversion job")),
    )

    encoded = _zip_b64({
        "pkg/SKILL.md": "---\nname: yd-storyboard\ndescription: YD\n---\n\n# YD\n",
        "pkg/references/gate.md": "Confirmation gate",
    })
    res = await import_routes.submit(
        "p",
        import_routes.ImportRequest(
            files=[import_routes.SourceFile(name="yd.zip", content_base64=encoded)],
            mode="native",
        ),
        {"username": "alice"},
    )
    assert res["ok"] is True
    item = res["data"]["items"][0]
    assert item["mode"] == "native"
    assert item["status"] == "installed"
    assert item["native_skill"]["id"] == "yd-storyboard"
    assert item["native_skill"]["file_count"] == 2

    listed = await freezone_routes.list_freezone_native_skills({"username": "alice"})
    assert [entry["id"] for entry in listed["data"]] == ["yd-storyboard"]

    file_res = await freezone_routes.read_freezone_native_skill_file_route(
        "yd-storyboard", path="references/gate.md", user={"username": "alice"}
    )
    assert file_res["data"]["content"] == "Confirmation gate"

    removed = await freezone_routes.remove_freezone_native_skill("yd-storyboard", {"username": "alice"})
    assert removed["data"]["deleted"] is True
    listed_after = await freezone_routes.list_freezone_native_skills({"username": "alice"})
    assert listed_after["data"] == []

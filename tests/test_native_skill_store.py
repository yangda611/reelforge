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


def test_chat_service_injects_workflow_skill_selection_for_slash_mention(tmp_path, monkeypatch):
    """用户从输入框选中 Workflow Skill（不是 native markdown）时也必须注入指令。

    线上症状：chip 只序列化成字面量 ``/yd-cinematic-storyboard``，后端只查 native
    markdown 技能表，匹配不上就注入空字符串；模型既不知道技能已被选中，也没有
    理由调 ``freezone_get_workflow_skill``，最后回「我这里没有 skill_view 工具」
    然后什么都不做。这里把 Workflow Skill 的命中与注入钉住。
    """
    from novelvideo.chat import service as chat_service

    monkeypatch.setattr(agent_config_store, "OUTPUT_DIR", tmp_path)
    agent_config_store.save_user_agent_config_item(
        username="alice",
        kind="recipes",
        payload={
            "schema_version": "dramaclaw.recipe.v1",
            "id": "yd-recipe",
            "name": "YD Recipe",
            "version": "1.0.0",
            "output_kind": "text",
            "action_keys": ["yd-recipe"],
            "system_prompt": "prompt",
            "planning_prompt": "planning",
            "result_summary": "result",
        },
    )
    agent_config_store.save_user_agent_config_item(
        username="alice",
        kind="skills",
        payload={
            "schema_version": "dramaclaw.workflow-skill.v1",
            "id": "yd-cinematic-storyboard",
            "name": "YD分镜skills",
            "version": "1.0.0",
            "enabled": True,
            "description": "把剧本拆成多个 clip",
            "category": "storyboard",
            "triggers": {"keywords": ["分镜"], "node_scopes": ["textGeneration"]},
            "allowed_recipe_ids": ["yd-recipe"],
            "planning": {
                "planning_notes": "根据用户目标动态规划。",
                "prompt_guide": "",
                "conduct_rules": ["仅创建本次计划需要的节点。"],
            },
            "evaluation": {
                "rating_bands": [{"score": 10, "description": "结果完整可用"}],
                "quality_threshold": 7,
                "domain_constraints": ["遵循用户目标"],
            },
        },
    )

    # 没提到技能：不注入，避免日常对话被技能规则污染。
    assert chat_service._freezone_native_skill_context("alice", "普通分镜需求") == ""

    hint = chat_service._freezone_native_skill_context(
        "alice", "/yd-cinematic-storyboard 使用这个skills将这个剧本拆成多个clip"
    )
    assert "[FREEZONE_WORKFLOW_SKILL_SELECTED]" in hint
    assert "yd-cinematic-storyboard" in hint
    assert 'freezone_get_workflow_skill(skill_id="yd-cinematic-storyboard")' in hint
    # 明确告诉模型别再去找 skill_view（那个工具在本 profile 里不存在）。
    assert "Do not call `skill_view` or `skills_list`" in hint
    assert "Do NOT re-route" in hint
    # 技能正文由工具返回，不在注入块里复制（避免与技能自身版本漂移）。
    assert "# YD分镜skills" not in hint

    # 未知 id 不应误命中。
    assert chat_service._freezone_native_skill_context("alice", "/not-a-skill 做点事") == ""


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

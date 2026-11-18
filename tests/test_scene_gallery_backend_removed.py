from pathlib import Path


def test_dead_grid_preview_backend_flow_is_removed() -> None:
    generation_source = Path("src/novelvideo/api/routes/generation.py").read_text(
        encoding="utf-8"
    )
    schemas_source = Path("src/novelvideo/api/schemas.py").read_text(encoding="utf-8")
    task_identity_source = Path("src/novelvideo/task_identity.py").read_text(
        encoding="utf-8"
    )

    assert '"/projects/{project}/episodes/{episode_num}/grids/generate"' not in generation_source
    assert "GridGenerateRequest" not in generation_source
    assert "GridGenerateRequest" not in schemas_source
    assert not Path("src/novelvideo/ray_tasks.py").exists()
    assert '"grid_preview"' not in task_identity_source

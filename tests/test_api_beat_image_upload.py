from __future__ import annotations

import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _client(monkeypatch, tmp_path):
    from novelvideo.api.routes import generation
    from novelvideo.api.deps import ProjectResolution

    async def fake_resolve_project_scope(project, user, *, required_role="viewer"):
        return ProjectResolution(
            ctx=None,
            username="alice",
            project_name=project,
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    monkeypatch.setattr(generation, "resolve_project_scope", fake_resolve_project_scope)

    def fake_static_url(ctx, relative_path, local_path=None):
        return f"/files/{relative_path}"

    monkeypatch.setattr(generation, "make_static_url_for_context", fake_static_url)

    app = FastAPI()
    app.include_router(generation.router, prefix="/api/v1")
    app.dependency_overrides[generation.get_api_user] = lambda: {"username": "alice"}
    return TestClient(app)


def test_upload_beat_render_writes_user_supplied_first_frame(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/projects/demo/episodes/2/beats/3/render/upload",
        files={"file": ("render.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["beat_num"] == 3
    assert body["data"]["frame_url"] == "/files/frames/ep002/beat_03.png"
    assert "pool_id" not in body["data"]
    assert (tmp_path / "frames" / "ep002" / "beat_03.png").exists()
    assert not (tmp_path / "grids").exists()


def test_upload_beat_render_rejects_non_image(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/projects/demo/episodes/2/beats/3/render/upload",
        files={"file": ("render.png", b"not an image", "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert not (tmp_path / "frames").exists()

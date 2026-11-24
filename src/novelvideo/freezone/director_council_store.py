"""Canvas-scoped source snapshots and immutable council preview records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from novelvideo.freezone.director_council import (
    MAX_SOURCE_CHARS,
    CouncilCompilation,
    CouncilModel,
    CouncilPreview,
    Kind,
    SceneAsset,
    SceneBrief,
)
from novelvideo.freezone.paths import freezone_root
from novelvideo.utils.state_index_files import write_json_atomic

NodeId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
PreviewId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class CouncilPreviewRequest(CouncilModel):
    action: Literal["preview"]
    request: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="", max_length=MAX_SOURCE_CHARS)
    node_ids: list[NodeId] = Field(default_factory=list, max_length=12)
    observations: dict[NodeId, str] = Field(default_factory=dict, max_length=12)
    locked_dialogue: list[str] | None = Field(default=None, max_length=60)
    locked_facts: list[str] = Field(default_factory=list, max_length=30)
    segment_max_ms: int | None = Field(default=None, gt=0, le=600_000)
    total_ms: int | None = Field(default=None, gt=0, le=1_800_000)
    deliverables: list[Kind] = Field(default_factory=lambda: ["video"], min_length=1, max_length=4)
    conditions_confirmed: bool = False


class CouncilCompileRequest(CouncilModel):
    action: Literal["compile"]
    preview_id: PreviewId
    prompts_authorized: Literal[True]


class CouncilRecord(CouncilModel):
    source_hash: str
    scene: SceneBrief
    preview: CouncilPreview
    compilation: CouncilCompilation | None = None


class SourceNodeMissing(ValueError):
    pass


class SourceTooLong(ValueError):
    pass


_SCRIPT_ROW_FIELDS = (
    "shot_no", "duration", "visual_description", "character_1", "character_description_1",
    "character_2", "character_description_2", "shot", "character_action", "emotion",
    "scene_tags", "lighting_mood", "sound", "dialogue",
)


def _node_source(kind: str, data: dict) -> str:
    script = data.get("scriptResult")
    if kind == "scriptNode" and isinstance(script, dict) and isinstance(script.get("rows"), list):
        rows = [
            {field: row[field] for field in _SCRIPT_ROW_FIELDS
             if field in row and isinstance(row[field], (str, int)) and row[field] != ""}
            for row in script["rows"] if isinstance(row, dict)
        ]
        if rows:
            return json.dumps({"title": str(script.get("title") or ""), "rows": rows}, ensure_ascii=False)
    return next((
        data[field] for field in ("content", "text", "prompt")
        if isinstance(data.get(field), str) and data[field].strip()
    ), "")


def scene_from_canvas(
    canvas: dict, request: CouncilPreviewRequest
) -> tuple[SceneBrief, str]:
    wanted = list(dict.fromkeys(request.node_ids))
    if set(request.observations) - set(wanted):
        raise SourceNodeMissing("observations must refer to selected canvas nodes")
    nodes = {
        node.get("id"): node for node in (canvas.get("nodes") or [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    assets = []
    snapshot = []
    source_chars = len(request.source)
    for node_id in wanted:
        node = nodes.get(node_id)
        if node is None:
            raise SourceNodeMissing("a selected source node is no longer on this canvas")
        data = node.get("data") or {}
        kind = str(node.get("type") or "node")[:128]
        title = str(data.get("displayName") or data.get("title") or node_id).strip()
        content = _node_source(kind, data)
        source_chars += len(content)
        if source_chars > MAX_SOURCE_CHARS:
            raise SourceTooLong(
                f"Source exceeds {MAX_SOURCE_CHARS} characters; select one scene or use the relevant excerpt."
            )
        assets.append(SceneAsset(
            node_id=node_id, kind=kind, label=title[:1600] or node_id,
            content=content, observation=request.observations.get(node_id, "")[:1600],
        ))
        # Detect changed assets, text, and upstream bindings without persisting URLs
        # or treating graph layout edits as source changes.
        snapshot.append((node_id, kind, data))
    links = [
        [edge.get("source"), edge.get("target"), edge.get("sourceHandle"),
         edge.get("targetHandle"), edge.get("data")]
        for edge in (canvas.get("edges") or []) if isinstance(edge, dict)
        and (edge.get("source") in wanted or edge.get("target") in wanted)
    ]
    snapshot_json = json.dumps([snapshot, links], ensure_ascii=False, sort_keys=True)
    scene = SceneBrief(
        **request.model_dump(exclude={"action", "node_ids", "conditions_confirmed", "observations"}),
        assets=assets,
    )
    return scene, hashlib.sha256(snapshot_json.encode()).hexdigest()


def record_path(state_dir: Path, canvas_id: str, actor: str, preview_id: str) -> Path:
    owner = hashlib.sha256(actor.encode()).hexdigest()
    return freezone_root(state_dir) / "director_council" / canvas_id / owner / f"{preview_id}.json"


def read_record(path: Path) -> CouncilRecord | None:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    return CouncilRecord.model_validate_json(content)


def save_record(path: Path, record: CouncilRecord) -> None:
    write_json_atomic(path, record.model_dump(mode="json"))

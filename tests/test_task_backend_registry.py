import ast
from pathlib import Path


def test_importing_runners_registers_builtin_project_task_runners():
    import novelvideo.task_backend.runners  # noqa: F401
    from novelvideo.task_backend.registry import (
        get_project_task_runner,
        registered_project_task_types,
    )

    names = set(registered_project_task_types())

    assert len(names) >= 25
    for task_type in {
        "single_video",
        "audio_generation_indextts2",
        "system_voice_setup",
        "voice_design",
        "build_scenes",
    }:
        assert task_type in names
        assert get_project_task_runner(task_type) is not None

    for removed_task_type in {
        "batch_render",
        "video_generation",
        "render_plan",
        "sketch_generation",
        "sketch_grid_generation",
        "sketch_regen",
        "sketch_edit_execute",
        "director_control_to_sketch",
        "mainline_sketch_from_context",
        "mainline_frame_from_context",
        "mainline_director_control_sketch",
        "selected_regen",
        "grid_regenerate",
        "global_optimize_video",
        "beat_video_prompt",
    }:
        assert removed_task_type not in names
        assert get_project_task_runner(removed_task_type) is None


def test_every_literal_enqueued_project_task_has_registered_runner():
    import novelvideo.task_backend.runners  # noqa: F401
    from novelvideo.task_backend.registry import registered_project_task_types

    route_dir = Path("src/novelvideo/api/routes")
    enqueued: set[str] = set()
    for path in route_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "enqueue_project_task":
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "task_type"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    enqueued.add(keyword.value.value)

    missing = enqueued - set(registered_project_task_types())
    assert missing == set()


def test_voice_design_requires_home_node():
    from novelvideo.task_backend.registry import project_task_requires_home_node
    assert project_task_requires_home_node("voice_design")

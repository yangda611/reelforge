"""Prompt text may only name canvas commands that the write tool really accepts.

The canvas write contract is ``_COMMAND_TYPES`` in the Freezone plugin, and the
tool's JSON Schema mirrors it. Prompt text that names a command outside that set
is worse than missing guidance: the Agent follows it, the request fails schema
validation before any handler runs (``phase=tool_validation``,
``retryable=false``), and the turn gives up. That is exactly how
``connect_nodes`` — a name that never existed, where the contract says
``create_edge`` — produced a user-facing ``tool_arguments_invalid``.

So the vocabulary is checked mechanically: every command-shaped token in the
prompts must be a real command type.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

from novelvideo.chat import service

ROOT = Path(__file__).resolve().parents[1]

# A command-shaped word: an authoring verb followed by the canvas object it acts
# on. Deliberately narrow so unrelated identifiers (run_after_create,
# link_type, create_task) are not mistaken for commands.
_COMMAND_TOKEN_RE = re.compile(
    r"\b(?:create|connect|link|delete|remove|add|move|group|layout|select|"
    r"clear|update|open)_(?:node|nodes|edge|edges|canvas|mainline_projection)\b"
)

_PROMPT_FILES = (
    ROOT / "src" / "novelvideo" / "chat" / "service.py",
    ROOT / "src" / "novelvideo" / "chat" / "tool_policy.py",
    ROOT / "src" / "novelvideo" / "chat" / "hermes_workspace.py",
)

_SKILL_ROOTS = (
    ROOT / ".hermes" / "skills",
)


@pytest.fixture(autouse=True)
def _restore_tools_registry_modules():
    sentinel = object()
    previous = {
        name: sys.modules.get(name, sentinel) for name in ("tools", "tools.registry")
    }
    yield
    for name, value in previous.items():
        if value is sentinel:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value


def _real_command_types() -> set[str]:
    tools_module = types.ModuleType("tools")
    registry_module = types.ModuleType("tools.registry")
    registry_module.tool_error = lambda value: value
    registry_module.tool_result = lambda value: value
    sys.modules["tools"] = tools_module
    sys.modules["tools.registry"] = registry_module

    path = ROOT / ".hermes" / "plugins" / "freezone" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "test_canvas_command_vocabulary_plugin", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module._COMMAND_TYPES)


def _prompt_sources() -> list[Path]:
    sources = [path for path in _PROMPT_FILES if path.exists()]
    for root in _SKILL_ROOTS:
        if root.exists():
            sources.extend(sorted(root.rglob("*.md")))
    return sources


def test_every_named_canvas_command_exists() -> None:
    valid = _real_command_types()
    # Pins the contract this test guards; a renamed command type fails loudly
    # here rather than silently widening the allow-list below.
    assert {"create_edge", "create_node", "add_next_node"} <= valid
    assert "connect_nodes" not in valid

    offenders = {}
    for path in _prompt_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        unknown = sorted(set(_COMMAND_TOKEN_RE.findall(text)) - valid)
        if unknown:
            offenders[str(path.relative_to(ROOT))] = unknown

    assert not offenders, (
        "Prompt text names canvas commands that the write tool rejects; the Agent "
        f"will follow them into a schema rejection. Use real command types only: {offenders}"
    )


def test_freezone_assistant_instructions_only_name_real_commands() -> None:
    """The Codex/Claude developer instructions are the highest-traffic prompt."""
    valid = _real_command_types()
    instructions = service._FREEZONE_CANVAS_ASSISTANT_INSTRUCTIONS
    unknown = sorted(set(_COMMAND_TOKEN_RE.findall(instructions)) - valid)
    assert not unknown, unknown

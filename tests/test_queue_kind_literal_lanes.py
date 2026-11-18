"""Every ``queue_kind`` literal in the tree must name a lane that exists.

TCP-P55: an endpoint once asked for a lane called ``"sketch"``, which
``QUEUE_KINDS`` has never contained. While ``normalize_queue_kind`` silently
fell back to ``default`` that was invisible; once D1 made it throw
(``task_backend/queues.py:19-20``) the endpoint became a live 500. This ratchet
stops any lane typo from being written again.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path


from novelvideo.task_backend.queues import QUEUE_KINDS

SIGNING_KEY = b"d" * 32
NOW = datetime(2026, 8, 14, 4, 5, 7, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# the ratchet
# --------------------------------------------------------------------------


def _value_literals(node: ast.AST) -> list[ast.Constant]:
    """String constants that can actually *become* the value of ``node``.

    Deliberately not ``ast.walk``: in
    ``queue_kind="ffmpeg" if task_type != "freezone_analyze" else "default"``
    (freezone.py:2446) the comparison operand is not a lane and walking the
    whole subtree would report it as one. Only the branches of a conditional
    and the arms of an ``or`` can end up in the argument, so only those are
    followed; anything else (a name, a call) carries no literal to check.
    """
    if isinstance(node, ast.Constant):
        return [node] if isinstance(node.value, str) else []
    if isinstance(node, ast.IfExp):
        return _value_literals(node.body) + _value_literals(node.orelse)
    if isinstance(node, ast.BoolOp):
        return [lit for value in node.values for lit in _value_literals(value)]
    return []


def _queue_kind_literals(tree: ast.AST) -> list[tuple[str, int]]:
    """Collect ``queue_kind=<str literal>`` call keywords and parameter defaults."""
    found: list[tuple[str, int]] = []

    def collect(node: ast.AST) -> None:
        found.extend((lit.value, lit.lineno) for lit in _value_literals(node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "queue_kind":
                    collect(keyword.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            defaulted = positional[len(positional) - len(args.defaults) :]
            for arg, default in zip(defaulted, args.defaults):
                if arg.arg == "queue_kind":
                    collect(default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if default is not None and arg.arg == "queue_kind":
                    collect(default)

    return found


def test_every_queue_kind_literal_in_src_names_a_known_lane():
    """Ratchet against TCP-P55 coming back.

    Shape reused from tests/test_task_backend_registry.py:37-70
    (``test_every_literal_enqueued_project_task_has_registered_runner``),
    widened from ``api/routes`` to the whole package — the offender lived in
    ``verification/routes.py``, outside that directory.
    """
    src_root = Path("src/novelvideo")
    assert src_root.is_dir()

    offenders: list[str] = []
    scanned = 0
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for value, lineno in _queue_kind_literals(tree):
            scanned += 1
            if value not in QUEUE_KINDS:
                offenders.append(f"{path}:{lineno} queue_kind={value!r}")

    assert scanned > 40, "scanner stopped seeing call sites — it has gone blind"
    assert offenders == []

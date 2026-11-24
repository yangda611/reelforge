"""Check canvas tools with the interpreter that actually runs Hermes."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from pathlib import Path
import sys


def verify_runtime(root: Path) -> dict[str, object]:
    sys.path.insert(0, str(root / "src"))
    plugin_path = root / ".hermes/plugins/freezone/__init__.py"
    checked: list[str] = []
    errors: dict[str, str] = {}
    # Include lazy helper imports: successful plugin registration alone hides
    # failures behind its guarded imports and never exercises write preflight.
    modules = sorted({"novelvideo.freezone.workflow_plan"} | {
        node.module
        for node in ast.walk(ast.parse(plugin_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and node.module and node.module.startswith("novelvideo.")
    })
    for name in modules:
        try:
            importlib.import_module(name)
            checked.append(name)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"

    for name, path in (
        ("freezone_runtime_probe", plugin_path),
        ("story_validation_runtime_probe", root / ".hermes/plugins/dramaclaw/story_validation.py"),
    ):
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for key, value in vars(module).items():
                if key.endswith("_IMPORT_ERROR") and value is not None:
                    errors[f"{name}.{key}"] = f"{type(value).__name__}: {value}"
            checked.append(name)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    return {"ok": not errors, "python": sys.executable, "checked": checked, "errors": errors}


if __name__ == "__main__":
    result = verify_runtime(Path(__file__).resolve().parents[1])
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(0 if result["ok"] else 1)

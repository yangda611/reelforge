from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_workflow_preflight_does_not_require_api_or_video_runtime():
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import sys

class RejectBackendImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'novelvideo.api' or fullname == 'novelvideo.freezone.video_node':
            raise ImportError('worker must not load backend service: ' + fullname)

sys.meta_path.insert(0, RejectBackendImports())
from novelvideo.freezone.workflow_preflight import _RECOMMENDED_MODEL_ALIASES
assert _RECOMMENDED_MODEL_ALIASES == {
    'imageGenNode': 'newapi_gpt_image2',
    'videoNode': 'newapi_seedance-2.0-fast',
}
"""],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_docker_and_local_hermes_install_and_check_freezone_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    setup = (ROOT / "scripts/setup-hermes.sh").read_text(encoding="utf-8")
    assert "deploy/hermes-freezone-requirements.txt" in dockerfile
    assert "python3 deploy/verify_freezone_runtime.py" in dockerfile
    assert 'uv pip install --python "$HERMES_PYTHON" -r' in setup
    assert '"$HERMES_PYTHON" "$root_dir/deploy/verify_freezone_runtime.py"' in setup


@pytest.mark.parametrize("missing_jsonschema", [False, True])
def test_runtime_probe_checks_lazy_imports_and_reports_missing_dependencies(missing_jsonschema):
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import json
from pathlib import Path
import runpy
import sys
import types

class RejectImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = {'novelvideo.api', 'novelvideo.freezone.video_node'}
        if sys.argv[1] == 'True':
            blocked.add('jsonschema')
        if fullname in blocked:
            raise ModuleNotFoundError('No module named ' + repr(fullname))

sys.meta_path.insert(0, RejectImports())
# Hermes supplies the protocol adapter; this test exercises the real plugin
# and all its application helpers without installing the separate agent host.
sys.modules['tools'] = types.ModuleType('tools')
registry = types.ModuleType('tools.registry')
registry.tool_result = registry.tool_error = lambda value: value
sys.modules['tools.registry'] = registry
probe = runpy.run_path('deploy/verify_freezone_runtime.py')
report = probe['verify_runtime'](Path.cwd())
print(json.dumps(report))
""", str(missing_jsonschema)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is (not missing_jsonschema)
    if missing_jsonschema:
        assert "novelvideo.freezone.workflow_plan" in report["errors"]
        assert "jsonschema" in report["errors"]["story_validation_runtime_probe"]
    else:
        assert "novelvideo.freezone.workflow_preflight" in report["checked"]
        assert "freezone_runtime_probe" in report["checked"]
        assert not report["errors"]

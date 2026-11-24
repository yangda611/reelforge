from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _bash() -> str:
    """Resolve a bash Python can actually exec.

    ``/bin/bash`` is an MSYS path when the suite runs under Git Bash on Windows;
    Python resolves it against the current drive and gets ``FileNotFoundError``.
    """
    return shutil.which("bash") or "/bin/bash"


def test_pydantic_ai_openai_adapter_imports_with_locked_sdk():
    # Recipe text generation imports this lazily. A Hermes install used to
    # downgrade openai after uv sync, so application startup still succeeded
    # while every generate_text action failed with a generic HTTP 503.
    from pydantic_ai.models.openai import OpenAIChatModel

    assert OpenAIChatModel is not None


def test_local_ce_keeps_hermes_out_of_application_venv():
    setup_script = (ROOT / "scripts" / "setup-hermes.sh").read_text(encoding="utf-8")

    assert (
        'HERMES_PYTHON="${HERMES_PYTHON:-$HERMES_ENV_DIR/bin/python}"' in setup_script
    )
    assert 'uv pip install --python "$HERMES_PYTHON"' in setup_script


def test_fresh_hermes_install_uses_public_https_repo(tmp_path, monkeypatch):
    bash = _bash()
    for name in ("HERMES_REPO", "HERMES_REF", "HERMES_PYTHON", "HERMES_BASE_PYTHON",
                 "HERMES_CLI_PATH", "HERMES_ENV_DIR", "HERMES_SOURCE_DIR"):
        monkeypatch.delenv(name, raising=False)
    for command, body in {
        "git": 'printf "%s\\n" "$@" > "$GIT_ARGS"; exit 29',
        "python": "exit 1",
        "uv": "exit 1",
    }.items():
        executable = tmp_path / command
        executable.write_text(f"#!/bin/sh\n{body}\n")
        executable.chmod(0o755)
    git_args = tmp_path / "git-args"
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("GIT_ARGS", str(git_args))
    monkeypatch.setenv("HERMES_SOURCE_DIR", str(tmp_path / "source"))
    monkeypatch.setenv("HERMES_PYTHON", str(tmp_path / "python"))

    result = subprocess.run(
        [bash, str(ROOT / "scripts" / "setup-hermes.sh")],
        capture_output=True, text=True, timeout=5,
    )

    assert result.returncode == 29, result.stderr
    assert git_args.read_text().splitlines() == [
        "clone", "--progress", "https://github.com/dramaclaw/hermes-agent.git",
        str(tmp_path / "source"),
    ]


def test_env_example_selects_hermes_without_overriding_agent_model():
    content = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "\nDRAMACLAW_CHAT_BACKEND=hermes\n" in content
    assert "HERMES_MODEL=" not in content

"""The native launcher provisions the selected runtime before starting services."""

from pathlib import Path
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _bash() -> str:
    """Resolve a bash Python can actually exec.

    ``/bin/bash`` is an MSYS path under Git Bash on Windows; Python resolves it
    against the current drive and raises ``FileNotFoundError``, which used to
    make every case here fail before it ran the launcher at all.
    """
    return shutil.which("bash") or "/bin/bash"


def _posix(path: Path) -> str:
    """Render a host path the way the bash under test prints ``$PWD``.

    MSYS bash on Windows reports ``/c/Users/...`` where Python holds
    ``C:\\Users\\...``, so the launcher's ``$PWD``-derived paths cannot be
    compared against ``str(tmp_path)`` there. The leaf may not exist yet, so
    resolve the deepest ancestor that does and append the rest unchanged.
    """
    if os.name != "nt":
        return str(path)
    anchor, tail = path, []
    while not anchor.exists():
        anchor, tail = anchor.parent, [anchor.name, *tail]
    rendered = subprocess.run(
        [_bash(), "-c", 'cd "$1" && pwd', "_", str(anchor)],
        capture_output=True, text=True, check=True,
    )
    return "/".join([rendered.stdout.strip(), *tail])


@pytest.mark.parametrize(
    ("backend", "legacy_backend", "file_backend", "expected_backend"),
    [
        ("", "", "", "hermes"),
        (" HERMES ", "codex", "", "hermes"),
        ("", "hermes", "", "hermes"),
        ("codex", "hermes", "hermes", "codex"),
        ("", "codex", "", "codex"),
        ("", "", "codex", "codex"),
        ("invalid", "", "", None),
    ],
)
@pytest.mark.parametrize("setup_fails", [False, True])
def test_launcher_provisions_only_selected_runtime(
    tmp_path, backend, legacy_backend, file_backend, expected_backend, setup_fails
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "frontend").mkdir()
    launcher = scripts / "start-ce.sh"
    launcher.write_text((ROOT / "scripts" / "start-ce.sh").read_text())
    (tmp_path / ".env").write_text(
        'CODEX_BIN="./my runtime/codex"\nHERMES_ENV_DIR="$PWD/custom-hermes"\n'
        + (f"DRAMACLAW_CHAT_BACKEND={file_backend}\n" if file_backend else "")
    )
    for runtime in ("codex", "hermes"):
        (scripts / f"setup-{runtime}.sh").write_text(
            "#!/bin/bash\nset -eu\n"
            '[ "$ST_EDITION" = ce ] && [ -z "$ST_CONTROL_PLANE_DSN" ]\n'
            f'printf "%s\\n" {runtime} >> "$SETUP_RESULT"\n'
            + (
                '[ "$CODEX_BIN" = "./my runtime/codex" ]\n'
                if runtime == "codex"
                else '[ "$HERMES_CLI_PATH" = "$PWD/custom-hermes/bin/hermes" ]\n'
            )
            + ("exit 29\n" if setup_fails else 'printf "%s/codex" "$PWD"\n')
        )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in {
        "pnpm": (
            'printf "%s\\n" "$DRAMACLAW_CHAT_BACKEND" "${CODEX_BIN:-}" '
            '"${HERMES_CLI_PATH:-}" > "$START_RESULT"; exit 23'
        ),
        "curl": "exit 0",
        "uv": 'printf "%s\\n" "$*" > "$UV_RESULT"',
    }.items():
        executable = binaries / name
        executable.write_text(f"#!/bin/sh\n{body}\n")
        executable.chmod(0o755)
    setup_output = tmp_path / "setup-output"
    start_output = tmp_path / "start-output"
    uv_output = tmp_path / "uv-output"
    result = subprocess.run(
        [_bash(), str(launcher)],
        env={
            "PATH": f"{binaries}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DRAMACLAW_CHAT_BACKEND": backend,
            "SUPERTALE_CHAT_BACKEND": legacy_backend,
            "SETUP_RESULT": str(setup_output),
            "START_RESULT": str(start_output),
            "UV_RESULT": str(uv_output),
            "ST_CONTROL_PLANE_DSN": "must-not-use-ee",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    if expected_backend is None:
        assert result.returncode == 2
        assert not setup_output.exists()
        assert not start_output.exists()
    else:
        assert result.returncode == (29 if setup_fails else 23), result.stderr
        assert setup_output.read_text().splitlines() == [expected_backend]
        if setup_fails:
            assert not start_output.exists()
        elif expected_backend == "hermes":
            assert start_output.read_text().splitlines() == [
                "hermes", "./my runtime/codex", _posix(tmp_path / "custom-hermes/bin/hermes")
            ]
            assert uv_output.read_text().splitlines() == [
                "sync --group dev --no-install-package openai-codex-cli-bin"
            ]
        else:
            assert start_output.read_text().splitlines() == [
                "codex", _posix(tmp_path / "codex"), ""
            ]

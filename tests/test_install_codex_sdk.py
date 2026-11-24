from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "deploy"))

import install_codex_sdk  # noqa: E402


def _make_tar_gz(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in entries.items():
            if isinstance(content, str) and content.startswith("SYMLINK:"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = content.removeprefix("SYMLINK:")
                tf.addfile(info)
            else:
                raw = content.encode("utf-8") if isinstance(content, str) else content
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                tf.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def test_parse_codex_git_source_reads_uv_lock() -> None:
    repo_path, rev, subdir = install_codex_sdk.parse_codex_git_source(
        REPOSITORY_ROOT / "uv.lock"
    )
    assert repo_path == "openai/codex"
    assert len(rev) == 40
    assert subdir == "sdk/python"
    assert (
        install_codex_sdk.build_archive_url(repo_path, rev)
        == f"https://github.com/openai/codex/archive/{rev}.tar.gz"
    )


def test_extract_subdirectory_filters_to_sdk_python(tmp_path: Path) -> None:
    rev = "a" * 40
    archive = _make_tar_gz(
        {
            f"codex-{rev}/README.md": "ignored",
            f"codex-{rev}/sdk/python/pyproject.toml": "[project]\nname = 'openai-codex'\n",
            f"codex-{rev}/sdk/python/src/openai_codex/__init__.py": "",
            f"codex-{rev}/sdk/python/evil_link": "SYMLINK:/etc/passwd",
        }
    )
    sdk_dir = install_codex_sdk.extract_subdirectory(
        archive,
        repo_name="codex",
        rev=rev,
        subdir="sdk/python",
        dest_dir=tmp_path,
    )
    assert sdk_dir == tmp_path / f"codex-{rev}" / "sdk" / "python"
    assert (sdk_dir / "pyproject.toml").is_file()
    assert (sdk_dir / "src" / "openai_codex" / "__init__.py").is_file()
    assert not (tmp_path / f"codex-{rev}" / "README.md").exists()
    assert not (sdk_dir / "evil_link").exists()


def test_extract_subdirectory_rejects_missing_prefix(tmp_path: Path) -> None:
    rev = "b" * 40
    archive = _make_tar_gz({f"codex-{rev}/README.md": "only root"})
    with pytest.raises(ValueError, match="did not contain prefix"):
        install_codex_sdk.extract_subdirectory(
            archive,
            repo_name="codex",
            rev=rev,
            subdir="sdk/python",
            dest_dir=tmp_path,
        )


def test_dockerfile_uses_codex_sdk_archive_installer() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--no-install-package openai-codex-cli-bin" in dockerfile
    assert "--no-install-package openai-codex" in dockerfile
    assert "python3 deploy/install_codex_sdk.py" in dockerfile

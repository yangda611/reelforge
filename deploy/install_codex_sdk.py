"""Install the pinned ``openai-codex`` Python SDK from its GitHub commit archive.

In Debian containers ``git`` is linked against ``libcurl3t64-gnutls``, which
frequently fails mid-transfer (``GnuTLS recv error (-9)``) when ``uv sync``
fetches the large ``openai/codex`` monorepo through a proxy or high-latency
link. Downloading the exact ``uv.lock``-pinned commit archive over HTTPS
(OpenSSL) and installing only ``sdk/python`` with ``--no-deps`` avoids cloning
the full repository while keeping ``uv.lock`` as the single source of truth
and still excluding ``openai-codex-cli-bin``.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_codex_git_source(lock_path: Path) -> tuple[str, str, str]:
    """Return ``(owner_repo, commit_sha, subdirectory)`` for ``openai-codex``."""
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    for pkg in data.get("package", []):
        if pkg.get("name") == "openai-codex":
            git_url = (pkg.get("source") or {}).get("git")
            if not git_url:
                raise ValueError("openai-codex entry in uv.lock has no git source")
            parsed = urllib.parse.urlparse(git_url)
            if parsed.scheme != "https" or parsed.netloc != "github.com":
                raise ValueError(f"Unsupported openai-codex git host: {git_url}")
            repo_path = parsed.path.lstrip("/").removesuffix(".git")
            if repo_path.count("/") != 1:
                raise ValueError(f"Unexpected GitHub repository path: {repo_path!r}")
            query = urllib.parse.parse_qs(parsed.query)
            rev = parsed.fragment or (query.get("rev") or [""])[0]
            if not _SHA1_RE.fullmatch(rev):
                raise ValueError(f"Expected 40-char commit SHA in uv.lock, got {rev!r}")
            subdir = (query.get("subdirectory") or [""])[0].strip("/")
            if not subdir:
                raise ValueError(f"Missing subdirectory in openai-codex source: {git_url}")
            return repo_path, rev, subdir
    raise ValueError("openai-codex package not found in uv.lock")


def build_archive_url(repo_path: str, rev: str) -> str:
    """Build the GitHub commit archive tarball URL."""
    return f"https://github.com/{repo_path}/archive/{rev}.tar.gz"


def download_bytes(url: str, *, attempts: int = 3, timeout: int = 60) -> bytes:
    """Download ``url`` with bounded retries on transient HTTP/network errors."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2 * attempt, 5))
    assert last_error is not None
    raise last_error


def extract_subdirectory(
    archive_bytes: bytes,
    *,
    repo_name: str,
    rev: str,
    subdir: str,
    dest_dir: Path,
) -> Path:
    """Safely extract only ``<repo_name>-<rev>/<subdir>/`` from the tarball."""
    expected_prefix = f"{repo_name}-{rev}/{subdir}/"
    dest_resolved = dest_dir.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
        selected: list[tarfile.TarInfo] = []
        for member in tf.getmembers():
            if not member.name.startswith(expected_prefix):
                continue
            if member.issym() or member.islnk():
                continue
            target = (dest_resolved / member.name).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"Refusing path traversal in tar member: {member.name}")
            selected.append(member)
        if not selected:
            raise ValueError(f"Archive did not contain prefix {expected_prefix!r}")
        tf.extractall(dest_resolved, members=selected, filter="data")

    sdk_dir = dest_resolved / f"{repo_name}-{rev}" / subdir
    if not (sdk_dir / "pyproject.toml").is_file():
        raise ValueError(f"Extracted SDK directory missing pyproject.toml: {sdk_dir}")
    return sdk_dir


def install_codex_sdk(lock_path: Path, python_bin: Path) -> None:
    """Download and install ``openai-codex`` into ``python_bin``'s environment."""
    repo_path, rev, subdir = parse_codex_git_source(lock_path)
    _, repo_name = repo_path.split("/", 1)
    archive_url = build_archive_url(repo_path, rev)
    archive_bytes = download_bytes(archive_url)
    with tempfile.TemporaryDirectory(prefix="codex-sdk-") as tmp_dir:
        sdk_dir = extract_subdirectory(
            archive_bytes,
            repo_name=repo_name,
            rev=rev,
            subdir=subdir,
            dest_dir=Path(tmp_dir),
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python_bin), "--no-deps", str(sdk_dir)],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, default=Path("uv.lock"))
    parser.add_argument("--python", type=Path, default=Path("/app/.venv/bin/python"))
    args = parser.parse_args()
    install_codex_sdk(args.lock_file, args.python)


if __name__ == "__main__":
    main()

"""User-scoped lossless storage for native Markdown Agent Skills."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import portalocker
import yaml

from novelvideo.freezone.agent_catalog_schema import SAFE_AGENT_CONFIG_ID
from novelvideo.freezone.skill_import import (
    MAX_FILE_BYTES,
    _decode_markdown_bytes,
    _decode_zip_filename,
    _has_valid_zip_skill_entrypoint,
    _is_macos_metadata_entry,
)

NATIVE_SKILL_MANIFEST = "_native_skill_manifest.json"
MAX_NATIVE_PACKAGE_BYTES = 8 * MAX_FILE_BYTES
RESERVED_NATIVE_SKILL_IDS = frozenset({
    "dramaclaw",
    "dramaclaw-workflows",
    "freezone",
    "interactive-story",
    "workflows",
    "_user",
})
_NON_EXECUTABLE_TEXT_SUFFIXES = frozenset({
    ".md", ".txt", ".json", ".yaml", ".yml", ".html", ".htm", ".css", ".svg",
})


def user_native_skills_dir(username: str) -> Path:
    from novelvideo.freezone.agent_config_store import OUTPUT_DIR

    return Path(OUTPUT_DIR) / username / "_account" / "freezone" / "native_skills"


def _validate_native_skill_id(skill_id: str) -> str:
    cleaned = str(skill_id or "").strip()
    if not SAFE_AGENT_CONFIG_ID.fullmatch(cleaned) or cleaned in RESERVED_NATIVE_SKILL_IDS:
        raise ValueError("invalid native skill id")
    return cleaned


def _slugify_skill_id(raw: str, *, fallback_hash: str) -> str:
    candidate = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", str(raw or "").strip())
    candidate = re.sub(r"[^a-z0-9_-]+", "-", candidate.lower()).strip("_-")[:128]
    if (
        SAFE_AGENT_CONFIG_ID.fullmatch(candidate)
        and candidate not in RESERVED_NATIVE_SKILL_IDS
    ):
        return candidate
    return f"skill-{fallback_hash[:12]}"


def _parse_frontmatter(skill_md: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^\ufeff?---\r?\n(.*?)\r?\n---\r?\n(.*)$", skill_md, re.DOTALL)
    if not match:
        return {}, skill_md
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, skill_md
    if not isinstance(loaded, dict):
        return {}, skill_md
    return loaded, match.group(2)


def _derive_description(body: str, fallback_name: str) -> str:
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:240]
    return fallback_name


def _ensure_skill_md_frontmatter(
    raw_bytes: bytes,
    *,
    skill_id: str,
    display_name: str,
    description: str,
) -> bytes:
    text = _decode_markdown_bytes(raw_bytes, "SKILL.md")
    meta, body = _parse_frontmatter(text)
    if (
        isinstance(meta.get("name"), str)
        and meta["name"].strip()
        and isinstance(meta.get("description"), str)
        and meta["description"].strip()
    ):
        # Preserve original SKILL.md bytes untouched when frontmatter is already valid.
        return raw_bytes
    merged = dict(meta)
    merged.setdefault("name", skill_id)
    if not str(merged.get("name") or "").strip():
        merged["name"] = skill_id
    if not str(merged.get("description") or "").strip():
        merged["description"] = description or display_name
    frontmatter = yaml.safe_dump(
        merged, allow_unicode=True, sort_keys=False
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{body.lstrip()}".encode("utf-8")


def _extract_heading_title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading[:120]
    return fallback


def parse_native_skill_package(name: str, content_base64: str) -> dict[str, Any]:
    """Validate and extract a Markdown/ZIP Skill package without altering its content."""
    if len(content_base64) > (MAX_FILE_BYTES * 4 // 3 + 8):
        raise ValueError("Source exceeds 2 MiB")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid base64 source") from exc
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("Source must contain 1 byte to 2 MiB")

    source_name = Path(name).name
    sha256 = hashlib.sha256(data).hexdigest()
    files: list[tuple[str, bytes]] = []
    warnings: list[str] = []

    if source_name.lower().endswith(".md"):
        _decode_markdown_bytes(data, source_name)
        files.append(("SKILL.md", data))
    elif source_name.lower().endswith(".zip"):
        raw_entries: list[tuple[str, bytes]] = []
        md_docs: list[tuple[str, str]] = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 128 or sum(x.file_size for x in entries) > MAX_NATIVE_PACKAGE_BYTES:
                    raise ValueError("Archive exceeds expanded size or file count limit")
                seen: set[str] = set()
                for entry in entries:
                    entry_name = _decode_zip_filename(entry)
                    path = PurePosixPath(entry_name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or "\\" in entry.filename
                        or "\\" in entry_name
                        or ":" in entry.filename
                        or ":" in entry_name
                        or entry_name in seen
                        or stat.S_ISLNK(entry.external_attr >> 16)
                    ):
                        raise ValueError("Unsafe or duplicate archive path")
                    seen.add(entry_name)
                    if _is_macos_metadata_entry(path):
                        continue
                    if entry.flag_bits & 1 or entry.file_size > MAX_FILE_BYTES:
                        raise ValueError("Encrypted or oversized archive entry")
                    if entry.is_dir():
                        continue
                    raw = archive.read(entry)
                    raw_entries.append((str(path), raw))
                    if path.suffix.lower() == ".md":
                        md_docs.append((str(path), _decode_markdown_bytes(raw, str(path))))
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError("Invalid ZIP archive") from exc
        if not _has_valid_zip_skill_entrypoint(md_docs):
            raise ValueError("ZIP must contain exactly one SKILL.md")

        entrypoint = next(
            (doc_path for doc_path, _ in md_docs if PurePosixPath(doc_path).name.lower() == "skill.md"),
            md_docs[0][0],
        )
        root_dir = posixpath.dirname(entrypoint)
        seen_rel: set[str] = set()
        for entry_path, raw in raw_entries:
            if root_dir:
                prefix = root_dir + "/"
                if not entry_path.startswith(prefix):
                    continue
                rel = entry_path[len(prefix):]
            else:
                rel = entry_path
            if entry_path == entrypoint:
                rel = "SKILL.md"
            rel_path = PurePosixPath(rel)
            if not rel or rel_path.is_absolute() or ".." in rel_path.parts or rel in seen_rel:
                raise ValueError("Unsafe or duplicate archive path")
            seen_rel.add(rel)
            files.append((rel, raw))
            if rel_path.suffix.lower() not in _NON_EXECUTABLE_TEXT_SUFFIXES:
                warnings.append(f"Preserved as static reference only (not executed in sandbox): {rel}")
    else:
        raise ValueError("Only .md and .zip sources are supported")

    skill_entry = next((raw for rel, raw in files if rel == "SKILL.md"), None)
    if skill_entry is None:
        raise ValueError("Skill package must contain SKILL.md")
    skill_text = _decode_markdown_bytes(skill_entry, "SKILL.md")
    meta, body = _parse_frontmatter(skill_text)
    raw_name = str(meta.get("name") or "").strip()
    stem = Path(source_name).stem
    skill_id = _slugify_skill_id(raw_name or stem, fallback_hash=sha256)
    display_name = _extract_heading_title(body, raw_name or stem or skill_id)
    description = str(meta.get("description") or "").strip() or _derive_description(body, display_name)

    final_skill_bytes = _ensure_skill_md_frontmatter(
        skill_entry,
        skill_id=skill_id,
        display_name=display_name,
        description=description,
    )
    normalized_files = [
        (rel, final_skill_bytes if rel == "SKILL.md" else raw)
        for rel, raw in files
    ]
    return {
        "skill_id": skill_id,
        "name": display_name,
        "description": description,
        "source_name": source_name,
        "source_sha256": sha256,
        "warnings": warnings,
        "files": normalized_files,
    }


def _read_manifest(skill_dir: Path) -> dict[str, Any] | None:
    manifest_path = skill_dir / NATIVE_SKILL_MANIFEST
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    skill_id = str(payload.get("id") or "").strip()
    if not SAFE_AGENT_CONFIG_ID.fullmatch(skill_id) or not (skill_dir / "SKILL.md").is_file():
        return None
    return payload


def list_user_native_skills(username: str) -> list[dict[str, Any]]:
    root = user_native_skills_dir(username)
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        manifest = _read_manifest(entry)
        if manifest is not None:
            items.append(manifest)
    return items


def get_user_native_skill(username: str, skill_id: str) -> dict[str, Any] | None:
    checked_id = _validate_native_skill_id(skill_id)
    return _read_manifest(user_native_skills_dir(username) / checked_id)


def read_user_native_skill_file(
    *,
    username: str,
    skill_id: str,
    file_path: str = "SKILL.md",
) -> dict[str, Any]:
    """Read one text file from an installed lossless native Markdown Skill."""
    checked_id = _validate_native_skill_id(skill_id)
    skill_dir = user_native_skills_dir(username) / checked_id
    manifest = _read_manifest(skill_dir)
    if manifest is None:
        raise FileNotFoundError(f"native skill not found: {checked_id}")

    raw_rel = str(file_path or "SKILL.md").strip()
    while raw_rel.startswith("./"):
        raw_rel = raw_rel[2:]
    if (
        not raw_rel
        or raw_rel.startswith("/")
        or "\\" in raw_rel
        or ":" in raw_rel
        or ".." in PurePosixPath(raw_rel).parts
        or raw_rel == NATIVE_SKILL_MANIFEST
    ):
        raise ValueError("invalid native skill file path")
    rel = PurePosixPath(posixpath.normpath(raw_rel))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("invalid native skill file path")

    resolved_root = skill_dir.resolve()
    target = (skill_dir / Path(*rel.parts)).resolve()
    if not target.is_relative_to(resolved_root) or not target.is_file():
        raise FileNotFoundError(f"native skill file not found: {rel}")

    content = _decode_markdown_bytes(target.read_bytes(), str(rel))
    return {
        "skill_id": checked_id,
        "name": manifest.get("name", checked_id),
        "file_path": str(rel),
        "content": content,
        "available_files": list(manifest.get("files") or []),
    }


def install_native_skill(*, username: str, name: str, content_base64: str) -> dict[str, Any]:
    package = parse_native_skill_package(name, content_base64)
    root = user_native_skills_dir(username)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".native-skills.lock").open("a") as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        skill_id = package["skill_id"]
        target = root / skill_id
        staging = root / f".staging-{skill_id}-{uuid.uuid4().hex}"
        backup = root / f".backup-{skill_id}-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True, exist_ok=True)
            for rel, raw in package["files"]:
                dest = staging / Path(rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
            manifest = {
                "id": skill_id,
                "name": package["name"],
                "description": package["description"],
                "source_name": package["source_name"],
                "source_sha256": package["source_sha256"],
                "file_count": len(package["files"]),
                "files": sorted(rel for rel, _ in package["files"]),
                "warnings": list(package["warnings"]),
                "enabled": True,
                "installed_at": time.time(),
                "storage_path": str(target),
            }
            (staging / NATIVE_SKILL_MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if target.exists():
                os.replace(target, backup)
            os.replace(staging, target)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            return manifest
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and not target.exists():
                try:
                    os.replace(backup, target)
                except OSError:
                    pass
            raise


def delete_user_native_skill(*, username: str, skill_id: str) -> bool:
    checked_id = _validate_native_skill_id(skill_id)
    root = user_native_skills_dir(username)
    if not root.is_dir():
        return False
    with (root / ".native-skills.lock").open("a") as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        target = root / checked_id
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

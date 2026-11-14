"""External Skill sources and native Bundle conversion checkpoints.

Sources are data only: never extracted or executed. Records are requester scoped.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import portalocker

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
MAX_PROMPT_CHARS = 450_000


def _decode_zip_filename(entry: zipfile.ZipInfo) -> str:
    """Recover UTF-8/GB18030 names from ZIPs packed without the UTF-8 bit."""
    name = entry.filename
    if entry.flag_bits & 0x800:
        return name
    try:
        raw_name = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw_name.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def _is_macos_metadata_entry(path: PurePosixPath) -> bool:
    return (
        "__MACOSX" in path.parts
        or path.name.startswith("._")
        or path.name == ".DS_Store"
    )


def _decode_markdown_bytes(raw: bytes, label: str) -> str:
    """Decode Markdown text safely, supporting UTF-8 BOM, UTF-16 BOM, and GB18030."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Unable to decode UTF-16 Markdown file: {label}") from exc
    if b"\x00" in raw:
        raise ValueError(f"Markdown source must be text, not binary: {label}")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Markdown source must be UTF-8 or GB18030 text: {label}")


def _has_valid_zip_skill_entrypoint(docs: list[tuple[str, str]]) -> bool:
    if not docs:
        return False
    exact_skill_count = sum(
        PurePosixPath(path).name.lower() == "skill.md" for path, _ in docs
    )
    if exact_skill_count == 1:
        return True
    if exact_skill_count > 1:
        return False
    if len(docs) == 1:
        return True
    named_skill_count = sum(
        PurePosixPath(path).name.lower().endswith("skill.md") for path, _ in docs
    )
    return named_skill_count == 1


def read_source(name: str, encoded: str) -> dict[str, Any]:
    if len(encoded) > (MAX_FILE_BYTES * 4 // 3 + 8):
        raise ValueError('Source exceeds 2 MiB')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Invalid base64 source') from exc
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError('Source must contain 1 byte to 2 MiB')
    docs: list[tuple[str, str]] = []
    warnings: list[str] = []
    resources: set[str] = set()
    if name.lower().endswith('.md'):
        file_name = Path(name).name
        docs.append((file_name, _decode_markdown_bytes(data, file_name)))
        resources.add(file_name)
    elif name.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 128 or sum(x.file_size for x in entries) > 8 * MAX_FILE_BYTES:
                    raise ValueError('Archive exceeds expanded size or file count limit')
                seen = set()
                for entry in entries:
                    entry_name = _decode_zip_filename(entry)
                    path = PurePosixPath(entry_name)
                    if (
                        path.is_absolute()
                        or '..' in path.parts
                        or '\\' in entry.filename
                        or '\\' in entry_name
                        or ':' in entry.filename
                        or ':' in entry_name
                        or entry_name in seen
                        or stat.S_ISLNK(entry.external_attr >> 16)
                    ):
                        raise ValueError('Unsafe or duplicate archive path')
                    seen.add(entry_name)
                    if _is_macos_metadata_entry(path):
                        continue
                    resources.add(entry_name)
                    if entry.flag_bits & 1 or entry.file_size > MAX_FILE_BYTES:
                        raise ValueError('Encrypted or oversized archive entry')
                    if entry.is_dir():
                        continue
                    if path.suffix.lower() == '.md':
                        docs.append(
                            (
                                str(path),
                                _decode_markdown_bytes(archive.read(entry), str(path)),
                            )
                        )
                    else:
                        warnings.append(f'Not converted or executed: {path}')
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError('Invalid ZIP archive') from exc
        if not _has_valid_zip_skill_entrypoint(docs):
            raise ValueError('ZIP must contain exactly one SKILL.md')
    else:
        raise ValueError('Only .md and .zip sources are supported')
    for doc_path, body in docs:
        for link in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:[^)]*)\)', body):
            link = link.strip('<>').split('#', 1)[0]
            if not link or re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', link):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), link))
            if target not in resources:
                warnings.append(f'Missing referenced resource: {target}')
    text = '\n\n'.join(f'--- SOURCE FILE: {path} ---\n{body}' for path, body in docs)
    if not text.strip() or len(text.encode()) > MAX_TEXT_BYTES:
        raise ValueError('Markdown content exceeds 512 KiB conversion context budget')
    return {'name': Path(name).name, 'text': text, 'warnings': warnings,
            'sha256': hashlib.sha256(data).hexdigest(), 'content_base64': encoded, 'resources': sorted(resources)}


def record_path(root: Path, username: str, import_id: str) -> Path:
    if not re.fullmatch(r'[a-f0-9]{32}', import_id):
        raise ValueError('Invalid import id')
    account = hashlib.sha256(username.encode()).hexdigest()
    return root / 'skill_imports' / account / f'{import_id}.json'


def save_record(root: Path, username: str, record: dict) -> None:
    path = record_path(root, username, record['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.write-lock').open('a') as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        if path.exists():
            record['task_id'] = json.loads(path.read_text()).get('task_id', '')
        _write_record(path, record)


def _write_record(path: Path, record: dict) -> None:
    temporary = path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def bind_task(root: Path, username: str, import_id: str, task_id: str) -> dict:
    path = record_path(root, username, import_id)
    with path.with_suffix('.write-lock').open('a') as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        record = json.loads(path.read_text())
        record['task_id'] = task_id
        _write_record(path, record)
        return record


def get_record(root: Path, username: str, import_id: str) -> dict:
    path = record_path(root, username, import_id)
    if not path.is_file():
        raise FileNotFoundError(import_id)
    return json.loads(path.read_text(encoding='utf-8'))


def public_record(record: dict) -> dict:
    from novelvideo.freezone.skill_import_contracts import CONVERSION_VERSION

    result = {key: value for key, value in record.items()
            if key not in {'source', 'catalog', 'analysis', 'username', 'checkpoints', 'capability_snapshot', 'validation_candidate', 'diagnostics', 'diagnostic_run_id', 'diagnostics_dropped', 'candidate_catalog', 'catalog_fingerprint', 'source_analysis', 'conversion_design'}}
    result['current_conversion_version'] = CONVERSION_VERSION
    return result


def create_record(root: Path, username: str, source: dict, batch_id: str, batch_name: str = '') -> dict:
    record = {'id': uuid.uuid4().hex, 'batch_id': batch_id, 'batch_name': batch_name,
              'name': source['name'], 'status': 'queued', 'stage': 'queued',
              'task_id': '', 'error': None, 'warnings': source['warnings'], 'bundle': None,
              'created_at': time.time(), 'source_sha256': source['sha256'], 'conversion_version': 1, 'source': source, 'username': username,
              'candidate_catalog': []}
    save_record(root, username, record)
    return record


def parse_json_output(text: str) -> dict:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


async def convert_record(root: Path, username: str, import_id: str, generate, progress=lambda *args: None) -> dict:
    from novelvideo.freezone.skill_import_quality import run_quality_pipeline
    record = get_record(root, username, import_id)
    await run_quality_pipeline(record, username, generate,
                               lambda: save_record(root, username, record), progress)
    return public_record(record)


def validate_analysis(analysis: dict) -> None:
    if not isinstance(analysis.get('unsupported_dependencies'), list) or not all(
            isinstance(value, str) for value in analysis['unsupported_dependencies']):
        raise ValueError('Analysis unsupported_dependencies must be an array of strings')
    if not isinstance(analysis.get('source_mapping'), (dict, list)) or not analysis['source_mapping']:
        raise ValueError('Analysis must include a nonempty source_mapping')


def validate_reused_recipes(bundle: dict, catalog: list[dict]) -> None:
    from novelvideo.freezone.agent_catalog_schema import validate_agent_recipe_config
    existing = {recipe['id']: validate_agent_recipe_config(recipe) for recipe in catalog}
    for recipe in bundle['recipes']:
        if recipe['id'] in existing and recipe != existing[recipe['id']]:
            raise ValueError(f"Recipe {recipe['id']} differs from catalog; use a new ID for a distinct method")

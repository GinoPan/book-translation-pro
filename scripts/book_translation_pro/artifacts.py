"""Portable artifact, hashing, schema, and path helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import sys
from pathlib import Path
from typing import Any

import jsonschema


class BTPError(RuntimeError):
    """Expected workflow error suitable for concise CLI reporting."""


def normalize_path(path: Path) -> Path:
    """Return an absolute path, using Windows extended syntax near MAX_PATH."""
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and len(value) >= 180 and not value.startswith("\\\\?\\"):
        if value.startswith("\\\\"):
            value = "\\\\?\\UNC\\" + value[2:]
        else:
            value = "\\\\?\\" + value
    return Path(value)


def _plain_absolute(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if os.name == "nt" and value.startswith("\\\\?\\"):
        return value[4:]
    return value


def external_tool_path(path: Path) -> str:
    """Return an absolute path without Windows extended syntax for CLI tools.

    Internal file operations use ``\\\\?\\`` when needed, but Java URI parsing
    treats that prefix as a hostname. Modern Windows processes can consume the
    equivalent plain absolute path directly.
    """
    return _plain_absolute(path)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_data(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BTPError(f"Required file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BTPError(f"Invalid JSON in {path}: {exc}") from exc


def _atomic_target(path: Path) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)


def atomic_write_text(path: Path, text: str) -> None:
    fd, temp_name = _atomic_target(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def atomic_copy(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BTPError(f"Input file does not exist: {source}")
    fd, temp_name = _atomic_target(destination)
    os.close(fd)
    try:
        shutil.copy2(source, temp_name)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def safe_project_path(project: Path, relative: str) -> Path:
    project_plain = _plain_absolute(project)
    candidate_plain = os.path.abspath(os.path.join(project_plain, relative.replace("/", os.sep)))
    try:
        common = os.path.commonpath((project_plain, candidate_plain))
    except ValueError as exc:
        raise BTPError(f"Path escapes project workspace: {relative}") from exc
    if os.path.normcase(common) != os.path.normcase(project_plain):
        raise BTPError(f"Path escapes project workspace: {relative}")
    return normalize_path(Path(candidate_plain))


def portable_path(path: Path, project: Path) -> str:
    try:
        project_plain = _plain_absolute(project)
        path_plain = _plain_absolute(path)
        common = os.path.commonpath((project_plain, path_plain))
        if os.path.normcase(common) != os.path.normcase(project_plain):
            raise ValueError
        return Path(os.path.relpath(path_plain, project_plain)).as_posix()
    except ValueError as exc:
        raise BTPError(f"Artifact is outside the project workspace: {path}") from exc


def ensure_nonblank(path: Path) -> str:
    if not path.is_file():
        raise BTPError(f"Required output is missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BTPError(f"Output is not valid UTF-8: {path}") from exc
    if not text.strip():
        raise BTPError(f"Output is blank: {path}")
    return text


def schema_path(name: str) -> Path:
    skill_root = Path(__file__).resolve().parents[2]
    source_path = skill_root / "references" / "schemas" / name
    if source_path.is_file():
        return source_path
    installed_path = Path(sys.prefix) / "share" / "book-translation-pro" / "references" / "schemas" / name
    if installed_path.is_file():
        return installed_path
    raise BTPError(f"Installed schema is missing: {name}")


def validate_schema(value: Any, name: str) -> None:
    schema = load_json(schema_path(name))
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise BTPError(f"Schema validation failed for {name} at {where}: {exc.message}") from exc


def project_file(project: Path, relative: str, *, required: bool = True) -> Path:
    path = safe_project_path(project, relative)
    if required and not path.exists():
        raise BTPError(f"Required project artifact is missing: {relative}")
    return path

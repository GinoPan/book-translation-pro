"""Approved QA exception storage and matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json, load_json, validate_schema


def empty_exceptions() -> dict[str, Any]:
    return {"schema_version": 1, "exceptions": []}


def load_exceptions(project: Path) -> dict[str, Any]:
    path = project / "qa" / "exceptions.json"
    if not path.is_file():
        return empty_exceptions()
    value = load_json(path)
    validate_schema(value, "qa-exceptions.schema.json")
    return value


def save_exceptions(project: Path, value: dict[str, Any]) -> dict[str, Any]:
    validate_schema(value, "qa-exceptions.schema.json")
    ids = [item["id"] for item in value["exceptions"]]
    if len(ids) != len(set(ids)):
        from .artifacts import BTPError

        raise BTPError("QA exception IDs must be unique")
    atomic_write_json(project / "qa" / "exceptions.json", value)
    return value


def record_exceptions(project: Path, source: Path) -> dict[str, Any]:
    value = load_json(source)
    save_exceptions(project, value)
    return {"recorded": "exceptions", "exception_count": len(value["exceptions"])}


def approved_exception_ids(
    project: Path, check_id: str | None = None, scope: str | None = None
) -> list[str]:
    matches: list[str] = []
    for item in load_exceptions(project)["exceptions"]:
        if item["status"] != "approved":
            continue
        if check_id is not None and item["check_id"] != check_id:
            continue
        if scope is not None and item["scope"] not in {scope, "book", "publication"}:
            continue
        matches.append(item["id"])
    return sorted(matches)

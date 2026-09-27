"""Glossary validation, selection, hashing, and review rendering."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, atomic_write_text, hash_data, load_json, safe_project_path, validate_schema


def load_glossary(project: Path) -> dict[str, Any]:
    value = load_json(project / "terminology" / "glossary.json")
    validate_glossary(value)
    return value


def _overlay_entries(base: list[dict[str, Any]], overlay: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay by ID or source/alias surface, with the later source winning."""
    result = list(base)
    for entry in overlay:
        surfaces = {str(entry["source"]).casefold().strip(), *(
            str(alias).casefold().strip() for alias in entry.get("aliases", [])
        )}
        result = [
            current for current in result
            if current["id"] != entry["id"]
            and not surfaces.intersection({
                str(current["source"]).casefold().strip(),
                *(str(alias).casefold().strip() for alias in current.get("aliases", [])),
            })
        ]
        result.append(entry)
    return result


def load_effective_glossary(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Merge reusable libraries in order, then apply the project glossary."""
    entries: list[dict[str, Any]] = []
    for relative in config.get("terminology", {}).get("libraries", []):
        library_path = safe_project_path(project, relative)
        library = load_json(library_path)
        validate_glossary(library)
        entries = _overlay_entries(entries, library["entries"])
    project_glossary = load_glossary(project)
    entries = _overlay_entries(entries, project_glossary["entries"])
    value = {"schema_version": 1, "entries": sorted(entries, key=lambda item: item["id"])}
    validate_glossary(value)
    atomic_write_json(project / "terminology" / "effective-glossary.json", value)
    return value


def validate_glossary(value: dict[str, Any]) -> None:
    validate_schema(value, "glossary.schema.json")
    owners: dict[str, str] = {}
    for entry in value["entries"]:
        for surface in [entry["source"], *entry.get("aliases", [])]:
            key = surface.casefold().strip()
            if key in owners and owners[key] != entry["id"]:
                raise BTPError(
                    f"Glossary surface {surface!r} belongs to both {owners[key]!r} and {entry['id']!r}"
                )
            owners[key] = entry["id"]


def save_glossary(project: Path, value: dict[str, Any]) -> None:
    validate_glossary(value)
    atomic_write_json(project / "terminology" / "glossary.json", value)
    atomic_write_text(project / "terminology" / "glossary-review.md", render_review(value))


def _surface_pattern(surface: str) -> re.Pattern[str]:
    escaped = re.escape(surface)
    if surface.isascii() and any(char.isalnum() for char in surface):
        return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
    return re.compile(escaped)


def entry_matches(entry: dict[str, Any], text: str) -> bool:
    return any(_surface_pattern(surface).search(text) for surface in [entry["source"], *entry.get("aliases", [])])


def entry_hash(entry: dict[str, Any]) -> str:
    semantic = {
        key: entry.get(key)
        for key in ("source", "target", "aliases", "category", "domain", "grammatical", "status", "scopes")
    }
    return hash_data(semantic)


def select_entries(glossary: dict[str, Any], text: str, book_node_id: str | None = None) -> list[dict[str, Any]]:
    selected = []
    for entry in glossary["entries"]:
        if entry["status"] != "approved":
            continue
        scopes = entry.get("scopes", [])
        if scopes and book_node_id not in scopes:
            continue
        if entry_matches(entry, text):
            selected.append(entry)
    return sorted(selected, key=lambda item: item["id"])


def selected_hashes(glossary: dict[str, Any], text: str, book_node_id: str | None) -> dict[str, str]:
    return {entry["id"]: entry_hash(entry) for entry in select_entries(glossary, text, book_node_id)}


def count_frequencies(glossary: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    for entry in glossary["entries"]:
        entry["frequency"] = sum(
            len(_surface_pattern(surface).findall(text))
            for surface in [entry["source"], *entry.get("aliases", [])]
            for text in texts
        )
    return glossary


def render_review(glossary: dict[str, Any]) -> str:
    lines = [
        "# Glossary Review",
        "",
        "Review proposed entries, resolve conflicts, and mark binding entries as `approved` before the terminology checkpoint.",
        "",
        "| ID | Source | Target | Category | Status | Confidence | Frequency |",
        "|---|---|---|---|---|---|---:|",
    ]
    for entry in glossary["entries"]:
        cells = [
            entry["id"],
            entry["source"],
            entry["target"],
            entry["category"],
            entry["status"],
            entry["confidence"],
            str(entry.get("frequency", 0)),
        ]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines) + "\n"


def empty_glossary() -> dict[str, Any]:
    return {"schema_version": 1, "entries": []}

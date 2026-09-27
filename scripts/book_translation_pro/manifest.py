"""Work-unit manifest creation and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import (
    BTPError,
    atomic_write_json,
    hash_data,
    load_json,
    portable_path,
    safe_project_path,
    sha256_file,
    validate_schema,
)


def create_manifest(
    project: Path,
    units: list[dict[str, Any]],
    source_fingerprint: str,
    config_hash: str,
    book_map: dict[str, Any],
    style_hash: str,
    page_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entries = []
    for unit in units:
        source_file = project / "source-work" / "work-units" / f"{unit['id']}.md"
        entry = {
            "unit_id": unit["id"],
            "order": unit["order"],
            "book_node_id": unit["book_node_id"],
            "segment_ids": unit["segment_ids"],
            "source_file": portable_path(source_file, project),
            "source_hash": sha256_file(source_file),
            "draft_file": f"target/draft/{unit['id']}.md",
            "edit_file": f"target/edited/{unit['id']}.md",
            "capsule_file": f"context/capsules/{unit['id']}.json",
            "observation_file": f"state/observations/{unit['id']}.json",
        }
        if "page_indices" in unit:
            entry["page_indices"] = unit["page_indices"]
        entries.append(entry)
    manifest = {
        "schema_version": 1,
        "source_fingerprint": source_fingerprint,
        "resolved_config_hash": config_hash,
        "book_map_hash": hash_data(book_map),
        "style_guide_hash": style_hash,
        "units": entries,
    }
    if page_map is not None:
        manifest["page_map_hash"] = hash_data(page_map)
    validate_schema(manifest, "manifest.schema.json")
    atomic_write_json(project / "state" / "manifest.json", manifest)
    return manifest


def load_manifest(project: Path) -> dict[str, Any]:
    manifest = load_json(project / "state" / "manifest.json")
    validate_schema(manifest, "manifest.schema.json")
    return manifest


def validate_manifest_sources(project: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for entry in manifest["units"]:
        unit_id = entry["unit_id"]
        if unit_id in seen_ids:
            errors.append(f"duplicate_unit_id:{unit_id}")
        seen_ids.add(unit_id)
        if entry["order"] in seen_orders:
            errors.append(f"duplicate_unit_order:{entry['order']}")
        seen_orders.add(entry["order"])
        try:
            path = safe_project_path(project, entry["source_file"])
        except BTPError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file():
            errors.append(f"missing_source:{unit_id}")
        elif sha256_file(path) != entry["source_hash"]:
            errors.append(f"source_hash_changed:{unit_id}")
    expected_orders = set(range(len(manifest["units"])))
    if seen_orders != expected_orders:
        errors.append("non_contiguous_unit_order")
    return errors

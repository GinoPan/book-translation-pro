"""Explicit, auditable migrations for released project contracts."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, hash_data, load_json, sha256_file


CURRENT_CONFIG_SCHEMA = 2
CURRENT_STATE_SCHEMA = 2


def migrate_config_data(value: dict[str, Any]) -> dict[str, Any]:
    """Return the current config shape without mutating the caller's value."""
    result = deepcopy(value)
    version = int(result.get("schema_version", 1))
    if version > CURRENT_CONFIG_SCHEMA:
        raise BTPError(f"Project configuration schema v{version} is newer than this release")
    if version < 1:
        raise BTPError(f"Unsupported project configuration schema v{version}")
    if version == 1:
        result["schema_version"] = 2
        result.setdefault("terminology", {}).setdefault("libraries", [])
        result.setdefault("style_guide", {}).setdefault("template", "general")
        result.setdefault("publishing", {}).setdefault(
            "bilingual_formats", ["markdown", "docx", "epub"]
        )
    return result


def migrate_state_data(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    version = int(result.get("schema_version", 1))
    if version > CURRENT_STATE_SCHEMA:
        raise BTPError(f"Run-state schema v{version} is newer than this release")
    if version < 1:
        raise BTPError(f"Unsupported run-state schema v{version}")
    if version == 1:
        from .state import now_utc

        stamp = now_utc()
        result["schema_version"] = 2
        result.setdefault("metrics", {"started_at": stamp, "updated_at": stamp})
        for review in result.get("human_reviews", []):
            review.setdefault("evidence_hashes", {})
            review.setdefault("exception_ids", [])
    return result


def migrate_project(project: Path) -> dict[str, Any]:
    """Migrate a v1 project in place and retain an audit record.

    Translation and edit records remain current because the v1->v2 migration
    only adds release metadata. QA/build are invalidated so the new gates run,
    but no work unit is silently scheduled for retranslation.
    """
    from .config import load_yaml, resolve_config, write_yaml

    config_path = project / "book-project.yaml"
    raw = load_yaml(config_path)
    before_config_version = int(raw.get("schema_version", 1))
    migrated_raw = migrate_config_data(raw)
    resolved = resolve_config(migrated_raw)
    new_config_hash = hash_data(resolved)

    previous_resolved_path = project / "resolved-config.json"
    old_config_hash = None
    if previous_resolved_path.is_file():
        old_config_hash = hash_data(load_json(previous_resolved_path))

    write_yaml(config_path, migrated_raw)
    atomic_write_json(previous_resolved_path, resolved)
    from .artifacts import atomic_write_text

    atomic_write_text(project / "resolved-config.sha256", new_config_hash + "\n")

    state_path = project / "state" / "run-state.json"
    state_before_version = None
    if state_path.is_file():
        state = load_json(state_path)
        state_before_version = int(state.get("schema_version", 1))
        state = migrate_state_data(state)
        state["resolved_config_hash"] = new_config_hash
        for record in state.get("units", {}).values():
            dependencies = record.get("dependency_hashes", {})
            if "config" in dependencies:
                dependencies["config"] = new_config_hash
        for stage_name in ("qa", "build"):
            stage = state.get("stages", {}).get(stage_name)
            if stage and stage.get("status") in {"completed", "skipped_approved"}:
                stage.update({
                    "status": "invalidated",
                    "reason_codes": ["schema_migrated"],
                    "output_hashes": {},
                })
        atomic_write_json(state_path, state)

    manifest_path = project / "state" / "manifest.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        manifest["resolved_config_hash"] = new_config_hash
        atomic_write_json(manifest_path, manifest)

    audit = {
        "schema_version": 1,
        "migration": "project-v1-to-v2",
        "config_schema_before": before_config_version,
        "config_schema_after": CURRENT_CONFIG_SCHEMA,
        "state_schema_before": state_before_version,
        "state_schema_after": CURRENT_STATE_SCHEMA if state_before_version is not None else None,
        "old_resolved_config_hash": old_config_hash,
        "new_resolved_config_hash": new_config_hash,
        "work_units_invalidated": 0,
        "qa_build_invalidated": state_path.is_file(),
        "config_sha256": sha256_file(config_path),
    }
    audit_path = project / "state" / "migrations" / "project-v1-to-v2.json"
    atomic_write_json(audit_path, audit)
    return audit

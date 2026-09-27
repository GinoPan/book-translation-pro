"""Dependency-aware run state, planning, recording, and checkpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import (
    BTPError,
    atomic_copy,
    atomic_write_json,
    ensure_nonblank,
    hash_data,
    load_json,
    safe_project_path,
    sha256_file,
    validate_schema,
)
from .config import load_resolved_project
from .glossary import load_effective_glossary, selected_hashes
from .manifest import load_manifest, validate_manifest_sources


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_record(status: str = "pending") -> dict[str, Any]:
    return {
        "status": status,
        "attempts": 0,
        "dependency_hashes": {},
        "output_hashes": {},
        "reason_codes": [],
        "updated_at": now_utc(),
    }


def load_state(project: Path) -> dict[str, Any]:
    state = load_json(project / "state" / "run-state.json")
    validate_schema(state, "run-state.schema.json")
    return state


def save_state(project: Path, state: dict[str, Any]) -> None:
    state.setdefault("metrics", {"started_at": now_utc(), "updated_at": now_utc()})
    state["metrics"]["updated_at"] = now_utc()
    validate_schema(state, "run-state.schema.json")
    atomic_write_json(project / "state" / "run-state.json", state)


def _set_stage_status(state: dict[str, Any], name: str, status: str, reason_codes: list[str]) -> None:
    """Update a derived stage without treating each status refresh as a new attempt."""
    stage = state["stages"][name]
    if stage["status"] != status or stage.get("reason_codes", []) != reason_codes:
        stage["status"] = status
        stage["reason_codes"] = reason_codes
        stage["updated_at"] = now_utc()


def refresh_work_stages(state: dict[str, Any], require_edit: bool) -> None:
    """Derive translate/edit stage status from the durable per-unit records."""
    records = list(state["units"].values())
    translated = bool(records) and all(record.get("output_hashes", {}).get("draft") for record in records)
    terminal = any(record["status"] == "failed_terminal" for record in records)
    translate_status = "failed_terminal" if terminal else "completed" if translated else "pending"
    _set_stage_status(state, "translate", translate_status, ["unit_failure"] if terminal else [])

    if not require_edit:
        _set_stage_status(state, "edit", "skipped_approved", [])
        return
    edited = bool(records) and all(record.get("output_hashes", {}).get("edit") for record in records)
    edit_status = "failed_terminal" if terminal else "completed" if edited else "pending"
    _set_stage_status(state, "edit", edit_status, ["unit_failure"] if terminal else [])


def initialize_state(
    project: Path,
    project_id: str,
    source_fingerprint: str,
    config_hash: str,
    manifest: dict[str, Any],
    checkpoints: list[str],
    visual_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visual_summary = visual_summary or {"applicable": False, "required_review_pages": []}
    reviews = []
    for name in checkpoints:
        if name == "visual_exceptions":
            pending_pages = visual_summary.get("required_review_pages", [])
            status = "pending" if pending_pages else "not_required"
            notes = f"Pending required page reviews: {pending_pages}" if pending_pages else "No page meets the mode review threshold."
        else:
            status, notes = "pending", ""
        reviews.append({"checkpoint": name, "status": status, "recorded_at": now_utc(), "notes": notes})
    visual_stage_status = "completed" if visual_summary.get("applicable") else "skipped_approved"
    state = {
        "schema_version": 2,
        "project_id": project_id,
        "source_fingerprint": source_fingerprint,
        "resolved_config_hash": config_hash,
        "stages": {
            name: empty_record(
                "completed" if name in {"initialize", "prepare"} else visual_stage_status if name == "visual" else "pending"
            )
            for name in ("initialize", "prepare", "visual", "translate", "edit", "qa", "build")
        },
        "units": {entry["unit_id"]: empty_record() for entry in manifest["units"]},
        "human_reviews": reviews,
        "metrics": {"started_at": now_utc(), "updated_at": now_utc()},
    }
    page_map_path = project / "analysis" / "page-map.json"
    if visual_summary.get("applicable") and page_map_path.is_file():
        state["stages"]["visual"]["output_hashes"] = {"page_map": sha256_file(page_map_path)}
    save_state(project, state)
    return state


def _review_pending(state: dict[str, Any], checkpoint: str) -> bool:
    return any(
        item["checkpoint"] == checkpoint and item["status"] not in {"approved", "not_required"}
        for item in state.get("human_reviews", [])
    )


def _current_dependencies(
    project: Path,
    entry: dict[str, Any],
    manifest: dict[str, Any],
    glossary: dict[str, Any],
    previous_entry: dict[str, Any] | None,
) -> dict[str, str]:
    source = safe_project_path(project, entry["source_file"])
    source_text = source.read_text(encoding="utf-8")
    if previous_entry:
        previous_capsule = safe_project_path(project, previous_entry["capsule_file"])
        incoming_capsule_hash = sha256_file(previous_capsule) if previous_capsule.is_file() else "missing"
    else:
        incoming_capsule_hash = hash_data({"root": True})
    book_map = load_json(project / "analysis" / "book-map.json")
    node = next((item for item in book_map["nodes"] if item["id"] == entry["book_node_id"]), {})
    term_hashes = selected_hashes(glossary, source_text, entry["book_node_id"])
    dependencies = {
        "source": sha256_file(source),
        "config": manifest["resolved_config_hash"],
        "book_node": hash_data(node),
        "style_guide": sha256_file(project / "config" / "style-guide.md"),
        "incoming_capsule": incoming_capsule_hash,
        "glossary_selection": hash_data(term_hashes),
    }
    if "page_indices" in entry:
        from .visual import visual_dependency_hash

        dependencies["visual"] = visual_dependency_hash(project, entry["page_indices"])
    return dependencies


def plan_run(project: Path, require_edit: bool, max_attempts: int) -> dict[str, Any]:
    manifest = load_manifest(project)
    state = load_state(project)
    config, _ = load_resolved_project(project)
    glossary = load_effective_glossary(project, config)
    source_errors = validate_manifest_sources(project, manifest)
    blocked: list[str] = list(source_errors)
    if _review_pending(state, "terminology"):
        blocked.append("checkpoint_pending:terminology")
    if _review_pending(state, "visual_exceptions"):
        blocked.append("checkpoint_pending:visual_exceptions")
    if state["source_fingerprint"] != manifest["source_fingerprint"]:
        blocked.append("source_fingerprint_changed")
    if state["resolved_config_hash"] != manifest["resolved_config_hash"]:
        blocked.append("resolved_config_changed_since_prepare")

    translation: list[str] = []
    record_only: list[str] = []
    unchanged: list[str] = []
    edit: list[str] = []
    capsules: list[str] = []
    details: dict[str, list[str]] = {}
    ordered = sorted(manifest["units"], key=lambda item: item["order"])
    previous: dict[str, Any] | None = None
    for entry in ordered:
        unit_id = entry["unit_id"]
        record = state["units"].setdefault(unit_id, empty_record())
        capsule_path = safe_project_path(project, entry["capsule_file"])
        capsule = load_json(capsule_path)
        if not str(capsule.get("summary", "")).strip():
            capsules.append(unit_id)
        draft = safe_project_path(project, entry["draft_file"])
        reasons: list[str] = []
        dependencies = _current_dependencies(project, entry, manifest, glossary, previous)
        if record["attempts"] >= max_attempts and record["status"] in {"failed_retryable", "failed_terminal"}:
            reasons.append("attempt_limit_reached")
            blocked.append(f"attempt_limit_reached:{unit_id}")
        elif not draft.is_file() or not draft.read_text(encoding="utf-8").strip():
            reasons.append("missing_or_blank_draft")
            translation.append(unit_id)
        elif not record.get("output_hashes", {}).get("draft"):
            reasons.append("existing_unrecorded_draft")
            record_only.append(unit_id)
        elif record.get("dependency_hashes") != dependencies:
            changed = sorted(
                key for key in dependencies if record.get("dependency_hashes", {}).get(key) != dependencies[key]
            )
            reasons.extend(f"dependency_changed:{key}" for key in changed)
            translation.append(unit_id)
            record["status"] = "invalidated"
            record["reason_codes"] = reasons.copy()
            record["updated_at"] = now_utc()
        elif record["output_hashes"].get("draft") != sha256_file(draft):
            reasons.append("draft_changed_unrecorded")
            record_only.append(unit_id)
        else:
            unchanged.append(unit_id)
        if draft.is_file() and draft.read_text(encoding="utf-8").strip() and require_edit:
            edited = safe_project_path(project, entry["edit_file"])
            if not edited.is_file() or not edited.read_text(encoding="utf-8").strip():
                edit.append(unit_id)
            elif (
                record.get("output_hashes", {}).get("edit") != sha256_file(edited)
                or record.get("output_hashes", {}).get("edit_input_draft") != sha256_file(draft)
            ):
                edit.append(unit_id)
        details[unit_id] = reasons
        previous = entry

    payload = {
        "schema_version": 1,
        "blocked": sorted(set(blocked)),
        "translation_unit_ids": translation,
        "record_only_unit_ids": record_only,
        "unchanged_unit_ids": unchanged,
        "edit_unit_ids": edit,
        "capsule_unit_ids": capsules,
        "details": details,
    }
    if capsules:
        payload["blocked"].append("context_capsules_pending")
    refresh_work_stages(state, require_edit)
    save_state(project, state)
    atomic_write_json(project / "state" / "work-queue.json", payload)
    return payload


def _entry_for(manifest: dict[str, Any], unit_id: str) -> dict[str, Any]:
    try:
        return next(entry for entry in manifest["units"] if entry["unit_id"] == unit_id)
    except StopIteration as exc:
        raise BTPError(f"Unknown work unit: {unit_id}") from exc


def record_text_output(
    project: Path,
    unit_id: str,
    kind: str,
    source: Path | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(project)
    state = load_state(project)
    config, _ = load_resolved_project(project)
    glossary = load_effective_glossary(project, config)
    entry = _entry_for(manifest, unit_id)
    field = "draft_file" if kind == "draft" else "edit_file"
    destination = safe_project_path(project, entry[field])
    if source and source.resolve() != destination.resolve():
        atomic_copy(source, destination)
    ensure_nonblank(destination)
    record = state["units"].setdefault(unit_id, empty_record())
    if kind == "draft":
        ordered = sorted(manifest["units"], key=lambda item: item["order"])
        index = next(index for index, item in enumerate(ordered) if item["unit_id"] == unit_id)
        previous = ordered[index - 1] if index else None
        record["dependency_hashes"] = _current_dependencies(project, entry, manifest, glossary, previous)
        record["attempts"] += 1
        record["status"] = "completed"
    else:
        record["output_hashes"]["edit_input_draft"] = sha256_file(
            safe_project_path(project, entry["draft_file"])
        )
    record["output_hashes"][kind] = sha256_file(destination)
    record["reason_codes"] = []
    record["updated_at"] = now_utc()
    if metrics:
        record["metrics"] = {key: value for key, value in metrics.items() if value is not None}
    if kind == "edit":
        for review in state.get("human_reviews", []):
            if review["checkpoint"] == "publication_signoff" and review.get("status") == "approved":
                review.update({
                    "status": "pending",
                    "notes": "Publication signoff invalidated by a subsequent edited output.",
                    "recorded_at": now_utc(),
                })
    require_edit = bool(config["passes"]["edit"])
    refresh_work_stages(state, require_edit)
    save_state(project, state)
    return record


def record_failure(
    project: Path,
    unit_id: str,
    reason: str,
    max_attempts: int,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = load_state(project)
    if unit_id not in state["units"]:
        raise BTPError(f"Unknown work unit: {unit_id}")
    record = state["units"][unit_id]
    record["attempts"] += 1
    record["status"] = "failed_terminal" if record["attempts"] >= max_attempts else "failed_retryable"
    record["reason_codes"] = [reason]
    record["updated_at"] = now_utc()
    if metrics:
        record["metrics"] = {key: value for key, value in metrics.items() if value is not None}
    require_edit = bool(load_resolved_project(project)[0]["passes"]["edit"])
    refresh_work_stages(state, require_edit)
    save_state(project, state)
    return record


def record_review(project: Path, checkpoint: str, approved: bool, reviewer: str, notes: str) -> None:
    state = load_state(project)
    evidence_hashes: dict[str, str] = {}
    exception_ids: list[str] = []
    if checkpoint == "publication_signoff" and approved:
        from .publication_review import signoff_evidence

        evidence_hashes, exception_ids = signoff_evidence(project)
    found = False
    for item in state.get("human_reviews", []):
        if item["checkpoint"] == checkpoint:
            item.update({
                "status": "approved" if approved else "changes_requested",
                "reviewer": reviewer,
                "recorded_at": now_utc(),
                "notes": notes,
                "evidence_hashes": evidence_hashes,
                "exception_ids": exception_ids,
            })
            found = True
            break
    if not found:
        raise BTPError(f"Checkpoint is not configured: {checkpoint}")
    save_state(project, state)

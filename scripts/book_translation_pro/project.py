"""Project initialization and preparation orchestration."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .artifacts import (
    BTPError,
    atomic_copy,
    atomic_write_json,
    atomic_write_text,
    hash_data,
    load_json,
    normalize_path,
    portable_path,
    safe_project_path,
    sha256_file,
    validate_schema,
)
from .config import resolve_project, write_yaml
from .glossary import empty_glossary, load_effective_glossary, load_glossary, render_review, save_glossary
from .ingest import SUPPORTED_SUFFIXES, convert_to_markdown, source_format
from .manifest import create_manifest
from .state import empty_record, initialize_state, load_state, now_utc, save_state
from .structure import draft_book_map, expand_long_segments, make_work_units, parse_blocks, profile_markdown
from .visual import augment_markdown_with_scanned_page_ocr, prepare_visual


PROJECT_DIRS = (
    "source",
    "analysis",
    "terminology",
    "config",
    "source-work/extracted",
    "source-work/ocr",
    "source-work/work-units",
    "target/draft",
    "target/edited",
    "context/capsules",
    "assets/original",
    "assets/page-images",
    "assets/display-crops",
    "state/observations",
    "state/checkpoints",
    "state/logs",
    "qa",
    "qa/contact-sheets",
    "qa/visual-reviews",
    "dist",
)


def _skill_root() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "SKILL.md").is_file():
        return source_root
    installed_root = Path(sys.prefix) / "share" / "book-translation-pro"
    if (installed_root / "SKILL.md").is_file():
        return installed_root
    raise BTPError("Book Translation Pro skill assets are missing from this installation")


def _slug(value: str, fallback_hash: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-._")
    return text[:64] or f"book-{fallback_hash[:8]}"


def initialize_project(
    source: Path,
    project: Path,
    target_language: str,
    source_language: str,
    mode: str,
    outputs: list[str],
    project_id: str | None = None,
    style_template: str = "general",
) -> dict[str, Any]:
    source = normalize_path(source)
    if not source.is_file():
        raise BTPError(f"Source file does not exist: {source}")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise BTPError("Book Translation Pro supports PDF, DOCX, EPUB, and CHM source files")
    project = normalize_path(project)
    if project.exists() and any(project.iterdir()):
        raise BTPError(f"Project directory is not empty: {project}")
    project.mkdir(parents=True, exist_ok=True)
    for relative in PROJECT_DIRS:
        (project / relative).mkdir(parents=True, exist_ok=True)

    source_hash = sha256_file(source)
    local_source = project / "source" / f"original{suffix}"
    atomic_copy(source, local_source)
    config = {
        "schema_version": 2,
        "project_id": project_id or _slug(source.stem, source_hash),
        "source": {
            "path": local_source.relative_to(project).as_posix(),
            "language": source_language,
            "format": source_format(local_source),
            "edition": "",
        },
        "target": {"language": target_language},
        "mode": mode,
        "outputs": outputs,
        "workspace": ".",
        "metadata": {"title": source.stem, "author": "", "translator": ""},
        "terminology": {"libraries": []},
        "style_guide": {"template": style_template},
        "custom_instructions": [],
    }
    write_yaml(project / "book-project.yaml", config)
    templates = {
        "general": _skill_root() / "assets" / "project-template" / "config" / "style-guide.md",
        "academic": _skill_root() / "assets" / "style-guides" / "academic.md",
        "technical": _skill_root() / "assets" / "style-guides" / "technical.md",
        "business": _skill_root() / "assets" / "style-guides" / "business.md",
    }
    if style_template not in templates:
        raise BTPError(f"Unknown Style Guide template: {style_template}")
    template = templates[style_template]
    atomic_copy(template, project / "config" / "style-guide.md")
    fingerprint = {
        "schema_version": 2,
        "algorithm": "sha256",
        "sha256": source_hash,
        "bytes": local_source.stat().st_size,
        "original_filename": source.name,
        "project_path": local_source.relative_to(project).as_posix(),
    }
    atomic_write_json(project / "source" / "source-fingerprint.json", fingerprint)
    resolved, config_hash = resolve_project(project)
    return {"project": str(project), "project_id": resolved["project_id"], "source_fingerprint": source_hash, "resolved_config_hash": config_hash}


def _clear_derived(project: Path) -> None:
    for relative in ("analysis", "source-work", "target", "context", "state", "qa", "dist"):
        path = safe_project_path(project, relative)
        if path.exists():
            portable_path(path, project)
            shutil.rmtree(path)
    for relative in ("assets/page-images", "assets/display-crops"):
        path = safe_project_path(project, relative)
        if path.exists():
            portable_path(path, project)
            shutil.rmtree(path)
    for relative in PROJECT_DIRS:
        (project / relative).mkdir(parents=True, exist_ok=True)


def _source_fingerprint(project: Path, config: dict[str, Any]) -> str:
    fingerprint = load_json(project / "source" / "source-fingerprint.json")
    source = safe_project_path(project, config["source"]["path"])
    actual = sha256_file(source)
    if fingerprint.get("sha256") != actual:
        raise BTPError("Source bytes changed after project initialization; create a new project or rebuild explicitly")
    return actual


def _write_segments(project: Path, segments: list[Any]) -> None:
    lines = []
    for item in segments:
        lines.append(json.dumps({
            "id": item.id,
            "order": item.order,
            "kind": item.kind,
            "heading_level": item.heading_level,
            "title": item.title,
            "book_node_id": item.book_node_id,
            "text": item.text,
        }, ensure_ascii=False, sort_keys=True))
    atomic_write_text(project / "source-work" / "segments.jsonl", "\n".join(lines) + "\n")


def _bootstrap_samples(unit_ids: list[str], count: int) -> list[str]:
    if len(unit_ids) <= count:
        return unit_ids
    if count == 1:
        return [unit_ids[0]]
    indexes = sorted({round(index * (len(unit_ids) - 1) / (count - 1)) for index in range(count)})
    return [unit_ids[index] for index in indexes]


def prepare_project(
    project: Path, rebuild: bool = False, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    project = normalize_path(project)
    resolved, config_hash = resolve_project(project, overrides)
    fingerprint = _source_fingerprint(project, resolved)
    manifest_path = project / "state" / "manifest.json"
    if manifest_path.is_file() and not rebuild:
        existing = load_json(manifest_path)
        if existing.get("source_fingerprint") == fingerprint and existing.get("resolved_config_hash") == config_hash:
            return {"reused": True, "unit_count": len(existing.get("units", [])), "manifest": str(manifest_path)}
        raise BTPError("Prepared artifacts do not match source/config; rerun prepare with --rebuild")
    if rebuild:
        _clear_derived(project)

    markdown_path, conversion = convert_to_markdown(project, resolved)
    fmt = source_format(safe_project_path(project, resolved["source"]["path"]), resolved["source"].get("format", "auto"))
    ocr_preflight = augment_markdown_with_scanned_page_ocr(project, resolved, markdown_path) if fmt == "pdf" else {"applicable": False}
    markdown = markdown_path.read_text(encoding="utf-8")
    profile = profile_markdown(markdown, fmt, safe_project_path(project, resolved["source"]["path"]).stat().st_size)
    profile["conversion"] = conversion
    profile["ocr_preflight"] = ocr_preflight
    atomic_write_json(project / "analysis" / "book-profile.json", profile)

    segments = expand_long_segments(parse_blocks(markdown), resolved["chunking"]["hard_max_characters"])
    if not segments:
        raise BTPError("No structural segments were extracted from the source")
    book_map, assigned = draft_book_map(segments, fingerprint)
    validate_schema(book_map, "book-map.schema.json")
    atomic_write_json(project / "analysis" / "book-map.json", book_map)
    _write_segments(project, assigned)

    chunking = resolved["chunking"]
    units = make_work_units(assigned, chunking["target_characters"], chunking["hard_max_characters"])
    for unit in units:
        atomic_write_text(project / "source-work" / "work-units" / f"{unit['id']}.md", unit.pop("text"))

    glossary_path = project / "terminology" / "glossary.json"
    if glossary_path.is_file():
        glossary = load_glossary(project)
    else:
        glossary = empty_glossary()
        save_glossary(project, glossary)
    effective_glossary = load_effective_glossary(project, resolved)
    atomic_write_text(project / "terminology" / "glossary-review.md", render_review(effective_glossary))

    exceptions_path = project / "qa" / "exceptions.json"
    if not exceptions_path.is_file():
        from .exceptions import empty_exceptions

        atomic_write_json(exceptions_path, empty_exceptions())

    sample_ids = _bootstrap_samples([unit["id"] for unit in units], resolved["terminology"]["bootstrap_samples"])
    atomic_write_json(project / "terminology" / "bootstrap-samples.json", {"schema_version": 1, "unit_ids": sample_ids})

    for index, unit in enumerate(units):
        capsule = {
            "schema_version": 1,
            "unit_id": unit["id"],
            "book_node_id": unit["book_node_id"],
            "incoming_capsule_ids": [units[index - 1]["id"]] if index else [],
            "summary": "",
            "continuity": [],
            "entities": [],
            "cross_references": [],
            "visual_continuations": [],
            "unresolved": [],
        }
        validate_schema(capsule, "context-capsule.schema.json")
        atomic_write_json(project / "context" / "capsules" / f"{unit['id']}.json", capsule)

    units, book_map, visual_summary = prepare_visual(project, resolved, units, book_map)
    profile["visual"] = visual_summary
    atomic_write_json(project / "analysis" / "book-profile.json", profile)

    style_hash = sha256_file(project / "config" / "style-guide.md")
    page_map_path = project / "analysis" / "page-map.json"
    page_map = load_json(page_map_path) if page_map_path.is_file() else None
    manifest = create_manifest(project, units, fingerprint, config_hash, book_map, style_hash, page_map)
    checkpoints = resolved["execution"].get("checkpoints", [])
    initialize_state(project, resolved["project_id"], fingerprint, config_hash, manifest, checkpoints, visual_summary)
    return {
        "reused": False,
        "unit_count": len(units),
        "segment_count": len(assigned),
        "book_node_count": len(book_map["nodes"]),
        "bootstrap_sample_ids": sample_ids,
        "warnings": profile["warnings"],
        "visual": visual_summary,
    }


def visualize_project(project: Path) -> dict[str, Any]:
    """Add or refresh V1.0 visual evidence for an already prepared project."""
    project = normalize_path(project)
    resolved, config_hash = resolve_project(project)
    fingerprint = _source_fingerprint(project, resolved)
    manifest = load_json(project / "state" / "manifest.json")
    book_map = load_json(project / "analysis" / "book-map.json")
    existing_page_map_path = project / "analysis" / "page-map.json"
    previous_page_map_hash = hash_data(load_json(existing_page_map_path)) if existing_page_map_path.is_file() else None
    units = [
        {
            "id": entry["unit_id"],
            "order": entry["order"],
            "book_node_id": entry["book_node_id"],
            "segment_ids": entry["segment_ids"],
        }
        for entry in manifest["units"]
    ]
    units, book_map, summary = prepare_visual(project, resolved, units, book_map)
    page_map_path = project / "analysis" / "page-map.json"
    page_map = load_json(page_map_path) if page_map_path.is_file() else None
    page_map_changed = (hash_data(page_map) if page_map is not None else None) != previous_page_map_hash
    updated_manifest = create_manifest(
        project, units, fingerprint, config_hash, book_map,
        sha256_file(project / "config" / "style-guide.md"), page_map,
    )
    state = load_state(project)
    state["resolved_config_hash"] = config_hash
    state["stages"].setdefault("visual", empty_record())
    state["stages"]["visual"].update({
        "status": "completed" if summary.get("applicable") else "skipped_approved",
        "attempts": state["stages"]["visual"].get("attempts", 0) + 1,
        "output_hashes": {"page_map": sha256_file(page_map_path)} if page_map_path.is_file() else {},
        "reason_codes": [],
        "updated_at": now_utc(),
    })
    if page_map_changed:
        for name in ("qa", "build"):
            state["stages"][name].update({"status": "invalidated", "reason_codes": ["visual_map_changed"], "updated_at": now_utc()})
    pending_pages = summary.get("required_review_pages", [])
    reviewed_scope = [
        page["page_index"] for page in (page_map or {}).get("pages", [])
        if page["risk"] >= resolved["visual"]["minimum_review_risk"]
    ]
    review_status = "pending" if pending_pages else "approved" if reviewed_scope else "not_required"
    review_notes = (
        f"Pending required page reviews: {pending_pages}" if pending_pages
        else f"All required page reviews retained: {reviewed_scope}" if reviewed_scope
        else "No page meets the mode review threshold."
    )
    found = False
    for review in state.get("human_reviews", []):
        if review["checkpoint"] == "visual_exceptions":
            found = True
            review.update({
                "status": review_status,
                "notes": review_notes,
                "recorded_at": now_utc(),
            })
    if not found and "visual_exceptions" in resolved["execution"].get("checkpoints", []):
        state.setdefault("human_reviews", []).append({
            "checkpoint": "visual_exceptions",
            "status": review_status,
            "recorded_at": now_utc(),
            "notes": review_notes,
        })
    state["units"] = {entry["unit_id"]: state["units"].get(entry["unit_id"], empty_record()) for entry in updated_manifest["units"]}
    save_state(project, state)
    return {**summary, "page_map_changed": page_map_changed}

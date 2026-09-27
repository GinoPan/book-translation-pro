"""Project configuration loading, mode defaults, and deterministic resolution."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .artifacts import BTPError, atomic_write_json, atomic_write_text, hash_data, load_json, validate_schema
from .migration import CURRENT_CONFIG_SCHEMA, migrate_config_data
from .rights import DEFAULT_RIGHTS


COMMON_DEFAULTS: dict[str, Any] = {
    "rights": DEFAULT_RIGHTS,
    "execution": {
        "runtime_adapter": "auto",
        "max_parallel": 8,
        "max_attempts_per_unit": 2,
        "keep_intermediates": True,
        "checkpoints": [],
    },
    "chunking": {
        "target_characters": 6000,
        "hard_max_characters": 9000,
        "prefer_section_boundaries": True,
    },
    "terminology": {
        "bootstrap_samples": 7,
        "high_frequency_limit": 20,
        "require_review_before_translation": False,
        "libraries": [],
    },
    "style_guide": {"template": "general"},
    "context_capsule": {"enabled": True, "max_characters": 1800},
    "passes": {"edit": True, "visual_qa": True, "completeness_qa": True},
    "visual": {
        "renderer": "auto",
        "render_dpi": 180,
        "minimum_review_risk": 2,
        "minimum_text_characters": 20,
        "thumbnail_width": 240,
        "ocr_mode": "auto",
        "ocr_languages": [],
        "embedded_text": "review",
    },
    "resume": {"enabled": True, "hash_algorithm": "sha256"},
    "publishing": {
        "clean_extraction_artifacts": True,
        "trim_width_mm": 170,
        "trim_height_mm": 240,
        "inside_margin_mm": 25,
        "outside_margin_mm": 20,
        "top_margin_mm": 22,
        "bottom_margin_mm": 22,
        "gutter_mm": 5,
        "body_font_east_asia": "宋体",
        "body_font_latin": "Times New Roman",
        "heading_font_east_asia": "黑体",
        "heading_font_latin": "Arial",
        "body_font_size_pt": 10.5,
        "line_spacing": 1.45,
        "toc_depth": 2,
        "bilingual_formats": ["markdown", "docx", "epub"],
    },
    "custom_instructions": [],
}


MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "fast": {
        "execution": {"checkpoints": []},
        "terminology": {"bootstrap_samples": 3, "require_review_before_translation": False},
        "passes": {"edit": False, "visual_qa": True},
        "visual": {"minimum_review_risk": 3},
    },
    "study": {
        "execution": {"checkpoints": ["terminology", "visual_exceptions"]},
        "terminology": {"bootstrap_samples": 7, "require_review_before_translation": True},
        "passes": {"edit": True, "visual_qa": True},
        "visual": {"minimum_review_risk": 2},
    },
    "publication": {
        "execution": {
            "checkpoints": ["terminology", "visual_exceptions", "publication_signoff"]
        },
        "terminology": {"bootstrap_samples": 12, "require_review_before_translation": True},
        "passes": {"edit": True, "visual_qa": True},
        "visual": {"minimum_review_risk": 0},
    },
}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BTPError(f"Project configuration not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise BTPError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BTPError(f"Project configuration must be a mapping: {path}")
    return value


def write_yaml(path: Path, value: dict[str, Any]) -> None:
    text = yaml.safe_dump(value, allow_unicode=True, sort_keys=False, width=100)
    atomic_write_text(path, text)


def resolve_config(raw: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = migrate_config_data(raw)
    mode = (overrides or {}).get("mode", raw.get("mode", "study"))
    if mode not in MODE_DEFAULTS:
        raise BTPError(f"Unknown mode: {mode}")
    resolved = deep_merge(COMMON_DEFAULTS, MODE_DEFAULTS[mode])
    resolved = deep_merge(resolved, raw)
    if overrides:
        resolved = deep_merge(resolved, overrides)
    resolved["schema_version"] = CURRENT_CONFIG_SCHEMA
    validate_schema(resolved, "project-config.schema.json")
    return resolved


def resolve_project(project: Path, overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    raw = load_yaml(project / "book-project.yaml")
    resolved = resolve_config(raw, overrides)
    digest = hash_data(resolved)
    atomic_write_json(project / "resolved-config.json", resolved)
    atomic_write_text(project / "resolved-config.sha256", digest + "\n")
    return resolved, digest


def load_resolved_project(project: Path) -> tuple[dict[str, Any], str]:
    resolved = load_json(project / "resolved-config.json")
    if resolved.get("schema_version", 1) != CURRENT_CONFIG_SCHEMA:
        raise BTPError(
            f"Project configuration schema v{resolved.get('schema_version', 1)} requires migration; "
            "run `btp migrate <project>`"
        )
    validate_schema(resolved, "project-config.schema.json")
    digest = hash_data(resolved)
    recorded_path = project / "resolved-config.sha256"
    if not recorded_path.is_file() or recorded_path.read_text(encoding="utf-8").strip() != digest:
        raise BTPError("resolved-config.json does not match resolved-config.sha256; rerun prepare")
    return resolved, digest

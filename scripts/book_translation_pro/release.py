"""Deterministic release-contract self-check independent of a book project."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import jsonschema

from . import __version__
from .artifacts import BTPError, load_json, schema_path
from .runtime import doctor_report
from .runtime_adapters import conformance_cases


REQUIRED_SCHEMAS = (
    "project-config.schema.json",
    "book-map.schema.json",
    "page-map.schema.json",
    "visual-review.schema.json",
    "glossary.schema.json",
    "context-capsule.schema.json",
    "manifest.schema.json",
    "run-state.schema.json",
    "segment-alignment.schema.json",
    "qa-exceptions.schema.json",
    "publication-review.schema.json",
)


def _schemas_valid() -> bool:
    """Every shipped schema is itself a valid Draft 2020-12 schema."""
    try:
        for name in REQUIRED_SCHEMAS:
            jsonschema.Draft202012Validator.check_schema(load_json(schema_path(name)))
    except (BTPError, jsonschema.SchemaError):
        return False
    return True


def _template_resolves() -> bool:
    """The shipped project template resolves and validates in every mode."""
    from .config import MODE_DEFAULTS, load_yaml, resolve_config
    from .project import _skill_root

    try:
        raw = load_yaml(_skill_root() / "assets" / "project-template" / "book-project.yaml")
        for mode in MODE_DEFAULTS:
            resolve_config(raw, {"mode": mode})
    except BTPError:
        return False
    return True


def release_check() -> dict[str, Any]:
    source_root = Path(__file__).resolve().parents[2]
    pyproject = source_root / "pyproject.toml"
    project_version = __version__
    if pyproject.is_file():
        project_version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    checks = {
        "version_consistent": project_version == __version__,
        "schemas_available": all(schema_path(name).is_file() for name in REQUIRED_SCHEMAS),
        "schemas_valid": _schemas_valid(),
        "project_template_resolves": _template_resolves(),
        "runtime_contracts": conformance_cases()["passed"],
    }
    host = doctor_report("publication")
    return {
        "release": "book-translation-pro",
        "version": __version__,
        "contract_ready": all(checks.values()),
        "checks": checks,
        "host_publication_ready": host["requested_mode_ready"],
        "host_limitations": [
            name for name, item in host["binaries"].items()
            if name in {"pandoc", "ebook-convert", "epubcheck", "word", "soffice"} and not item["found"]
        ],
    }

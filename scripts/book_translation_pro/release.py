"""Deterministic release-contract self-check independent of a book project."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import schema_path
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


def release_check() -> dict[str, Any]:
    source_root = Path(__file__).resolve().parents[2]
    pyproject = source_root / "pyproject.toml"
    project_version = __version__
    if pyproject.is_file():
        project_version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    checks = {
        "version_consistent": project_version == __version__,
        "schemas_available": all(schema_path(name).is_file() for name in REQUIRED_SCHEMAS),
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

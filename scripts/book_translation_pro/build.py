"""Verified publication-oriented Markdown, DOCX, EPUB, and PDF construction."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_copy, atomic_write_json, atomic_write_text, load_json, portable_path, safe_project_path, sha256_file
from .publication import typeset_outputs, validate_publication_outputs
from .qa import run_qa
from .state import load_state, now_utc, save_state


def _verify_zip(path: Path, required_suffix: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise BTPError(f"Build output is missing or empty: {path}")
    if not zipfile.is_zipfile(path):
        raise BTPError(f"Build output is not a valid ZIP package: {path}")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None or not any(name.endswith(required_suffix) for name in archive.namelist()):
            raise BTPError(f"Build package failed integrity checks: {path}")


def _verify_pdf(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise BTPError(f"Build output is missing or empty: {path}")
    try:
        import fitz

        with fitz.open(path) as document:
            if len(document) < 1:
                raise BTPError(f"PDF has no pages: {path}")
    except ImportError:
        if not path.read_bytes().startswith(b"%PDF"):
            raise BTPError(f"Build output is not a PDF: {path}")


def _verify_generated(generated: dict[str, Path]) -> None:
    for name, path in generated.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise BTPError(f"Build output is missing or empty: {path}")
        kind = name.removeprefix("bilingual_")
        if kind == "docx":
            _verify_zip(path, "document.xml")
        elif kind == "epub":
            _verify_zip(path, ".opf")
        elif kind == "pdf":
            _verify_pdf(path)


def typeset_preview(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Create a review candidate without satisfying or bypassing the build gate."""
    output_dir = project / "dist" / "typeset-preview"
    generated = typeset_outputs(project, config, output_dir)
    _verify_generated(generated)
    validation = validate_publication_outputs(generated, config, project)
    if not validation["passed"]:
        raise BTPError("Publication candidate failed format validation: " + "; ".join(validation["errors"]))
    report = {
        "schema_version": 1,
        "built_at": now_utc(),
        "status": "review_candidate",
        "final_build_gate_satisfied": False,
        "clean_master_sha256": sha256_file(generated["markdown"]),
        "publication_validation": validation,
        "generated": {
            name: {"path": portable_path(value, project), "sha256": sha256_file(value)}
            for name, value in generated.items()
        },
    }
    atomic_write_json(project / "qa" / "typeset-preview.json", report)
    if config["mode"] == "publication":
        from .publication_review import create_review_template

        create_review_template(project)
    return report


def _promote_reviewed_candidate(project: Path, dist: Path) -> dict[str, Path]:
    candidate = load_json(project / "qa" / "typeset-preview.json")
    promoted: dict[str, Path] = {}
    for name, item in candidate.get("generated", {}).items():
        source = safe_project_path(project, item["path"])
        if sha256_file(source) != item["sha256"]:
            raise BTPError(f"Reviewed publication candidate changed after signoff: {name}")
        if name.removeprefix("bilingual_") == "xhtml":
            promoted[name] = source
            continue
        destination = dist / source.name
        if source.resolve() != destination.resolve():
            atomic_copy(source, destination)
        promoted[name] = destination
    return promoted


def build_outputs(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    qa = run_qa(project, config)
    if not qa["passed"]:
        raise BTPError(f"Completeness QA failed with {qa['failure_count']} blocking issue(s)")
    dist = project / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    generated = (
        _promote_reviewed_candidate(project, dist)
        if config["mode"] == "publication"
        else typeset_outputs(project, config, dist)
    )
    _verify_generated(generated)
    validation = validate_publication_outputs(generated, config, project)
    if not validation["passed"]:
        raise BTPError("Publication output failed format validation: " + "; ".join(validation["errors"]))

    report = {
        "schema_version": 1,
        "built_at": now_utc(),
        "clean_master_sha256": sha256_file(generated["markdown"]),
        "publication_validation": validation,
        "generated": {
            name: {"path": portable_path(value, project), "sha256": sha256_file(value)}
            for name, value in generated.items()
        },
        "qa_report": "qa/completeness.json",
        "qa_score": qa.get("score"),
        "metrics": qa.get("metrics", {}),
        "approved_exception_ids": qa.get("approved_exception_ids", []),
    }
    if config["mode"] == "publication":
        report["publication_signoff"] = next(
            item for item in load_state(project).get("human_reviews", [])
            if item["checkpoint"] == "publication_signoff"
        )
    atomic_write_json(project / "qa" / "build.json", report)
    lines = ["# Build Report", "", f"Built: {report['built_at']}", "", "| Format | File | SHA-256 |", "|---|---|---|"]
    for name, item in report["generated"].items():
        lines.append(f"| {name} | {item['path']} | `{item['sha256']}` |")
    atomic_write_text(project / "qa" / "build.md", "\n".join(lines) + "\n")

    state = load_state(project)
    stage = state["stages"]["build"]
    stage.update({
        "status": "completed",
        "attempts": stage["attempts"] + 1,
        "dependency_hashes": {"qa": sha256_file(project / "qa" / "completeness.json")},
        "output_hashes": {name: item["sha256"] for name, item in report["generated"].items()},
        "reason_codes": [],
        "updated_at": now_utc(),
    })
    save_state(project, state)
    return report

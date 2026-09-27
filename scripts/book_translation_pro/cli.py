"""Command-line interface for Book Translation Pro."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import BTPError, atomic_copy, load_json, normalize_path, safe_project_path, validate_schema
from .build import build_outputs, typeset_preview
from .config import load_resolved_project
from .figures import normalize_image_backgrounds, reconstruct_figures
from .exceptions import record_exceptions
from .glossary import load_glossary, save_glossary, select_entries, validate_glossary
from .manifest import load_manifest
from .migration import migrate_project
from .project import initialize_project, prepare_project, visualize_project
from .rights import INTENDED_USES, RIGHTS_STATUSES, rights_config, rights_warning
from .publication import publication_table_inventory
from .publication_review import create_review_template, record_publication_review
from .qa import run_qa
from .release import release_check
from .runtime import doctor_report
from .runtime_adapters import conformance_cases
from .state import (
    load_state,
    plan_run,
    record_failure,
    record_review,
    record_text_output,
)
from .visual import record_visual_review, show_page


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _project(value: str) -> Path:
    return normalize_path(Path(value))


def _config_overrides(args: argparse.Namespace) -> dict[str, Any] | None:
    overlay: dict[str, Any] = {}
    if getattr(args, "mode", None):
        overlay["mode"] = args.mode
    if getattr(args, "target", None):
        overlay["target"] = {"language": args.target}
    if getattr(args, "outputs", None):
        overlay["outputs"] = args.outputs
    return overlay or None


def _entry(manifest: dict[str, Any], unit_id: str) -> dict[str, Any]:
    for item in manifest["units"]:
        if item["unit_id"] == unit_id:
            return item
    raise BTPError(f"Unknown work unit: {unit_id}")


def _record_json(args: argparse.Namespace) -> dict[str, Any]:
    project = _project(args.project)
    manifest = load_manifest(project)
    if args.kind == "glossary":
        value = load_json(Path(args.input).resolve())
        validate_glossary(value)
        save_glossary(project, value)
        return {"recorded": "glossary", "entry_count": len(value["entries"])}
    if not args.unit:
        raise BTPError(f"--unit is required for record kind {args.kind}")
    entry = _entry(manifest, args.unit)
    source = Path(args.input).resolve()
    value = load_json(source)
    if args.kind == "capsule":
        validate_schema(value, "context-capsule.schema.json")
        if value["unit_id"] != args.unit:
            raise BTPError("Capsule unit_id does not match --unit")
        if not value["summary"].strip():
            raise BTPError("A finalized Context Capsule must have a nonblank summary")
        destination = safe_project_path(project, entry["capsule_file"])
    else:
        if value.get("schema_version") != 1:
            raise BTPError("Observation schema_version must be 1")
        for field in ("new_entries", "conflicts", "source_issues", "used_glossary_ids"):
            if not isinstance(value.get(field, []), list):
                raise BTPError(f"Observation field {field} must be an array")
        destination = safe_project_path(project, entry["observation_file"])
    if source != destination.resolve():
        atomic_copy(source, destination)
    return {"recorded": args.kind, "unit_id": args.unit, "path": str(destination)}


def show_unit(project: Path, unit_id: str, phase: str) -> dict[str, Any]:
    config, _ = load_resolved_project(project)
    manifest = load_manifest(project)
    entry = _entry(manifest, unit_id)
    source_path = safe_project_path(project, entry["source_file"])
    source_text = source_path.read_text(encoding="utf-8")
    ordered = sorted(manifest["units"], key=lambda item: item["order"])
    index = next(index for index, item in enumerate(ordered) if item["unit_id"] == unit_id)
    incoming = None
    if index:
        incoming = load_json(safe_project_path(project, ordered[index - 1]["capsule_file"]))
    glossary = load_glossary(project)
    selected = select_entries(glossary, source_text, entry["book_node_id"])
    payload: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "unit_id": unit_id,
        "target_language": config["target"]["language"],
        "book_node_id": entry["book_node_id"],
        "source_file": str(source_path),
        "style_guide_file": str(project / "config" / "style-guide.md"),
        "incoming_capsule": incoming,
        "glossary_entries": selected,
        "custom_instructions": config.get("custom_instructions", []),
        "rights": rights_config(config),
        "data_handling": (
            "Keep source content local and do not send it to an external service "
            "unless rights.source_upload_allowed is true."
        ),
        "source_page_indices": entry.get("page_indices", []),
        "visual_evidence": [],
        "expected": {
            "draft_file": str(safe_project_path(project, entry["draft_file"])),
            "edit_file": str(safe_project_path(project, entry["edit_file"])),
            "capsule_file": str(safe_project_path(project, entry["capsule_file"])),
            "observation_file": str(safe_project_path(project, entry["observation_file"])),
        },
    }
    page_map_path = project / "analysis" / "page-map.json"
    if page_map_path.is_file():
        page_map = load_json(page_map_path)
        wanted = set(entry.get("page_indices", []))
        payload["visual_evidence"] = [
            {
                "page_index": page["page_index"],
                "image": str(safe_project_path(project, page["image_ref"])),
                "layout": page["layout"],
                "risk": page["risk"],
                "crops": [str(safe_project_path(project, region["asset_ref"])) for region in page["regions"] if region.get("asset_ref")],
            }
            for page in page_map["pages"] if page["page_index"] in wanted
        ]
    if phase == "edit":
        payload["draft_file"] = str(safe_project_path(project, entry["draft_file"]))
    return payload


def status_report(project: Path) -> dict[str, Any]:
    config, digest = load_resolved_project(project)
    state = load_state(project)
    manifest = load_manifest(project)
    counts: dict[str, int] = {}
    for record in state["units"].values():
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    qa_path = project / "qa" / "completeness.json"
    build_path = project / "qa" / "build.json"
    visual_path = project / "qa" / "visual.json"
    report = {
        "schema_version": 1,
        "project_id": config["project_id"],
        "mode": config["mode"],
        "resolved_config_hash": digest,
        "unit_count": len(manifest["units"]),
        "unit_status_counts": counts,
        "stages": state["stages"],
        "human_reviews": state.get("human_reviews", []),
        "qa": load_json(qa_path) if qa_path.is_file() else None,
        "visual_qa": load_json(visual_path) if visual_path.is_file() else None,
        "build": load_json(build_path) if build_path.is_file() else None,
    }
    report["rights"] = rights_config(config)
    warning = rights_warning(config)
    if warning:
        report["rights_warning"] = warning
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btp", description="Book Translation Pro V1.0")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="Check local dependencies")
    p_doctor.add_argument("--mode", choices=("fast", "study", "publication"), default="study")
    p_doctor.set_defaults(handler=lambda args: doctor_report(args.mode))

    p_runtime = sub.add_parser("runtime-conformance", help="Verify the four portable runtime profiles")
    p_runtime.set_defaults(handler=lambda args: conformance_cases())

    p_release = sub.add_parser("release-check", help="Verify the installed 1.0 release contracts")
    p_release.set_defaults(handler=lambda args: release_check())

    p_init = sub.add_parser("init", help="Create a translation project")
    p_init.add_argument("source")
    p_init.add_argument("--project", required=True)
    p_init.add_argument("--target", required=True)
    p_init.add_argument("--source-language", default="auto")
    p_init.add_argument("--mode", choices=("fast", "study", "publication"), default="study")
    p_init.add_argument("--output", dest="outputs", action="append", choices=("markdown", "docx", "epub", "pdf", "bilingual"))
    p_init.add_argument("--project-id")
    p_init.add_argument("--style-template", choices=("general", "academic", "technical", "business"), default="general")
    p_init.add_argument("--rights-status", choices=RIGHTS_STATUSES, default="unknown")
    p_init.add_argument("--rights-basis", default="")
    p_init.add_argument("--intended-use", choices=INTENDED_USES)
    p_init.add_argument("--redistribution-allowed", action="store_true")
    p_init.add_argument("--source-upload-allowed", action="store_true")
    p_init.add_argument("--rights-attribution", default="")
    p_init.set_defaults(handler=lambda args: initialize_project(
        Path(args.source), Path(args.project), args.target, args.source_language, args.mode,
        args.outputs or ["docx", "epub", "pdf"], args.project_id, args.style_template,
        args.rights_status, args.rights_basis, args.intended_use,
        args.redistribution_allowed, args.source_upload_allowed, args.rights_attribution,
    ))

    p_prepare = sub.add_parser("prepare", help="Extract, map, chunk, and initialize state")
    p_prepare.add_argument("project")
    p_prepare.add_argument("--rebuild", action="store_true")
    p_prepare.add_argument("--mode", choices=("fast", "study", "publication"))
    p_prepare.add_argument("--target")
    p_prepare.add_argument("--output", dest="outputs", action="append", choices=("markdown", "docx", "epub", "pdf", "bilingual"))
    p_prepare.set_defaults(handler=lambda args: prepare_project(_project(args.project), args.rebuild, _config_overrides(args)))

    p_visualize = sub.add_parser("visualize", help="Create or refresh Page Map and visual evidence")
    p_visualize.add_argument("project")
    p_visualize.set_defaults(handler=lambda args: visualize_project(_project(args.project)))

    p_reconstruct_figures = sub.add_parser(
        "reconstruct-figures",
        help="Create deterministic source-page crops and repair mapped target figure markup",
    )
    p_reconstruct_figures.add_argument("project")
    p_reconstruct_figures.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing exhibit crops; the default preserves reviewed assets",
    )
    p_reconstruct_figures.add_argument(
        "--keys",
        default="",
        help="Comma-separated exhibit keys (for example 6.8,6.9) to force-regenerate; other assets are left untouched",
    )
    p_reconstruct_figures.set_defaults(
        handler=lambda args: reconstruct_figures(
            _project(args.project),
            load_resolved_project(_project(args.project))[0],
            only_missing=not args.overwrite,
            keys=[key.strip() for key in args.keys.split(",") if key.strip()] or None,
        )
    )

    p_normalize_images = sub.add_parser(
        "normalize-images",
        help="Flip inverted dark-background line-art scans back to dark-on-white",
    )
    p_normalize_images.add_argument("project")
    p_normalize_images.set_defaults(
        handler=lambda args: normalize_image_backgrounds(
            _project(args.project),
            load_resolved_project(_project(args.project))[0],
        )
    )

    p_plan = sub.add_parser("plan", help="Plan translation, recording, and editing work")
    p_plan.add_argument("project")
    p_plan.set_defaults(handler=lambda args: plan_run(
        _project(args.project),
        load_resolved_project(_project(args.project))[0]["passes"]["edit"],
        load_resolved_project(_project(args.project))[0]["execution"]["max_attempts_per_unit"],
    ))

    p_show = sub.add_parser("show-unit", help="Show a runtime-neutral work-item package")
    p_show.add_argument("project")
    p_show.add_argument("unit")
    p_show.add_argument("--phase", choices=("capsule", "translate", "edit"), default="translate")
    p_show.set_defaults(handler=lambda args: show_unit(_project(args.project), args.unit, args.phase))

    p_show_page = sub.add_parser("show-page", help="Show one page evidence package and review file")
    p_show_page.add_argument("project")
    p_show_page.add_argument("page", type=int)
    p_show_page.set_defaults(handler=lambda args: show_page(_project(args.project), args.page))

    p_record = sub.add_parser("record", help="Validate and record a worker result or checkpoint")
    p_record.add_argument("project")
    p_record.add_argument("--kind", required=True, choices=("draft", "edit", "capsule", "observation", "glossary", "visual-review", "publication-review", "exceptions", "review", "failure"))
    p_record.add_argument("--unit")
    p_record.add_argument("--input")
    p_record.add_argument("--checkpoint")
    p_record.add_argument("--approve", action="store_true")
    p_record.add_argument("--reviewer", default="user")
    p_record.add_argument("--notes", default="")
    p_record.add_argument("--reason", default="worker_failed")
    p_record.add_argument("--duration-seconds", type=float)
    p_record.add_argument("--input-tokens", type=int)
    p_record.add_argument("--output-tokens", type=int)
    p_record.add_argument("--cost", type=float)
    p_record.add_argument("--currency", default="USD")

    def record_handler(args: argparse.Namespace) -> Any:
        project = _project(args.project)
        config, _ = load_resolved_project(project)
        metrics = {
            "duration_seconds": args.duration_seconds,
            "input_tokens": args.input_tokens,
            "output_tokens": args.output_tokens,
            "cost": args.cost,
            "currency": args.currency.upper() if args.cost is not None else None,
        }
        if args.kind in {"draft", "edit"}:
            if not args.unit:
                raise BTPError("--unit is required")
            return record_text_output(project, args.unit, args.kind, Path(args.input) if args.input else None, metrics)
        if args.kind in {"capsule", "observation", "glossary"}:
            if not args.input:
                raise BTPError("--input is required")
            return _record_json(args)
        if args.kind == "visual-review":
            if not args.input:
                raise BTPError("--input is required")
            return record_visual_review(project, Path(args.input).resolve())
        if args.kind == "publication-review":
            if not args.input:
                raise BTPError("--input is required")
            return record_publication_review(project, Path(args.input).resolve())
        if args.kind == "exceptions":
            if not args.input:
                raise BTPError("--input is required")
            return record_exceptions(project, Path(args.input).resolve())
        if args.kind == "review":
            if not args.checkpoint:
                raise BTPError("--checkpoint is required")
            record_review(project, args.checkpoint, args.approve, args.reviewer, args.notes)
            return {"recorded": "review", "checkpoint": args.checkpoint, "approved": args.approve}
        if not args.unit:
            raise BTPError("--unit is required")
        return record_failure(project, args.unit, args.reason, config["execution"]["max_attempts_per_unit"], metrics)

    p_record.set_defaults(handler=record_handler)

    p_qa = sub.add_parser("qa", help="Run completeness and V1.0 visual QA")
    p_qa.add_argument("project")
    p_qa.set_defaults(handler=lambda args: run_qa(_project(args.project), load_resolved_project(_project(args.project))[0]))

    p_build = sub.add_parser("build", help="Build requested output formats after QA")
    p_build.add_argument("project")
    p_build.set_defaults(handler=lambda args: build_outputs(_project(args.project), load_resolved_project(_project(args.project))[0]))

    p_typeset = sub.add_parser(
        "typeset", help="Create a non-final publication-layout review candidate without bypassing QA"
    )
    p_typeset.add_argument("project")
    p_typeset.set_defaults(
        handler=lambda args: typeset_preview(
            _project(args.project), load_resolved_project(_project(args.project))[0]
        )
    )

    p_check_tables = sub.add_parser(
        "check-tables", help="Compare raw HTML/pipe tables with the cleaned publication master"
    )
    p_check_tables.add_argument("project")
    p_check_tables.set_defaults(
        handler=lambda args: publication_table_inventory(
            _project(args.project), load_resolved_project(_project(args.project))[0]
        )
    )

    p_review = sub.add_parser(
        "publication-review", help="Create a final rendered-output review template for the current candidate"
    )
    p_review.add_argument("project")
    p_review.set_defaults(handler=lambda args: create_review_template(_project(args.project)))

    p_migrate = sub.add_parser("migrate", help="Migrate a pre-1.0 project to the current contracts")
    p_migrate.add_argument("project")
    p_migrate.set_defaults(handler=lambda args: migrate_project(_project(args.project)))

    p_status = sub.add_parser("status", help="Show project state")
    p_status.add_argument("project")
    p_status.set_defaults(handler=lambda args: status_report(_project(args.project)))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        _print(result)
        if args.command == "doctor" and not result["requested_mode_ready"]:
            return 2
        if args.command == "qa" and not result["passed"]:
            return 1
        return 0
    except BTPError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

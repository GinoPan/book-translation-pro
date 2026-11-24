"""Source readiness: what prepare left in the source units, checked before anyone translates.

Problems found after translation cost a retranslation; found here, they cost a
fix to the source.  The report lists every exhibit prepare could not treat,
captions set apart from their pictures, and extraction residue in the units.
The ``source_readiness`` checkpoint blocks ``plan`` from dispatching translation
until a person approves the current report; changing a unit afterwards makes
the approval stale.

Severities:
``block``   the translator would get broken source (exhibit fragments, a table
            the tool could not rebuild, a caption in another unit than its picture)
``review``  worth a look, not wrong by itself (a total that disagrees with its
            rows, residue the build strips)
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, atomic_write_text, hash_data, load_json
from .figures import IMAGE_RE, _figure_asset_name
from .manifest import load_manifest

CHECKPOINT = "source_readiness"
REPORT = Path("qa") / "source-readiness.json"
OVERVIEW = Path("qa") / "source-readiness.md"
COMMENT_ONLY_RE = re.compile(r"^(?:\s|<!--.*?-->)*$", re.S)

# Retrofit outcomes, as the translator would meet them.
EXHIBIT_PROBLEMS = {
    "unmatched": ("block", "the exhibit's text is still scattered through the unit as fragments"),
    "needs_hand_rebuild": ("block", "the rebuilt table ran columns together, so it was not spliced in"),
}


def units_hash(manifest: dict[str, Any]) -> str:
    """What an approval is bound to: the source text of every unit."""
    return hash_data({entry["unit_id"]: entry["source_hash"] for entry in manifest["units"]})


def _exhibit_problems(project: Path) -> list[dict[str, Any]]:
    log_path = project / "analysis" / "exhibit-retrofit-log.json"
    inventory_path = project / "analysis" / "exhibit-inventory.json"
    if not log_path.is_file() or not inventory_path.is_file():
        return []
    problems: list[dict[str, Any]] = []
    inventory = {(e["key"], e["part"]): e for e in load_json(inventory_path)["exhibits"]}
    for decision in load_json(log_path)["decisions"]:
        exhibit = f"{decision['key']}" + (f" part {decision['part']}" if decision.get("part", 1) > 1 else "")
        where = {"exhibit": exhibit, "page": decision.get("page"), "unit_id": decision.get("caption_unit")}
        status = decision.get("status")
        if status in EXHIBIT_PROBLEMS:
            severity, what = EXHIBIT_PROBLEMS[status]
            problems.append({"kind": f"exhibit_{status}", "severity": severity, **where,
                             "detail": f"{what} ({decision.get('reason', '')})"})
        elif status == "untouched" and decision.get("classification") == "uncertain":
            problems.append({"kind": "exhibit_uncertain", "severity": "block", **where,
                             "detail": f"no rule recognised the exhibit, so its text is left as extracted ({decision.get('reason', '')})"})
        elif status == "untouched" and decision.get("classification") == "raster":
            problems.append({"kind": "exhibit_picture", "severity": "review", **where,
                             "detail": "a picture without a text layer is kept as printed; any words in it stay in English"})
        item = inventory.get((decision["key"], decision.get("part", 1)), {})
        for total in item.get("checks", {}).get("totals", []):
            if total["verdict"] == "mismatch":
                problems.append({"kind": "table_total_mismatch", "severity": "review", **where,
                                 "detail": f"{total['column']}: printed total {total['stated']} but the rows sum to "
                                           f"{total['sum_of_rows']} (a possible misprint in the source)"})
    return problems


def _caption_problems(project: Path, texts: dict[str, str]) -> list[dict[str, Any]]:
    """An exhibit's picture and its caption must be in the same unit (one unit is translated at a time)."""
    inventory_path = project / "analysis" / "exhibit-inventory.json"
    if not inventory_path.is_file():
        return []
    from .retrofit import _is_caption_of, _plain

    problems = []
    for exhibit in load_json(inventory_path)["exhibits"]:
        if exhibit["classification"] != "diagram":
            continue
        asset = _figure_asset_name(exhibit["key"])
        pictures = [uid for uid, text in texts.items() if asset in text]
        captions = [uid for uid, text in texts.items()
                    if any(_is_caption_of(_plain(line), exhibit) for line in text.splitlines())]
        if pictures and captions and not set(pictures) & set(captions):
            problems.append({"kind": "caption_apart_from_picture", "severity": "block", "exhibit": exhibit["key"],
                             "page": exhibit["page"], "unit_id": captions[0],
                             "detail": f"caption in {', '.join(captions)}, picture in {', '.join(pictures)}"})
    return problems


def _residue(project: Path, texts: dict[str, str]) -> list[dict[str, Any]]:
    from .publication import PRINT_MARKER_RE, _fragment_image

    problems = []
    for uid, text in texts.items():
        if COMMENT_ONLY_RE.match(text):
            continue  # counted in the report as empty; prepare carries such units through unchanged
        stamps = len(PRINT_MARKER_RE.findall(text))
        # IMAGE_RE finds both Markdown images and raw <img> tags.
        slivers = sum(1 for src in IMAGE_RE.findall(text) if _fragment_image(project, src))
        if stamps:
            problems.append({"kind": "print_stamps", "severity": "review", "unit_id": uid, "count": stamps,
                             "detail": f"{stamps} printer's page stamp(s) in the text"})
        if slivers:
            problems.append({"kind": "image_slivers", "severity": "review", "unit_id": uid, "count": slivers,
                             "detail": f"{slivers} sliver image(s) left from converting a drawing"})
    return problems


def readiness_report(project: Path) -> dict[str, Any]:
    """Check the prepared source units and write the report and its overview page."""
    manifest = load_manifest(project)
    texts = {entry["unit_id"]: (project / entry["source_file"]).read_text(encoding="utf-8")
             for entry in sorted(manifest["units"], key=lambda item: item["order"])}
    problems = _exhibit_problems(project) + _caption_problems(project, texts) + _residue(project, texts)
    counts = Counter(problem["severity"] for problem in problems)
    report = {
        "schema_version": 1,
        "units_hash": units_hash(manifest),
        "units": len(texts),
        "status": "blocked" if counts["block"] else "ready",
        "blocking": counts["block"],
        "to_review": counts["review"],
        # Units left with no text (a drawing's slivers or fragments were all they held).
        "empty_units": [uid for uid, text in texts.items() if COMMENT_ONLY_RE.match(text)],
        "problems": problems,
    }
    atomic_write_json(project / REPORT, report)
    atomic_write_text(project / OVERVIEW, render_overview(report))
    return {"status": report["status"], "blocking": report["blocking"], "to_review": report["to_review"],
            "empty_units": len(report["empty_units"]),
            "report": REPORT.as_posix(), "overview": OVERVIEW.as_posix(),
            "by_kind": dict(Counter(problem["kind"] for problem in problems))}


def render_overview(report: dict[str, Any]) -> str:
    lines = ["# Source readiness", "",
             f"Status: **{report['status']}**. {report['blocking']} blocking problem(s), "
             f"{report['to_review']} to review, over {report['units']} units. "
             f"{len(report['empty_units'])} unit(s) hold no text and are carried through unchanged.", "",
             "Translation waits for approval of this report: "
             "`btp record <project> --kind review --checkpoint source_readiness --approve --reviewer <name>`. "
             "Approving with blocking problems needs `--notes` saying why they are accepted.", ""]
    for severity, title in (("block", "Blocking"), ("review", "To review")):
        rows = [p for p in report["problems"] if p["severity"] == severity]
        if not rows:
            continue
        lines += [f"## {title} ({len(rows)})", "", "| Kind | Exhibit | Page | Unit | Detail |", "|---|---|---|---|---|"]
        for p in rows:
            detail = p["detail"].replace("|", "\\|")
            lines.append(f"| {p['kind']} | {p.get('exhibit', '')} | {p.get('page') or ''} | {p.get('unit_id') or ''} | {detail} |")
        lines.append("")
    return "\n".join(lines)


def readiness_evidence(project: Path, notes: str) -> dict[str, str]:
    """What an approval binds to; refuses a stale report, or blocking problems accepted without a reason."""
    path = project / REPORT
    if not path.is_file():
        raise BTPError("Run `btp source-readiness <project>` before approving the source_readiness checkpoint")
    report = load_json(path)
    if report["units_hash"] != units_hash(load_manifest(project)):
        raise BTPError("The source units changed after the readiness report; rerun `btp source-readiness` and review it again")
    if report["blocking"] and not notes.strip():
        raise BTPError(f"The report has {report['blocking']} blocking problem(s); fix them and rerun the report, "
                       "or approve with --notes saying why they are accepted")
    return {"units": report["units_hash"], "report": hash_data(report)}


def approval_is_stale(project: Path, review: dict[str, Any]) -> bool:
    """An approved review no longer matches the units (a unit changed after approval)."""
    if review.get("status") != "approved" or "units" not in review.get("evidence_hashes", {}):
        return False
    return review["evidence_hashes"]["units"] != units_hash(load_manifest(project))

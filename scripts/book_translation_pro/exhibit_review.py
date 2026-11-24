"""Exhibit review: every exhibit side by side with its translation, judged once.

``build_exhibit_review`` draws an original/translation comparison for every
exhibit in the inventory, writes a review file whose verdicts start ``pending``,
and an overview page.  The language agent looks at every comparison and sets each
verdict to ``ok`` or ``flagged`` (with notes); ``record_exhibit_review`` accepts
the file only when every exhibit is judged, an exhibit with an automatic problem
is flagged, and nothing changed since the comparisons were drawn.  The user then
reads the overview (flagged exhibits first) and approves the ``exhibits``
checkpoint, which binds to the recorded review.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, atomic_write_text, hash_data, load_json, safe_project_path, sha256_file, validate_schema
from .figure_render import RENDER_REPORT, Fonts, diagram_blocks, diagram_render_problems, translated_asset
from .exhibits import merged_cells
from .figures import _target_root
from .manifest import load_manifest
from .retrofit import exhibit_blocks
from .state import now_utc

REVIEW_DIR = "qa/exhibit-review"
TEMPLATE = f"{REVIEW_DIR}/review.json"
OVERVIEW = f"{REVIEW_DIR}/index.html"
RECORD = "qa/exhibit-review.json"
CHECKPOINT = "exhibits"
TREATED_TABLES = {"table_grid", "table_lists"}
SIDE_HEIGHT = 900          # pixels; each side of a comparison is scaled to at most this


# --- what there is to review -----------------------------------------------------------

def _units_in_order(project: Path) -> list[dict[str, Any]]:
    return sorted(load_manifest(project)["units"], key=lambda entry: entry["order"])


def _table_blocks(project: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every rebuilt table in the target, with its source block and the translator's
    note set right below it (a declared misprint correction)."""
    root = _target_root(project, config)
    out = []
    for entry in _units_in_order(project):
        target_path = root / f"{entry['unit_id']}.md"
        if not target_path.is_file():
            continue
        target = target_path.read_text(encoding="utf-8")
        source = safe_project_path(project, entry["source_file"]).read_text(encoding="utf-8")
        sources = [b for b in exhibit_blocks(source) if b["kind"] == "exhibit-table"]
        targets = [b for b in exhibit_blocks(target) if b["kind"] == "exhibit-table"]
        for index, block in enumerate(targets):
            note = re.match(r"\n+((?:>[^\n]*(?:\n|$))+)", target[block["span"][1]:])
            out.append({"unit_id": entry["unit_id"], "key": block["key"], "target": block["body"],
                        "source": sources[index]["body"] if index < len(sources) else "",
                        "note": note.group(1).strip() if note else ""})
    return out


def _corrections(project: Path) -> dict[str, list[str]]:
    """Declared misprint corrections by exhibit key, parsed exactly as QA parses them."""
    from .qa import _declared_corrections

    found: dict[str, list[str]] = {}
    for path in sorted((project / "state" / "observations").glob("*.json")):
        for printed, corrected, key in _declared_corrections(load_json(path)):
            found.setdefault(key, []).append(f"{printed} -> {corrected}")
    return found


def review_items(project: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """One item per inventory exhibit: how it is treated, what the automatic checks
    say, and the evidence a recorded verdict is bound to."""
    inventory_path = project / "analysis" / "exhibit-inventory.json"
    if not inventory_path.is_file():
        raise BTPError("No exhibit inventory: prepare writes it for a Study or Publication PDF book; for a project prepared earlier, run retrofit-exhibits (a run without --apply is enough)")
    inventory = sorted(load_json(inventory_path)["exhibits"], key=lambda e: (e["page"], e["key"], e["part"]))
    report_path = project / RENDER_REPORT
    renders = load_json(report_path).get("renders", {}) if report_path.is_file() else {}
    diagrams = {(i["exhibit"]["key"], i["exhibit"]["part"], i["exhibit"]["page"]): i
                for i in diagram_blocks(project, config) if i["exhibit"]}
    tables: dict[str, list[dict[str, Any]]] = {}
    for block in _table_blocks(project, config):
        tables.setdefault(block["key"], []).append(block)
    table_index: dict[str, int] = {}
    corrections = _corrections(project)
    seen: dict[str, int] = {}
    items = []
    for exhibit in inventory:
        identity = (exhibit["key"], exhibit["part"], exhibit["page"])
        base = exhibit["key"] + (f"/{exhibit['part']}" if exhibit["parts"] > 1 else "")
        seen[base] = seen.get(base, 0) + 1
        item_id = base if seen[base] == 1 else f"{base}@p{exhibit['page']}"
        item: dict[str, Any] = {
            "id": item_id, "key": exhibit["key"], "part": exhibit["part"], "page": exhibit["page"],
            "classification": exhibit["classification"], "unit_id": None, "auto_problems": [],
            "corrections": corrections.get(exhibit["key"], []), "verdict": "pending", "notes": "",
        }
        if identity in diagrams:
            block = diagrams[identity]
            item.update(treatment="diagram", unit_id=block["unit_id"])
            item["auto_problems"] = diagram_render_problems(project, block, renders)
            asset = project / translated_asset(exhibit)
            item["evidence"] = hash_data({"asset": sha256_file(asset) if asset.is_file() else None,
                                          "labels": block["translations"]})
        elif exhibit["classification"] in TREATED_TABLES and table_index.get(exhibit["key"], 0) < len(tables.get(exhibit["key"], [])):
            block = tables[exhibit["key"]][table_index.get(exhibit["key"], 0)]
            table_index[exhibit["key"]] = table_index.get(exhibit["key"], 0) + 1
            item.update(treatment="table", unit_id=block["unit_id"])
            cells = [[cell.strip() for cell in line.strip().strip("|").split("|")]
                     for line in block["source"].splitlines() if line.strip().startswith("|")]
            for cell in merged_cells(cells):
                item["auto_problems"].append(f"cell holds two numbers (columns ran together): {cell}")
            for total in exhibit.get("checks", {}).get("totals", []):
                if total["verdict"] == "mismatch" and not item["corrections"]:
                    item["auto_problems"].append(
                        f"{total['column']} total {total['stated']} but its rows sum to {total['sum_of_rows']}")
            item["evidence"] = hash_data({"source": block["source"], "target": block["target"], "note": block["note"]})
        elif exhibit["classification"] == "raster":
            item.update(treatment="photo", evidence=hash_data(exhibit))
        else:
            item.update(treatment="kept", evidence=hash_data(exhibit))
            item["auto_problems"].append(
                f"keeps its earlier treatment ({exhibit['classification']}: {exhibit['reason']})")
        item["image"] = f"{_slug(item_id)}.png"
        items.append(item)
    return items


def _slug(item_id: str) -> str:
    return "exhibit-" + re.sub(r"[^0-9A-Za-z]+", "-", item_id).strip("-").lower()


# --- comparison images -----------------------------------------------------------------

def _original(document: Any, exhibit: dict[str, Any]) -> Any:
    """The exhibit as printed, caption included: its crop, table region, or the page."""
    import fitz
    from PIL import Image

    from .exhibits import upright_page

    page, _rotation, _holder = upright_page(document, exhibit["page"])
    area = exhibit.get("crop") or exhibit.get("region")
    clip = fitz.Rect(area) | fitz.Rect(exhibit["caption_bbox"]) if area else page.rect
    pix = page.get_pixmap(dpi=200, clip=clip + (-6, -6, 6, 6), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _unescape(text: str) -> str:
    return text.replace("\\$", "$").replace("\\*", "*")


def _table_segments(body: str) -> list[tuple[str, Any]]:
    """The block in reading order: ("table", rows of cells, a cell a list of lines) and
    ("text", paragraph) segments."""
    segments: list[tuple[str, Any]] = []
    current: list[list[list[str]]] = []
    for line in body.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith("|"):
            if not re.fullmatch(r"\|(?:\s*:?-+:?\s*\|)+", stripped):
                current.append([_unescape(cell.strip()).split("<br>") for cell in stripped.strip("|").split("|")])
            continue
        if current:
            segments.append(("table", current))
            current = []
        if stripped:
            segments.append(("text", _unescape(stripped)))
    return segments


def _wrapped(text: str, width: float, font: Any, size: float) -> list[str]:
    lines, current = [], ""
    for char in text:
        if current and font.text_length(current + char, size) > width:
            lines.append(current)
            current = ""
        current += char
    return lines + ([current] if current else [])


def _draw_table(body: str, note: str, fonts: Fonts) -> Any:
    """The translated table drawn as a plain grid, with its paragraphs and note in order."""
    import fitz
    from PIL import Image

    font, size, pad, width = fonts.objects["zh"], 9.0, 4.0, 860.0
    document = fitz.open()
    page = document.new_page(width=900, height=1400)
    page.insert_font(fontname="zh", fontfile=fonts.files["zh"])
    y, right = 20.0, 320.0

    def text(value: str, color: tuple[float, float, float] = (0, 0, 0)) -> None:
        nonlocal y
        for line in _wrapped(value, width, font, size):
            page.insert_text((20, y + size), line, fontname="zh", fontsize=size, color=color)
            y += size * 1.5
        y += size * 0.3

    for kind, value in _table_segments(body):
        if kind == "text":
            text(value)
            continue
        columns = max(len(row) for row in value)
        widths = [max(font.text_length(line, size) for row in value if c < len(row) for line in row[c] or [""]) + 2 * pad
                  for c in range(columns)]
        for row in value:
            height = max(len(cell) for cell in row) * size * 1.3 + 2 * pad
            x = 20.0
            for c in range(columns):
                cell = row[c] if c < len(row) else [""]
                page.draw_rect(fitz.Rect(x, y, x + widths[c], y + height), color=(0.5, 0.5, 0.5), width=0.4)
                for n, line in enumerate(cell):
                    page.insert_text((x + pad, y + pad + size + n * size * 1.3), line, fontname="zh", fontsize=size)
                x += widths[c]
            right = max(right, x)
            y += height
        y += size * 1.2
    if note:
        right = 880.0
        text(_unescape(" ".join(line.lstrip("> ").strip() for line in note.splitlines())), (0.6, 0, 0))
    clip = fitz.Rect(0, 0, min(max(right, 300) + 20, 900), min(y + 20, 1400))
    pix = page.get_pixmap(dpi=200, clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _side_by_side(left: Any, right: Any | None) -> Any:
    from PIL import Image

    def fit(image: Any) -> Any:
        scale = min(1.0, SIDE_HEIGHT / image.height)
        return image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))

    left = fit(left)
    if right is None:
        return left
    right = fit(right)
    gap = 24
    canvas = Image.new("RGB", (left.width + gap + right.width, max(left.height, right.height)), "white")
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width + gap, 0))
    for y in range(canvas.height):
        canvas.putpixel((left.width + gap // 2, y), (200, 0, 0))
    return canvas


def build_exhibit_review(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Draw every comparison, write the review file (verdicts pending) and the overview."""
    import fitz

    items = review_items(project, config)
    inventory = {(e["key"], e["part"], e["page"]): e for e in load_json(project / "analysis" / "exhibit-inventory.json")["exhibits"]}
    tables = {}
    for block in _table_blocks(project, config):
        tables.setdefault(block["key"], []).append(block)
    taken: dict[str, int] = {}
    out_dir = project / REVIEW_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    fonts = Fonts()
    with fitz.open(safe_project_path(project, config["source"]["path"])) as document:
        for item in items:
            exhibit = inventory[(item["key"], item["part"], item["page"])]
            left = _original(document, exhibit)
            if item["treatment"] == "diagram":
                from PIL import Image
                right = Image.open(project / translated_asset(exhibit)).convert("RGB") \
                    if (project / translated_asset(exhibit)).is_file() else None
            elif item["treatment"] == "table":
                index = taken.get(item["key"], 0)
                taken[item["key"]] = index + 1
                block = tables[item["key"]][index]
                right = _draw_table(block["target"], block["note"], fonts)
            else:
                right = None
            _side_by_side(left, right).save(out_dir / item["image"])
    review = {"schema_version": 1, "reviewer": "", "reviewed_at": now_utc(), "items": items}
    validate_schema(review, "exhibit-review.schema.json")
    atomic_write_json(project / TEMPLATE, review)
    write_overview(project, review)
    return {"template": TEMPLATE, "overview": OVERVIEW, "exhibits": len(items),
            "with_automatic_problems": sum(1 for item in items if item["auto_problems"])}


# --- recording and verification ----------------------------------------------------------

def _current(project: Path, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in review_items(project, config)}


def stale_items(project: Path, config: dict[str, Any], review: dict[str, Any]) -> list[str]:
    """Exhibits whose review no longer matches what is there now (or that it misses)."""
    current = _current(project, config)
    recorded = {item["id"]: item for item in review["items"]}
    stale = [item_id for item_id, item in current.items()
             if item_id not in recorded or recorded[item_id]["evidence"] != item["evidence"]
             or recorded[item_id]["auto_problems"] != item["auto_problems"]]
    return sorted(stale + [item_id for item_id in recorded if item_id not in current])


def record_exhibit_review(project: Path, config: dict[str, Any], source: Path) -> dict[str, Any]:
    review = load_json(source)
    validate_schema(review, "exhibit-review.schema.json")
    pending = [item["id"] for item in review["items"] if item["verdict"] == "pending"]
    if pending:
        raise BTPError(f"Exhibit review still has pending verdicts: {', '.join(pending[:10])}"
                       + (f" (+{len(pending) - 10} more)" if len(pending) > 10 else "")
                       + "; look at each comparison and set ok or flagged")
    unflagged = [item["id"] for item in review["items"] if item["auto_problems"] and item["verdict"] != "flagged"]
    if unflagged:
        raise BTPError("Exhibits with automatic problems must be flagged for the user: " + ", ".join(unflagged))
    silent = [item["id"] for item in review["items"] if item["verdict"] == "flagged" and not item["notes"].strip()]
    if silent:
        raise BTPError("Flagged exhibits need a note saying what to look at: " + ", ".join(silent))
    if not review["reviewer"].strip():
        raise BTPError("Exhibit review needs a reviewer")
    stale = stale_items(project, config, review)
    if stale:
        raise BTPError("Exhibit review does not match the current exhibits (re-run exhibit-review): " + ", ".join(stale[:10]))
    atomic_write_json(project / RECORD, review)
    write_overview(project, review)
    flagged = [item["id"] for item in review["items"] if item["verdict"] == "flagged"]
    return {"recorded": "exhibit-review", "path": RECORD, "exhibits": len(review["items"]), "flagged": flagged,
            "overview": OVERVIEW}


def review_evidence(project: Path, config: dict[str, Any]) -> dict[str, str]:
    """What an approval of the exhibits checkpoint is bound to; fails when there is
    nothing current to approve."""
    path = project / RECORD
    if not path.is_file():
        raise BTPError("Record the exhibit review (record --kind exhibit-review) before approving the exhibits checkpoint")
    review = load_json(path)
    stale = stale_items(project, config, review)
    if stale:
        raise BTPError("The recorded exhibit review is stale for: " + ", ".join(stale[:10]))
    return {"exhibit_review": sha256_file(path)}


# --- overview --------------------------------------------------------------------------

def write_overview(project: Path, review: dict[str, Any]) -> None:
    """One page: exhibits the user should look at first, then the rest folded away."""
    def card(item: dict[str, Any]) -> str:
        problems = "".join(f"<li class=auto>{html.escape(p)}</li>" for p in item["auto_problems"])
        fixes = "".join(f"<li class=fix>corrected {html.escape(c)}</li>" for c in item["corrections"])
        note = f"<p class=note>{html.escape(item['notes'])}</p>" if item["notes"].strip() else ""
        return (f"<article class='{item['verdict']}' id='{html.escape(_slug(item['id']))}'>"
                f"<h3>Exhibit {html.escape(item['id'])} <small>page {item['page']} · {html.escape(item['treatment'])}"
                f"{' · ' + html.escape(item['unit_id']) if item['unit_id'] else ''} · {html.escape(item['verdict'])}</small></h3>"
                f"<ul>{problems}{fixes}</ul>{note}"
                f"<a href='{html.escape(item['image'])}'><img loading=lazy src='{html.escape(item['image'])}' alt=''></a></article>")

    items = review["items"]
    first = [i for i in items if i["verdict"] != "ok" or i["auto_problems"]]
    rest = [i for i in items if i not in first]
    counts = {v: sum(1 for i in items if i["verdict"] == v) for v in ("ok", "flagged", "pending")}
    page = f"""<!doctype html>
<html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Exhibit review</title>
<style>
:root {{ --ink:#1d1d1f; --muted:#6b6b70; --line:#d9d9de; --bg:#fafafa; --flag:#b54708; --auto:#b42318; --ok:#067647; }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, sans-serif; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:18px; margin:28px 0 8px; }}
.summary {{ color:var(--muted); margin:0 0 12px; }}
article {{ background:#fff; border:1px solid var(--line); border-left:4px solid var(--line); border-radius:6px; padding:10px 12px; margin:10px 0; }}
article.flagged {{ border-left-color:var(--flag); }} article.ok {{ border-left-color:var(--ok); }}
h3 {{ font-size:16px; margin:0 0 4px; }} small {{ color:var(--muted); font-weight:normal; }}
ul {{ margin:4px 0; padding-left:18px; }} li.auto {{ color:var(--auto); }} li.fix {{ color:var(--muted); }}
.note {{ margin:4px 0; color:var(--flag); }}
img {{ max-width:100%; height:auto; border:1px solid var(--line); margin-top:6px; }}
details > summary {{ cursor:pointer; font-weight:600; margin:20px 0 8px; }}
</style></head><body>
<h1>Exhibit review</h1>
<p class=summary>{len(items)} exhibits · {counts['flagged']} flagged · {counts['ok']} fine · {counts['pending']} not yet judged.
Left: as printed. Right: the translation (none for photos and exhibits kept as they were).</p>
<h2>Look at these ({len(first)})</h2>
{''.join(card(i) for i in first) or '<p class=summary>Nothing flagged.</p>'}
<details><summary>Judged fine ({len(rest)})</summary>
{''.join(card(i) for i in rest)}
</details>
</body></html>
"""
    atomic_write_text(project / OVERVIEW, page)

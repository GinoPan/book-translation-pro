"""Render a diagram exhibit with its labels translated in place.

The line art stays untouched; only the label text is replaced.  Placement is
deterministic: every candidate rectangle, font size (down to ``MIN_SCALE`` of the
source size) and line arrangement is scored, preferring no new crossing of line
art, then the largest size, then the source shape.  The source often lays a white
patch behind a label to cut the arrow it crosses; those patches are removed with
the source text, and a new cut is made only where the translation crosses a line.

Every render is checked: each replaced label is drawn, no source text is left,
and no line is cut without text over the cut.
"""

from __future__ import annotations

import math
import re
from itertools import combinations
from pathlib import Path
from typing import Any

from .artifacts import BTPError
from .exhibits import boxes_in, group_labels, label_lines, upright_page

# (regular, bold) CJK font pairs, first found wins: Windows, macOS, Linux.
FONT_CANDIDATES = (
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
    ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
)
MIN_SCALE = 0.6           # smallest font size, relative to the source label
LINE_HEIGHT = 1.15
ROW_CLEARANCE = 0.25      # space kept between stacked rows, in font sizes
RENDER_DPI = 250
_NO_LINE_START = set("，、。：；）」』》,.:;)")


def _fitz() -> Any:
    import fitz

    return fitz


def font_files() -> tuple[str, str]:
    """The CJK fonts used for translated labels; fail loudly when they are missing."""
    for regular, bold in FONT_CANDIDATES:
        if Path(regular).is_file() and Path(bold).is_file():
            return regular, bold
    raise BTPError("Translated figure labels need a CJK font; none found among: "
                   + ", ".join(regular for regular, _bold in FONT_CANDIDATES))


class Fonts:
    def __init__(self) -> None:
        fitz = _fitz()
        self.files = dict(zip(("zh", "zhb"), font_files()))
        self.objects = {name: fitz.Font(fontfile=path) for name, path in self.files.items()}
        self._top: dict[str, float] = {}

    def top_offset(self, name: str) -> float:
        """Per point of size, from a text box's top to its first line's em-box top (measured once)."""
        if name not in self._top:
            fitz = _fitz()
            scratch = fitz.open()
            page = scratch.new_page()
            page.insert_font(fontname=name, fontfile=self.files[name])
            size, y0 = 10.0, 100.0
            page.insert_textbox(fitz.Rect(50, y0, 300, y0 + 40), "测试字样", fontname=name, fontsize=size,
                                lineheight=LINE_HEIGHT)
            line = fitz.Rect(page.get_text("dict")["blocks"][0]["lines"][0]["bbox"])
            self._top[name] = ((line.y0 + line.y1) / 2 - size / 2 - y0) / size
        return self._top[name]


# --- line art --------------------------------------------------------------------------

def line_art(page: Any, crop: Any) -> tuple[list[tuple[Any, Any, int]], list[tuple[Any, int]]]:
    """Straight pieces of line art (with paint order) and the white patches drawn over them."""
    pieces: list[tuple[Any, Any, int]] = []
    masks: list[tuple[Any, int]] = []
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        # A horizontal or vertical line has an empty rect, which intersects nothing.
        pad = max(float(drawing.get("width") or 0), 0.5)
        if not crop.intersects(rect + (-pad, -pad, pad, pad)):
            continue
        fill = drawing.get("fill")
        if fill and min(fill) > 0.97 and rect.get_area() < 0.5 * crop.get_area():
            masks.append((rect, drawing["seqno"]))
            continue
        for item in drawing["items"]:
            if item[0] == "l":
                ends = [(item[1], item[2])]
            elif item[0] == "c":
                ends = [(item[1], item[4])]
            elif item[0] == "re":
                r = item[1]
                ends = [(r.tl, r.tr), (r.tr, r.br), (r.br, r.bl), (r.bl, r.tl)]
            elif item[0] == "qu":
                q = item[1]
                ends = [(q.ul, q.ur), (q.ur, q.lr), (q.lr, q.ll), (q.ll, q.ul)]
            else:
                ends = []
            pieces += [(a, b, drawing["seqno"]) for a, b in ends]
    return pieces, masks


def _samples(a: Any, b: Any, step: float = 2.0) -> list[Any]:
    """Points along a piece no more than ``step`` apart: a fixed count would leave
    gaps wider than a label on a long line, and a cut or crossing there unseen."""
    fitz = _fitz()
    count = max(1, math.ceil(abs(b - a) / step))
    return [fitz.Point(a.x + (b.x - a.x) * t / count, a.y + (b.y - a.y) * t / count) for t in range(count + 1)]


def hits(rect: Any, art: tuple[list[Any], list[Any]]) -> bool:
    """Whether visible line art passes through the rectangle."""
    pieces, masks = art
    for a, b, order in pieces:
        for point in _samples(a, b):
            if rect.contains(point) and not any(mask.contains(point) and seq > order for mask, seq in masks):
                return True
    return False


def hidden_points(pieces: list[Any], masks: list[tuple[Any, int]]) -> list[Any]:
    """Points of line art under a later white patch."""
    return [point for a, b, order in pieces for point in _samples(a, b, step=0.5)
            if any(mask.contains(point) and seq > order for mask, seq in masks)]


def knockouts_of(group: dict[str, Any], masks: list[tuple[Any, int]]) -> list[Any]:
    """White patches laid behind this label; a box's white background is larger than the label."""
    size = group["size"]
    zone = group["bbox"] + (-0.8 * size, -0.8 * size, 0.8 * size, 0.8 * size)
    return [rect for rect, _seq in masks if zone.contains(rect)]


def remove_patches(page: Any, rects: list[Any]) -> tuple[list[Any], list[Any]]:
    """Delete white patches, revealing the lines under them.  Only a patch with nothing but
    white fill wholly inside it is deleted (deletion takes everything it covers)."""
    fitz = _fitz()
    removed, kept = [], []
    drawings = page.get_drawings()
    for rect in rects:
        area = rect + (-0.2, -0.2, 0.2, 0.2)
        inside = [d for d in drawings if area.contains(d["rect"])]
        if all(d.get("fill") and min(d["fill"]) > 0.97 and not d.get("color") for d in inside):
            page.add_redact_annot(area, fill=False)
            removed.append(rect)
        else:
            kept.append(rect)
    if removed:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    return removed, kept


# --- placement -------------------------------------------------------------------------

def alignment(group: dict[str, Any], groups: list[dict[str, Any]], boxes: list[Any]) -> int:
    fitz = _fitz()
    lines = group["lines"]
    centers = [(l["bbox"].x0 + l["bbox"].x1) / 2 for l in lines]
    lefts = [l["bbox"].x0 for l in lines]
    if len(lines) > 1:
        if max(lefts) - min(lefts) < 0.8:
            return fitz.TEXT_ALIGN_LEFT
        return fitz.TEXT_ALIGN_CENTER if max(centers) - min(centers) < 2.5 else fitz.TEXT_ALIGN_LEFT
    if group["box"] is not None:
        # A column of items in one box (a legend) shares a left edge.
        if any(g is not group and g["box"] == group["box"] and abs(g["bbox"].x0 - lefts[0]) < 1.5 for g in groups):
            return fitz.TEXT_ALIGN_LEFT
        box = boxes[group["box"]]
        if abs((box.x0 + box.x1) / 2 - centers[0]) < 4:
            return fitz.TEXT_ALIGN_CENTER
    return fitz.TEXT_ALIGN_LEFT


def candidate_rects(group: dict[str, Any], groups: list[dict[str, Any]], boxes: list[Any], crop: Any) -> list[Any]:
    """Where the translation may go, most faithful first.

    In a box: its inner width, vertically halfway to its neighbours in the box, clear
    of the box edges (Chinese glyphs fill the em box).  A loose label: its own
    footprint, then one more line of height, then a little more width.
    """
    fitz = _fitz()
    b, size = group["bbox"], group["size"]
    if group["box"] is not None:
        box = boxes[group["box"]]
        same = [g for g in groups if g is not group and g["box"] == group["box"]]
        above = [(g["bbox"].y1 + b.y0) / 2 for g in same if g["bbox"].y1 <= b.y0 + 0.5]
        below = [(g["bbox"].y0 + b.y1) / 2 for g in same if g["bbox"].y0 >= b.y1 - 0.5]
        y0 = max(above + [box.y0 + 0.3 * size])
        y1 = min(below + [box.y1 - 0.3 * size])
        return [fitz.Rect(box.x0 + 2.5, y0, box.x1 - 2.5, y1) & crop]
    pad = 0.15 * size
    footprint = fitz.Rect(b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad)
    taller = fitz.Rect(footprint.x0, footprint.y0 - 0.6 * size, footprint.x1, footprint.y1 + 0.6 * size)
    wider = fitz.Rect(taller.x0, taller.y0, taller.x1 + 0.25 * b.width, taller.y1)
    return [footprint & crop, taller & crop, wider & crop]


def break_units(text: str) -> list[str]:
    """Unbreakable pieces: the translator marks allowed breaks with "|"; "\\n" forces one;
    a space allows one; closing punctuation stays with the text it closes."""
    units: list[str] = []
    for part in re.split(r"(\n)", text):
        if part == "\n":
            units.append("\n")
            continue
        for piece in re.split(r"\|| (?=\S)", part):
            if piece and units and units[-1] != "\n" and piece[0] in _NO_LINE_START:
                units[-1] += piece
            elif piece:
                units.append(piece)
    return units


def joined(units: list[str]) -> str:
    out = ""
    for unit in units:
        # Chinese and Latin letters or digits are set with a space between them.
        latin_edge = out and (re.match(r"[A-Za-z0-9(]", unit[0]) or re.search(r"[A-Za-z0-9)]$", out))
        out += (" " if latin_edge else "") + unit
    return out


def arrangements(text: str, width: float, font: Any, size: float) -> list[list[str]]:
    """Ways to set the text on 1..n lines within the width, fewest lines first, each balanced."""
    units = break_units(text)
    if "\n" in units:
        lines, current = [], []
        for unit in units + ["\n"]:
            if unit == "\n":
                lines.append(current)
                current = []
            else:
                current.append(unit)
        options = [lines]
    else:
        options = []
        n = len(units)
        for count in range(1, min(n, 6) + 1):
            best = None
            for cuts in combinations(range(1, n), count - 1):
                bounds = (0, *cuts, n)
                lines = [units[bounds[i]:bounds[i + 1]] for i in range(count)]
                longest = max(font.text_length(joined(line), size) for line in lines)
                if best is None or longest < best[0]:
                    best = (longest, lines)
            options.append(best[1])
    result = []
    for lines in options:
        texts = [joined(line) for line in lines]
        if all(font.text_length(t, size) <= width for t in texts):
            result.append(texts)
    return result


def ink_boxes(lines: list[str], rect: Any, top: float, size: float, align: int, font: Any) -> list[Any]:
    fitz = _fitz()
    boxes = []
    for index, line in enumerate(lines):
        width = font.text_length(line, size)
        x0 = rect.x0 + (rect.width - width) / 2 if align == fitz.TEXT_ALIGN_CENTER else rect.x0
        y = top + index * size * LINE_HEIGHT
        boxes.append(fitz.Rect(x0, y, x0 + width, y + size))
    return boxes


def ink_height(count: int, size: float) -> float:
    return (count - 1) * size * LINE_HEIGHT + size


def choose(group: dict[str, Any], translation: str, rects: list[Any], align: int, art: Any, font: Any,
           knockout: bool, max_size: float | None = None) -> dict[str, Any]:
    """Score every rectangle, size and arrangement; returns the best placement or a failure."""
    floor = round(group["size"] * MIN_SCALE, 2)
    center = (group["bbox"].y0 + group["bbox"].y1) / 2
    # A label whose source cut the lines it crossed (knockout) may cross them again and
    # gets a new cut; one that simply sat on a line (a fishbone tick) may cross it too.
    source_crosses = knockout or any(
        hits(l["bbox"] + (0.3, 0.25 * l["size"], -0.3, -0.25 * l["size"]), art) for l in group["lines"])
    best = None
    size = min(group["size"], max_size or group["size"])
    while size >= floor - 1e-6:
        for index, rect in enumerate(rects):
            for lines in arrangements(translation, rect.width, font, size):
                height = ink_height(len(lines), size)
                if height + ROW_CLEARANCE * size > rect.height + 0.01:
                    continue
                top = min(max(center - height / 2, rect.y0), rect.y1 - height)
                collides = not source_crosses and any(
                    hits(box, art) for box in ink_boxes(lines, rect, top, size, align, font))
                shape = abs(len(lines) - len(group["lines"])) if group["box"] is None else len(lines)
                score = (collides, -size, shape, index)
                if best is None or score < best["score"]:
                    best = {"score": score, "rect": rect, "top": top, "size": size, "lines": lines}
        size = round(size - 0.2, 2)
    if best is None:
        return {"status": "does_not_fit", "size": floor}
    best["status"] = "crosses_line_art" if best["score"][0] else "ok"
    return best


# --- render ----------------------------------------------------------------------------

def render_diagram(source_pdf: Path, exhibit: dict[str, Any], translations: list[str], destination: Path) -> dict[str, Any]:
    """Render one diagram with its labels replaced; returns the check report.

    ``translations`` line up with ``exhibit["labels"]``; a kept label (a code, a
    number) must come back unchanged.  The labels are recomputed from the PDF and
    must match the inventory, so a stale inventory fails loudly.
    """
    fitz = _fitz()
    if len(translations) != len(exhibit["labels"]):
        raise BTPError(f"Exhibit {exhibit['key']}: {len(exhibit['labels'])} labels, {len(translations)} translations")
    fonts = Fonts()
    with fitz.open(source_pdf) as document:
        page, rotation, _holder = upright_page(document, exhibit["page"])
        if rotation:
            raise BTPError(f"Exhibit {exhibit['key']}: rotated diagrams are not rendered in place")
        crop = fitz.Rect(exhibit["crop"])
        caption = fitz.Rect(exhibit["caption_bbox"])
        boxes = boxes_in(page, crop)
        groups = group_labels(label_lines(page, crop, caption, document_body_size(exhibit)), boxes)
        if [g["text"] for g in groups] != [label["text"] for label in exhibit["labels"]]:
            raise BTPError(f"Exhibit {exhibit['key']}: labels on page {exhibit['page']} no longer match the inventory")
        targets = [t.strip() for t in translations]
        changed = [(g, t) for g, t in zip(groups, targets) if t.replace("|", "") != g["text"]]
        source_pieces, source_masks = line_art(page, crop)
        patches = {id(g): knockouts_of(g, source_masks) for g, _t in changed}
        for group, _t in changed:
            for line in group["lines"]:
                inset = min(line["bbox"].height * 0.25, line["size"] * 0.3)
                page.add_redact_annot(line["bbox"] + (0.3, inset, -0.3, -inset), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        removed, kept_patches = remove_patches(page, [r for rects in patches.values() for r in rects])
        for name, path in fonts.files.items():
            page.insert_font(fontname=name, fontfile=path)
        art = line_art(page, crop)
        rows: list[dict[str, Any]] = []
        # Items of one list (a legend: same box, same left edge, same size) share the
        # smallest size any of them needs, so the list stays even.
        plans: dict[int, dict[str, Any]] = {}
        for group, target in changed:
            name = "zhb" if "Bold" in group["font"] else "zh"
            plans[id(group)] = choose(group, target, candidate_rects(group, groups, boxes, crop),
                                      alignment(group, groups, boxes), art, fonts.objects[name], bool(patches[id(group)]))
        for group, target in changed:
            column = [h for h, _t in changed if group["box"] is not None and h["box"] == group["box"]
                      and abs(h["bbox"].x0 - group["bbox"].x0) < 1.5 and h["size"] == group["size"]]
            if len(column) >= 3:
                cap = min(plans[id(h)]["size"] for h in column)
                if cap < plans[id(group)].get("size", cap):
                    name = "zhb" if "Bold" in group["font"] else "zh"
                    plans[id(group)] = choose(group, target, candidate_rects(group, groups, boxes, crop),
                                              alignment(group, groups, boxes), art, fonts.objects[name],
                                              bool(patches[id(group)]), max_size=cap)
        ink: list[tuple[Any, float]] = []
        new_patches: list[Any] = []
        for group, target in changed:
            name = "zhb" if "Bold" in group["font"] else "zh"
            plan = plans[id(group)]
            align = alignment(group, groups, boxes)
            if plan["status"] == "does_not_fit":
                rows.append({"source": group["text"], "target": target.replace("|", ""), "status": "does_not_fit"})
                continue
            boxes_drawn = ink_boxes(plan["lines"], plan["rect"], plan["top"], plan["size"], align, fonts.objects[name])
            if patches[id(group)]:
                for box in boxes_drawn:
                    if hits(box, art):
                        patch = box + (-0.15 * plan["size"], -0.1 * plan["size"], 0.15 * plan["size"], 0.1 * plan["size"])
                        page.draw_rect(patch, color=None, fill=(1, 1, 1), width=0, overlay=True)
                        new_patches.append(patch)
            box_top = plan["top"] - fonts.top_offset(name) * plan["size"]
            rect = plan["rect"]
            page.insert_textbox(fitz.Rect(rect.x0, box_top, rect.x1, box_top + ink_height(len(plan["lines"]), plan["size"]) + 2 * plan["size"]),
                                "\n".join(plan["lines"]), fontname=name, fontsize=plan["size"], align=align,
                                lineheight=LINE_HEIGHT, color=_rgb(group["color"]))
            ink += [(box, group["size"]) for box in boxes_drawn]
            rows.append({"source": group["text"], "target": target.replace("|", ""), "status": plan["status"],
                         "size_from": group["size"], "size_to": plan["size"], "lines": len(plan["lines"])})
        # Checks: no cut line without text over it; no source text left.
        final_pieces, _masks = line_art(page, crop)
        cutters = [(rect, 10 ** 9) for rect in kept_patches + new_patches]
        gaps = [p for p in hidden_points(final_pieces, cutters)
                if not any((box + (-0.3 * s, -0.3 * s, 0.3 * s, 0.3 * s)).contains(p) for box, s in ink)]
        left = page.get_text("text", clip=crop)
        leftovers = [g["text"] for g, _t in changed if any(l["text"] in left for l in g["lines"] if len(l["text"]) > 3)]
        destination.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(dpi=RENDER_DPI, clip=crop, alpha=False).save(destination)
    problems = [f"{row['source']} ({row['status']})" for row in rows if row["status"] != "ok"]
    problems += [f"line cut without text near ({round(p.x)}, {round(p.y)})" for p in gaps[:5]]
    problems += [f"source text left: {text}" for text in leftovers]
    return {
        "key": exhibit["key"], "page": exhibit["page"], "asset": str(destination), "labels": len(groups),
        "replaced": len(changed), "patches": {"source": sum(len(v) for v in patches.values()), "removed": len(removed),
                                              "kept": len(kept_patches), "new": len(new_patches)},
        "passed": not problems, "problems": problems, "rows": rows,
    }


def document_body_size(exhibit: dict[str, Any]) -> float:
    return float(exhibit.get("body_size") or 0)


def _rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255

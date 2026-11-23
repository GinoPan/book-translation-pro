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
CURVE_STEPS = 8           # straight pieces per Bézier curve
INK_MARGIN = 0.05         # glyph ink beyond the em box, in font sizes
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

    def baseline(self, name: str) -> float:
        """Per point of size, from a line's em-box top to its baseline, chosen so the glyphs'
        ink is centred on the em box placement assumes (measured once from rendered ink)."""
        if name not in self._top:
            fitz = _fitz()
            scratch = fitz.open()
            page = scratch.new_page(width=200, height=120)
            page.insert_font(fontname=name, fontfile=self.files[name])
            size, base = 40.0, 70.0
            page.insert_text((10, base), "测试字样", fontname=name, fontsize=size)
            scale = 4
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
            rows = [y for y in range(pix.height)
                    if min(pix.samples[y * pix.stride: y * pix.stride + pix.width]) < 128]
            if not rows:
                raise BTPError(f"Font {self.files[name]} drew no ink while measuring its placement")
            ink_middle = (rows[0] + rows[-1] + 1) / 2 / scale
            self._top[name] = (base - ink_middle) / size + 0.5
        return self._top[name]


# --- line art --------------------------------------------------------------------------

def line_art(page: Any, crop: Any) -> tuple[list[tuple[Any, Any, int]], list[tuple[Any, int]], list[tuple[Any, int]]]:
    """Straight pieces of line art (with paint order), the white patches drawn over
    them, and the other filled shapes (a shaded box hides the lines painted before it)."""
    fitz = _fitz()
    pieces: list[tuple[Any, Any, int]] = []
    masks: list[tuple[Any, int]] = []
    cover: list[tuple[Any, int]] = []
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
        if fill and min(fill) > 0.97 and drawing.get("type") == "f":
            continue  # a white page or panel background: no visible edge
        if fill and rect.get_area() < 0.5 * crop.get_area():
            cover.append((rect, drawing["seqno"]))
        for item in drawing["items"]:
            if item[0] == "l":
                ends = [(item[1], item[2])]
            elif item[0] == "c":
                # Follow the curve: its chord would cut through a circle's middle.
                p0, c1, c2, p3 = item[1], item[2], item[3], item[4]
                points = [p0 * (1 - t) ** 3 + c1 * 3 * (1 - t) ** 2 * t + c2 * 3 * (1 - t) * t ** 2 + p3 * t ** 3
                          for t in (i / CURVE_STEPS for i in range(CURVE_STEPS + 1))]
                ends = list(zip(points, points[1:]))
            elif item[0] == "re":
                r = item[1]
                ends = [(r.tl, r.tr), (r.tr, r.br), (r.br, r.bl), (r.bl, r.tl)]
            elif item[0] == "qu":
                q = item[1]
                ends = [(q.ul, q.ur), (q.ur, q.lr), (q.lr, q.ll), (q.ll, q.ul)]
            else:
                ends = []
            pieces += [(a, b, drawing["seqno"]) for a, b in ends
                       if crop.intersects(fitz.Rect(min(a.x, b.x) - pad, min(a.y, b.y) - pad,
                                                    max(a.x, b.x) + pad, max(a.y, b.y) + pad))]
    return pieces, masks, cover


def _samples(a: Any, b: Any, step: float = 2.0) -> list[Any]:
    """Points along a piece no more than ``step`` apart: a fixed count would leave
    gaps wider than a label on a long line, and a cut or crossing there unseen."""
    fitz = _fitz()
    count = max(1, math.ceil(abs(b - a) / step))
    return [fitz.Point(a.x + (b.x - a.x) * t / count, a.y + (b.y - a.y) * t / count) for t in range(count + 1)]


GRID = 16.0  # cell size, in points, of the spatial indexes below


def _cells(rect: Any) -> list[tuple[int, int]]:
    return [(i, j) for i in range(math.floor(rect.x0 / GRID), math.floor(rect.x1 / GRID) + 1)
            for j in range(math.floor(rect.y0 / GRID), math.floor(rect.y1 / GRID) + 1)]


class PointGrid:
    """Points bucketed by cell, so a rectangle is tested only against points near it
    (a detailed drawing has tens of thousands)."""

    def __init__(self, points: list[Any]) -> None:
        self.points = points
        self.cells: dict[tuple[int, int], list[Any]] = {}
        for point in points:
            self.cells.setdefault((math.floor(point.x / GRID), math.floor(point.y / GRID)), []).append(point)

    def __iter__(self) -> Any:
        return iter(self.points)

    def __len__(self) -> int:
        return len(self.points)

    def within(self, rect: Any) -> bool:
        return any(rect.contains(point) for cell in _cells(rect) for point in self.cells.get(cell, ()))


def visible_points(art: tuple[list[Any], ...]) -> PointGrid:
    """Sample points of the line art not hidden under a later white patch or filled
    shape (computed once per page state: placement tests every candidate against them)."""
    pieces, *hiders = art
    # Plain numbers, not fitz objects: this runs for every sample point of the drawing.
    by_cell: dict[tuple[int, int], list[tuple[float, float, float, float, int]]] = {}
    for group in hiders:
        for shape, seq in group:
            for cell in _cells(shape):
                by_cell.setdefault(cell, []).append((shape.x0, shape.y0, shape.x1, shape.y1, seq))
    visible = []
    for a, b, order in pieces:
        for point in _samples(a, b):
            x, y = point.x, point.y
            near = by_cell.get((math.floor(x / GRID), math.floor(y / GRID)), ())
            if not any(seq > order and x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1, seq in near):
                visible.append(point)
    return PointGrid(visible)


def hits(rect: Any, points: PointGrid) -> bool:
    """Whether visible line art passes through the rectangle."""
    return points.within(rect)


def underlines_of(group: dict[str, Any], pieces: list[tuple[Any, Any, int]]) -> list[Any]:
    """Horizontal pieces lying under this label's own lines (an underlined word or
    heading): they belong to the text and go with it.  A box edge or a rule runs past
    the text it sits under, so it is never taken."""
    found = []
    for line in group["lines"]:
        box, size = line["bbox"], line["size"]
        zone_x0, zone_x1 = box.x0 - 1.0, box.x1 + 1.0
        zone_y0, zone_y1 = box.y1 - 0.35 * size, box.y1 + 0.4 * size
        for a, b, _order in pieces:
            if (abs(a.y - b.y) < 0.3 and zone_y0 <= a.y <= zone_y1
                    and zone_x0 <= min(a.x, b.x) and max(a.x, b.x) <= zone_x1
                    # an underline spans at least a word; an arrow shaft's end does not
                    and abs(a.x - b.x) >= 1.5 * size):
                found.append(_fitz().Rect(min(a.x, b.x), a.y, max(a.x, b.x), a.y))
    return found


def hidden_points(pieces: list[Any], masks: list[tuple[Any, int]]) -> list[Any]:
    """Points of line art under a later white patch."""
    return [point for a, b, order in pieces for point in _samples(a, b, step=0.5)
            if any(mask.contains(point) and seq > order for mask, seq in masks)]


def _edged(rect: Any, seq: int, pieces: list[tuple[Any, Any, int]]) -> bool:
    """Whether lines drawn after this white shape run along its edges: a box with a
    white background, not a patch cut behind a label."""
    def on_edge(a: Any, b: Any) -> bool:
        horizontal = abs(a.y - b.y) < 0.3 and min(abs(a.y - rect.y0), abs(a.y - rect.y1)) < 0.6
        vertical = abs(a.x - b.x) < 0.3 and min(abs(a.x - rect.x0), abs(a.x - rect.x1)) < 0.6
        long_enough = abs(b - a) > 0.5 * min(rect.width, rect.height)
        return (horizontal or vertical) and long_enough
    return any(order > seq and on_edge(a, b) for a, b, order in pieces)


def knockouts_of(group: dict[str, Any], masks: list[tuple[Any, int]], pieces: list[tuple[Any, Any, int]]) -> list[Any]:
    """White patches laid behind this label to cut a line it crosses.  A patch that
    hides no earlier line (a box's white background, edged after it) is not one."""
    size = group["size"]
    zone = group["bbox"] + (-0.8 * size, -0.8 * size, 0.8 * size, 0.8 * size)
    found = []
    for rect, seq in masks:
        if not zone.contains(rect) or _edged(rect, seq, pieces):
            continue
        inner = rect + (0.3, 0.3, -0.3, -0.3)
        under = [(a, b) for a, b, order in pieces if order < seq
                 and inner.intersects(_fitz().Rect(min(a.x, b.x) - 0.5, min(a.y, b.y) - 0.5, max(a.x, b.x) + 0.5, max(a.y, b.y) + 0.5))]
        if any(inner.contains(point) for a, b in under for point in _samples(a, b, step=0.5)):
            found.append(rect)
    return found


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


def candidate_rects(group: dict[str, Any], groups: list[dict[str, Any]], boxes: list[Any], crop: Any,
                    align: int) -> list[Any]:
    """Where the translation may go, most faithful first.

    In a box: its inner width, vertically halfway to its neighbours in the box and
    horizontally halfway to labels beside it, clear of the box edges (Chinese glyphs
    fill the em box); a left-set label keeps its own left edge, a centred one stays
    centred on itself.  Then, as for a loose label: its own footprint, then one more
    line of height, then a little more width.
    """
    fitz = _fitz()
    b, size = group["bbox"], group["size"]
    pad = 0.15 * size
    footprint = fitz.Rect(b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad)
    taller = fitz.Rect(footprint.x0, footprint.y0 - 0.6 * size, footprint.x1, footprint.y1 + 0.6 * size)
    wider = fitz.Rect(taller.x0, taller.y0, taller.x1 + 0.25 * b.width, taller.y1)
    loose = [footprint & crop, taller & crop, wider & crop]
    if align == fitz.TEXT_ALIGN_LEFT:
        # Left-set text starts where the source text started (often right at a rule).
        for rect in loose:
            rect.x0 = max(rect.x0, b.x0)
    if group["box"] is None:
        return loose
    box = boxes[group["box"]]
    same = [g for g in groups if g is not group and g["box"] == group["box"]]
    beside = [g for g in same if g["bbox"].y0 < b.y1 - 0.5 and g["bbox"].y1 > b.y0 + 0.5]
    above = [(g["bbox"].y1 + b.y0) / 2 for g in same if g not in beside and g["bbox"].y1 <= b.y0 + 0.5]
    below = [(g["bbox"].y0 + b.y1) / 2 for g in same if g not in beside and g["bbox"].y0 >= b.y1 - 0.5]
    left = [(g["bbox"].x1 + b.x0) / 2 for g in beside if g["bbox"].x1 <= b.x0 + 0.5]
    right = [(g["bbox"].x0 + b.x1) / 2 for g in beside if g["bbox"].x0 >= b.x1 - 0.5]
    y0 = max(above + [box.y0 + 0.3 * size])
    y1 = min(below + [box.y1 - 0.3 * size])
    x0 = max(left + [box.x0 + 2.5])
    x1 = min(right + [box.x1 - 2.5])
    if align == fitz.TEXT_ALIGN_LEFT:
        x0 = max(x0, b.x0)
    elif left or right:
        middle = (b.x0 + b.x1) / 2
        half = min(middle - x0, x1 - middle)
        x0, x1 = middle - half, middle + half
    return [fitz.Rect(x0, y0, x1, y1) & crop] + loose


BULLET = "•"


def break_units(text: str) -> list[str]:
    """Unbreakable pieces: the translator marks allowed breaks with "|" and forced ones
    with "||" ("\\n" forces one too); a space allows one; closing punctuation stays with
    the text it closes, and a bullet with the item it opens."""
    units: list[str] = []
    for part in re.split(r"(\n|\|\|)", text):
        if part in ("\n", "||"):
            units.append("\n")
            continue
        for piece in re.split(r"\|| (?=\S)", part):
            if piece and units and units[-1] != "\n" and piece[0] in _NO_LINE_START:
                units[-1] += piece
            elif piece and units and units[-1] == BULLET:
                units[-1] = f"{BULLET} {piece}"
            elif piece:
                units.append(piece)
    return units


def list_breaks(source_lines: list[str], translation: str) -> str:
    """A source label set as a bulleted list, one item per line, keeps one item per
    line: a forced break goes before every bullet but the first."""
    if sum(1 for line in source_lines if line.lstrip().startswith(BULLET)) < 2:
        return translation
    head, *items = translation.split(BULLET)
    if not items:
        return translation
    rebuilt = head + BULLET + items[0]
    for item in items[1:]:
        rebuilt = rebuilt.rstrip("|").rstrip() + "||" + BULLET + item
    return rebuilt


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
        # Forced lines; each one may still wrap at its own break marks, as few times as fits.
        segments, current = [], []
        for unit in units + ["\n"]:
            if unit == "\n":
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(unit)
        lines: list[str] = []
        for segment in segments:
            fitting = _balanced(segment, width, font, size)
            if not fitting:
                return []
            lines += fitting[0]
        return [lines]
    return _balanced(units, width, font, size)


def _balanced(units: list[str], width: float, font: Any, size: float) -> list[list[str]]:
    """For each line count (1..6), the split whose longest line is shortest."""
    n = len(units)
    unit_width = [font.text_length(unit, size) for unit in units]
    space = font.text_length(" ", size)
    # gap[i]: the space joined() sets before unit i when it follows unit i-1 on a line.
    gap = [0.0] + [space if (re.match(r"[A-Za-z0-9(]", units[i][0]) or re.search(r"[A-Za-z0-9)]$", units[i - 1]))
                   else 0.0 for i in range(1, n)]

    def line_width(i: int, j: int) -> float:  # units i..j-1 on one line
        return sum(unit_width[i:j]) + sum(gap[i + 1:j])

    options = []
    # best[k][j]: (longest line, cut positions) setting units[:j] on k lines.
    best: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {(0, 0): (0.0, ())}
    for count in range(1, min(n, 6) + 1):
        for j in range(count, n + 1):
            candidates = [(max(best[count - 1, i][0], line_width(i, j)), best[count - 1, i][1] + (i,))
                          for i in range(count - 1, j) if (count - 1, i) in best]
            best[count, j] = min(candidates)
        cuts = best[count, n][1][1:] + (n,)
        bounds = (0, *cuts)
        options.append([units[bounds[k]:bounds[k + 1]] for k in range(count)])
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
        # CJK ink runs a little past the em box; the margin keeps it off a nearby rule.
        boxes.append(fitz.Rect(x0, y - INK_MARGIN * size, x0 + width, y + size + INK_MARGIN * size))
    return boxes


def ink_height(count: int, size: float) -> float:
    return (count - 1) * size * LINE_HEIGHT + size


def choose(group: dict[str, Any], translation: str, rects: list[Any], align: int, points: list[Any], font: Any,
           knockout: bool, others: list[Any], max_size: float | None = None) -> dict[str, Any]:
    """Score every rectangle, size and arrangement; returns the best placement or a failure.

    ``others`` are the footprints of the other labels: a placement that runs into one
    is taken only when nothing else fits, and is then reported."""
    floor = round(group["size"] * MIN_SCALE, 2)
    center = (group["bbox"].y0 + group["bbox"].y1) / 2
    # A label whose source cut the lines it crossed (knockout) may cross them again and
    # gets a new cut; one that simply sat on a line (a fishbone tick) may cross it too.
    source_crosses = knockout or any(
        hits(l["bbox"] + (0.3, 0.25 * l["size"], -0.3, -0.25 * l["size"]), points) for l in group["lines"])
    best = None
    size = min(group["size"], max_size or group["size"])
    while size >= floor - 1e-6:
        for index, rect in enumerate(rects):
            for lines in arrangements(translation, rect.width, font, size):
                height = ink_height(len(lines), size)
                if height + ROW_CLEARANCE * size > rect.height + 0.01:
                    continue
                top = min(max(center - height / 2, rect.y0), rect.y1 - height)
                drawn = ink_boxes(lines, rect, top, size, align, font)
                collides = not source_crosses and any(hits(box, points) for box in drawn)
                overlaps = any(box.intersects(other) for box in drawn for other in others)
                shape = abs(len(lines) - len(group["lines"])) if group["box"] is None else len(lines)
                score = (collides or overlaps, -size, shape, index)
                if best is None or score < best["score"]:
                    best = {"score": score, "rect": rect, "top": top, "size": size, "lines": lines,
                            "overlaps": overlaps}
        size = round(size - 0.2, 2)
    if best is None:
        return {"status": "does_not_fit", "size": floor}
    best["status"] = ("overlaps_label" if best["overlaps"] else "crosses_line_art") if best["score"][0] else "ok"
    return best


def set_lines(page: Any, lines: list[str], boxes: list[Any], name: str, size: float,
              color: tuple[float, float, float], fonts: Fonts) -> None:
    """Set each line at its own baseline, exactly in the box placement checked (a text
    box would space the lines by the font's own line height and drift down)."""
    for text, box in zip(lines, boxes):
        em_top = box.y0 + INK_MARGIN * size
        page.insert_text((box.x0, em_top + fonts.baseline(name) * size), text, fontname=name, fontsize=size,
                         color=color)


def picture_area(crop: Any, caption: Any) -> Any:
    """The rendered picture stops short of its own caption: the book sets the caption
    as text, and the source caption would stay in English inside the picture."""
    out = _fitz().Rect(crop)
    if out.intersects(caption):
        if caption.y0 - out.y0 < out.y1 - caption.y1:
            out.y0 = caption.y1 + 1
        else:
            out.y1 = caption.y0 - 1
    return out


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
        changed = [(g, list_breaks([l["text"] for l in g["lines"]], t)) for g, t in zip(groups, targets)
                   if t.replace("|", "") != g["text"]]
        out = picture_area(crop, caption)
        source_pieces, source_masks, source_cover = line_art(page, crop)
        patches = {id(g): knockouts_of(g, source_masks, source_pieces) for g, _t in changed}
        for group, _t in changed:
            for line in group["lines"]:
                inset = min(line["bbox"].height * 0.25, line["size"] * 0.3)
                page.add_redact_annot(line["bbox"] + (0.3, inset, -0.3, -inset), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        removed, kept_patches = remove_patches(page, [r for rects in patches.values() for r in rects])
        # An underline goes with its words: painted out, unless another line crosses it.
        underlines = [u for g, _t in changed for u in underlines_of(g, source_pieces)]
        crossing = visible_points(([(a, b, o) for a, b, o in source_pieces
                                    if not any(abs(a.y - u.y0) < 0.3 and abs(b.y - u.y0) < 0.3
                                               and u.x0 - 0.5 <= min(a.x, b.x) and max(a.x, b.x) <= u.x1 + 0.5
                                               for u in underlines)], source_masks, source_cover)) if underlines else PointGrid([])
        kept_underlines = []
        for underline in underlines:
            strip = underline + (-0.4, -0.7, 0.4, 0.7)
            if hits(strip, crossing):
                kept_underlines.append(underline)
                continue
            page.draw_rect(strip, color=None, fill=(1, 1, 1), width=0, overlay=True)
        for name, path in fonts.files.items():
            page.insert_font(fontname=name, fontfile=path)
        points = visible_points(line_art(page, crop))
        footprints = {id(g): g["bbox"] + (0.5, 0.5, -0.5, -0.5) for g in groups}
        rows: list[dict[str, Any]] = []

        def plan_for(group: dict[str, Any], target: str, max_size: float | None = None) -> dict[str, Any]:
            name = "zhb" if "Bold" in group["font"] else "zh"
            align = alignment(group, groups, boxes)
            others = [rect for key, rect in footprints.items() if key != id(group)]
            return choose(group, target, candidate_rects(group, groups, boxes, crop, align), align, points,
                          fonts.objects[name], bool(patches[id(group)]), others, max_size=max_size)

        # Items of one list (a legend: same box, same left edge, same size) share the
        # smallest size any of them needs, so the list stays even.
        plans: dict[int, dict[str, Any]] = {id(group): plan_for(group, target) for group, target in changed}
        for group, target in changed:
            column = [h for h, _t in changed if group["box"] is not None and h["box"] == group["box"]
                      and abs(h["bbox"].x0 - group["bbox"].x0) < 1.5 and h["size"] == group["size"]]
            if len(column) >= 3:
                cap = min(plans[id(h)]["size"] for h in column)
                if cap < plans[id(group)].get("size", cap):
                    plans[id(group)] = plan_for(group, target, max_size=cap)
        ink: list[tuple[Any, float]] = []
        drawn_by: list[tuple[int, Any]] = []
        new_patches: list[Any] = []
        for group, target in changed:
            name = "zhb" if "Bold" in group["font"] else "zh"
            plan = plans[id(group)]
            align = alignment(group, groups, boxes)
            if plan["status"] == "does_not_fit":
                rows.append({"source": group["text"], "target": target.replace("|", ""), "status": "does_not_fit"})
                continue
            boxes_drawn = ink_boxes(plan["lines"], plan["rect"], plan["top"], plan["size"], align, fonts.objects[name])
            drawn_by += [(id(group), box) for box in boxes_drawn]
            if patches[id(group)]:
                for box in boxes_drawn:
                    if hits(box, points):
                        patch = box + (-0.15 * plan["size"], -0.1 * plan["size"], 0.15 * plan["size"], 0.1 * plan["size"])
                        page.draw_rect(patch, color=None, fill=(1, 1, 1), width=0, overlay=True)
                        new_patches.append(patch)
            set_lines(page, plan["lines"], boxes_drawn, name, plan["size"], _rgb(group["color"]), fonts)
            ink += [(box, group["size"]) for box in boxes_drawn]
            rows.append({"source": group["text"], "target": target.replace("|", ""), "status": plan["status"],
                         "size_from": group["size"], "size_to": plan["size"], "lines": len(plan["lines"])})
        # Checks: no cut line without text over it; no source text left.
        final_pieces, _masks, _cover = line_art(page, crop)
        cutters = [(rect, 10 ** 9) for rect in kept_patches + new_patches]
        gaps = [p for p in hidden_points(final_pieces, cutters)
                if not any((box + (-0.3 * s, -0.3 * s, 0.3 * s, 0.3 * s)).contains(p) for box, s in ink)]
        left = page.get_text("text", clip=crop)
        # A number or code the translation keeps is not source text left behind.
        leftovers = [g["text"] for g, t in changed
                     if any(l["text"] in left and l["text"] not in t.replace("|", "") for l in g["lines"] if len(l["text"]) > 3)]
        clashes = sorted({(a, b) for a, box in drawn_by for b, other in drawn_by
                          if a < b and box.intersects(other + (0.2, 0.2, -0.2, -0.2))})
        destination.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(dpi=RENDER_DPI, clip=out, alpha=False).save(destination)
    names = {id(g): g["text"] for g in groups}
    problems = [f"{row['source']} ({row['status']})" for row in rows if row["status"] != "ok"]
    problems += [f"line cut without text near ({round(p.x)}, {round(p.y)})" for p in gaps[:5]]
    problems += [f"source text left: {text}" for text in leftovers]
    problems += [f"labels overlap: {names[a]} / {names[b]}" for a, b in clashes]
    problems += [f"underline kept (another line crosses it) near ({round(u.x0)}, {round(u.y0)})" for u in kept_underlines]
    return {
        "key": exhibit["key"], "page": exhibit["page"], "asset": str(destination), "labels": len(groups),
        "replaced": len(changed), "patches": {"source": sum(len(v) for v in patches.values()), "removed": len(removed),
                                              "kept": len(kept_patches), "new": len(new_patches),
                                              "underlines_removed": len(underlines) - len(kept_underlines)},
        "passed": not problems, "problems": problems, "rows": rows,
    }


def document_body_size(exhibit: dict[str, Any]) -> float:
    return float(exhibit.get("body_size") or 0)


def _rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255


# --- rendering a project's translated diagrams --------------------------------------

# Bump when placement or checks change, so cached renders are redone.
RENDERER_VERSION = 5
RENDER_REPORT = "qa/figure-render.json"


def translated_asset(exhibit: dict[str, Any]) -> str:
    """Project-relative path of a diagram's translated image (one per page of the exhibit)."""
    stem = "exhibit-" + exhibit["key"].replace(".", "-").casefold()
    if exhibit.get("parts", 1) > 1:
        stem += f"-p{exhibit['part']}"
    return f"assets/translated/figures/{stem}.png"


def render_input_hash(exhibit: dict[str, Any], translations: list[str]) -> str:
    from .artifacts import hash_data

    return hash_data({"renderer": RENDERER_VERSION, "exhibit": exhibit, "translations": translations})


def diagram_blocks(project: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every figure-label block in the target, paired with its inventory diagram.

    The n-th block of an exhibit in reading order is the n-th diagram page of
    that exhibit.  A block without a diagram to pair with is returned with
    ``exhibit`` None, so callers report it instead of skipping it.
    """
    from .artifacts import load_json
    from .figures import _target_root
    from .manifest import load_manifest
    from .retrofit import exhibit_blocks

    inventory = load_json(project / "analysis" / "exhibit-inventory.json")["exhibits"]
    pages: dict[str, list[dict[str, Any]]] = {}
    for exhibit in sorted(inventory, key=lambda item: item["page"]):
        if exhibit["classification"] == "diagram":
            pages.setdefault(exhibit["key"], []).append(exhibit)
    seen: dict[str, int] = {}
    out = []
    root = _target_root(project, config)
    for entry in sorted(load_manifest(project)["units"], key=lambda item: item["order"]):
        path = root / f"{entry['unit_id']}.md"
        if not path.is_file():
            continue
        for block in exhibit_blocks(path.read_text(encoding="utf-8")):
            if block["kind"] != "figure-labels":
                continue
            index = seen.get(block["key"], 0)
            seen[block["key"]] = index + 1
            candidates = pages.get(block["key"], [])
            out.append({"unit_id": entry["unit_id"], "key": block["key"],
                        "exhibit": candidates[index] if index < len(candidates) else None,
                        "translations": block["labels"]})
    return out


def render_project_figures(project: Path, config: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """Render every translated diagram whose inputs changed; report every result.

    One exhibit's failure is recorded and the others still render: the report,
    not an exception, is what QA reads.
    """
    from .artifacts import atomic_write_json, load_json, safe_project_path

    report_path = project / RENDER_REPORT
    previous = load_json(report_path).get("renders", {}) if report_path.is_file() else {}
    source_pdf = safe_project_path(project, config["source"]["path"])
    renders: dict[str, Any] = {}
    counts = {"rendered": 0, "cached": 0, "failed": 0}
    for item in diagram_blocks(project, config):
        if item["exhibit"] is None:
            name = f"{item['key']}@{item['unit_id']}"
            renders[name] = {"key": item["key"], "unit_id": item["unit_id"], "passed": False,
                             "problems": ["label block has no diagram in the exhibit inventory"]}
            counts["failed"] += 1
            continue
        exhibit = item["exhibit"]
        asset = translated_asset(exhibit)
        name = asset.rsplit("/", 1)[1].removesuffix(".png")
        digest = render_input_hash(exhibit, item["translations"])
        cached = previous.get(name)
        if not force and cached and cached.get("input_hash") == digest and (project / asset).is_file():
            renders[name] = cached
            counts["cached"] += 1
            continue
        try:
            result = render_diagram(source_pdf, exhibit, item["translations"], project / asset)
        except BTPError as exc:
            result = {"key": exhibit["key"], "page": exhibit["page"], "passed": False, "problems": [str(exc)]}
        result.update(asset=asset, unit_id=item["unit_id"], input_hash=digest)
        renders[name] = result
        counts["rendered" if result["passed"] else "failed"] += 1
    atomic_write_json(report_path, {"schema_version": 1, "renderer": RENDERER_VERSION, "renders": renders})
    return {"report": RENDER_REPORT, **counts,
            "failures": {name: r["problems"][:3] for name, r in renders.items() if not r["passed"]}}


_TABLE_MARKER_RE = re.compile(r'^<!-- /?btp:exhibit-table(?: exhibit="[^"]+")? -->\n?', re.MULTILINE)
_LAST_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<path>[^)\s]+)\)\s*$")


class ExhibitPublisher:
    """Turn exhibit markup into what a reader sees.

    Target text: a figure-label block is removed and the image right before it
    becomes the translated diagram, which must be rendered from exactly these
    labels and have passed its checks.  Source text (the bilingual original
    side): the block is removed and the original image stays.  Rebuilt tables
    lose only their marker comments.  Blocks are paired with diagram pages in
    reading order, so one publisher must see the units in order.
    """

    def __init__(self, project: Path, config: dict[str, Any]) -> None:
        from .artifacts import load_json

        self.project = project
        inventory_path = project / "analysis" / "exhibit-inventory.json"
        inventory = load_json(inventory_path)["exhibits"] if inventory_path.is_file() else None
        self.pages: dict[str, list[dict[str, Any]]] | None = None
        if inventory is not None:
            self.pages = {}
            for exhibit in sorted(inventory, key=lambda item: item["page"]):
                if exhibit["classification"] == "diagram":
                    self.pages.setdefault(exhibit["key"], []).append(exhibit)
        report_path = project / RENDER_REPORT
        self.renders = load_json(report_path).get("renders", {}) if report_path.is_file() else {}
        self.seen: dict[str, int] = {}

    def _translated(self, block: dict[str, Any]) -> str:
        key = block["key"]
        index = self.seen.get(key, 0)
        self.seen[key] = index + 1
        pages = (self.pages or {}).get(key, [])
        if index >= len(pages):
            raise BTPError(f"Exhibit {key}: figure-label block has no diagram in analysis/exhibit-inventory.json")
        exhibit = pages[index]
        asset = translated_asset(exhibit)
        render = self.renders.get(asset.rsplit("/", 1)[1].removesuffix(".png"))
        if (not render or not render.get("passed")
                or render.get("input_hash") != render_input_hash(exhibit, block["labels"])
                or not (self.project / asset).is_file()):
            raise BTPError(f"Exhibit {key} (page {exhibit['page']}) has no passing render of its current labels; "
                           "run render-figures and qa")
        return asset

    def _apply(self, text: str, translated: bool) -> str:
        from .retrofit import exhibit_blocks

        out, cursor = [], 0
        for block in exhibit_blocks(text):
            if block["kind"] != "figure-labels":
                continue
            start, end = block["span"]
            before = text[cursor:start]
            if translated:
                asset = self._translated(block)
                image = _LAST_IMAGE_RE.search(before)
                if not image:
                    raise BTPError(f"Exhibit {block['key']}: no image right before its figure-label block")
                before = before[:image.start("path")] + asset + before[image.end("path"):]
            out.append(before)
            cursor = end
        out.append(text[cursor:])
        return re.sub(r"\n{3,}", "\n\n", _TABLE_MARKER_RE.sub("", "".join(out)))

    def target(self, text: str) -> str:
        return self._apply(text, translated=True)

    def source(self, text: str) -> str:
        return self._apply(text, translated=False)

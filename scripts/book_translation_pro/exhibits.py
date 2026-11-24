"""Source-PDF exhibit inventory: classify each captioned exhibit and recover its content.

Every captioned exhibit gets one treatment, decided by rules on the page geometry
(never by a model):

``table_grid``   numbers set in aligned columns: rebuilt as a real table
``table_lists``  side-by-side lists under bold headings: rebuilt as a table of lists
``diagram``      vector line art carrying text labels: labels are translated in place
``raster``       an embedded picture with no text layer: kept as it is
``uncertain``    none of the rules holds: left untouched and listed for review

A rebuilt table must account for every character of its page region, so it can
replace the extracted text; anything else is ``uncertain``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import BTPError
from .figures import LIST_LEADER_RE, SOURCE_CAPTION_RE, _caption_key, _figure_crop_box

NUMERIC_RE = re.compile(r"^\(?(?:\$\s?)?(?:\d[\d,]*(?:\.\d+)?%?|—|-)\)?$")
NUMBER_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
CONTINUATION_RE = re.compile(r"^\(?\s*continue[sd]\s*\)?$", re.IGNORECASE)
TOTAL_RE = re.compile(r"^totals?\b", re.IGNORECASE)
# Upright rotation for a page whose text runs along this direction.
UPRIGHT_ROTATION = {(0, -1): -90, (0, 1): 90}
GRID_NUMERIC_SHARE = 0.25
MIN_TABLE_ROWS = 2
WRAP_GAP = 0.5            # a gap below this many font sizes continues the same cell or label
LABEL_WRAP_GAP = 0.45
LABEL_MAX_SIZE_RATIO = 1.0
LABEL_TITLE_SIZE_RATIO = 1.05  # text this much larger than the body is a title set in the figure
# A list item in a figure: a bullet, a code with a dash ("FR1—Produce …"), or a number ("1. Understand …").
LIST_MARKER_RE = re.compile(r"^(?:[•▪●◦]\s*|[A-Z]{1,4}\d+\s*[—–]\s*\S|\d{1,2}\.\s+\S)")
LIST_HANG_MAX = 4.0            # a wrapped item line is indented under its text by at most this many font sizes


def _fitz() -> Any:
    try:
        import fitz
    except ImportError as exc:
        raise BTPError("PyMuPDF is required for the exhibit inventory") from exc
    return fitz


# --- text geometry -----------------------------------------------------------------

def _direction(line: dict[str, Any]) -> tuple[int, int]:
    return tuple(round(value) for value in line["dir"])  # type: ignore[return-value]


def body_style(document: Any) -> tuple[str, float]:
    """The font and size that carry the most characters: the running text."""
    weight: Counter[tuple[str, float]] = Counter()
    for page in document:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = span["text"].strip()
                    if text:
                        weight[(span["font"], round(span["size"], 1))] += len(text)
    if not weight:
        return "", 0.0
    return weight.most_common(1)[0][0]


def spans_of(page: Any) -> list[dict[str, Any]]:
    """Upright text spans with glyph-tight boxes (trailing spaces excluded)."""
    fitz = _fitz()
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            if _direction(line) != (1, 0):
                continue
            for span in line["spans"]:
                glyphs = [c for c in span["chars"] if c["c"].strip()]
                if not glyphs:
                    continue
                text = " ".join("".join(c["c"] for c in span["chars"]).split())
                box = fitz.Rect(min(c["bbox"][0] for c in glyphs), span["bbox"][1],
                                max(c["bbox"][2] for c in glyphs), span["bbox"][3])
                out.append({"text": text, "bbox": box, "size": round(span["size"], 1), "font": span["font"],
                            "bold": "Bold" in span["font"], "color": span["color"]})
    return out


def text_lines(spans: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Spans grouped into printed lines, left to right; a lone "$" joins the amount after it."""
    rows: list[list[dict[str, Any]]] = []
    for span in sorted(spans, key=lambda s: (round(s["bbox"].y0), s["bbox"].x0)):
        if rows and abs(rows[-1][0]["bbox"].y0 - span["bbox"].y0) < 1.5:
            rows[-1].append(span)
        else:
            rows.append([span])
    lines = []
    for row in rows:
        row.sort(key=lambda s: s["bbox"].x0)
        merged: list[dict[str, Any]] = []
        for span in row:
            if merged and merged[-1]["text"] == "$":
                merged[-1] = dict(span, text="$" + span["text"], bbox=merged[-1]["bbox"] | span["bbox"])
            else:
                merged.append(dict(span))
        lines.append(merged)
    return lines


def _line_text(line: list[dict[str, Any]]) -> str:
    return " ".join(span["text"] for span in line)


def numeric(text: str) -> bool:
    return bool(NUMERIC_RE.match(text))


def number_tokens(texts: list[str]) -> Counter[str]:
    return Counter(token for text in texts for token in NUMBER_TOKEN_RE.findall(text.replace("$", "")))


def chars(texts: list[str]) -> Counter[str]:
    return Counter(char for text in texts for char in text if not char.isspace())


def upright_page(document: Any, page_number: int) -> tuple[Any, int, Any]:
    """The page turned so most of its text reads left to right.

    A sideways exhibit is shown through a rotated copy; returns (page, rotation,
    holder) where holder keeps the copy alive.
    """
    fitz = _fitz()
    page = document[page_number - 1]
    by_direction: Counter[tuple[int, int]] = Counter()
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            by_direction[_direction(line)] += sum(len(span["text"].strip()) for span in line["spans"])
    direction, _count = by_direction.most_common(1)[0] if by_direction else ((1, 0), 0)
    rotation = UPRIGHT_ROTATION.get(direction, 0)
    if not rotation:
        return page, 0, None
    holder = fitz.open()
    copy = holder.new_page(width=page.rect.height, height=page.rect.width)
    copy.show_pdf_page(copy.rect, document, page_number - 1, rotate=rotation)
    return copy, rotation, holder


# --- tables --------------------------------------------------------------------------

def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def _columns_from_header(header_lines: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for line in header_lines:
        for span in line:
            r = span["bbox"]
            target = next((c for c in columns if _overlap(c["x0"], c["x1"], r.x0, r.x1) > 0), None)
            if target:
                target["x0"], target["x1"] = min(target["x0"], r.x0), max(target["x1"], r.x1)
                target["parts"].append(span)
            else:
                columns.append({"x0": r.x0, "x1": r.x1, "parts": [span]})
    columns.sort(key=lambda c: c["x0"])
    for column in columns:
        title = ""
        for span in sorted(column["parts"], key=lambda s: (s["bbox"].y0, s["bbox"].x0)):
            title = title + span["text"] if title.endswith("-") else (title + " " + span["text"]).strip()
        column["title"] = title
    return columns


def _header_merges_columns(header_lines: list[list[dict[str, Any]]], skeleton: list[dict[str, Any]]) -> bool:
    """Whether a column built from the headings would sit over two data columns."""
    return any(
        sum(_overlap(column["x0"], column["x1"], data["x0"], data["x1"]) > 0 for data in skeleton) >= 2
        for column in _columns_from_header(header_lines))


def _columns_under_skeleton(header_lines: list[list[dict[str, Any]]], skeleton: list[dict[str, Any]]
                            ) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """Data columns titled by the heading words over them; a heading over several
    columns becomes a group heading line instead."""
    columns = [{"x0": c["x0"], "x1": c["x1"], "parts": []} for c in skeleton]
    groups: list[list[str]] = []
    unplaced: list[list[dict[str, Any]]] = []
    for line in header_lines:
        spanning, alone = [], []
        for span in line:
            r = span["bbox"]
            overlaps = [(_overlap(c["x0"], c["x1"], r.x0, r.x1), i) for i, c in enumerate(skeleton)]
            touched = [i for amount, i in overlaps if amount > 0]
            best_amount, best = max(overlaps)
            if not touched:
                alone.append(span)  # a heading over no data (weeks still to come): its own empty column
            elif len(touched) == 1 or best_amount >= 0.7 * r.width:
                columns[best]["parts"].append(span)
            else:
                spanning.append(span["text"])
        if spanning:
            groups.append(spanning)
        if alone:
            unplaced.append(alone)
    for extra in _columns_from_header(unplaced) if unplaced else []:
        columns.append({"x0": extra["x0"], "x1": extra["x1"], "parts": extra["parts"]})
    columns.sort(key=lambda c: c["x0"])
    for column in columns:
        title = ""
        for span in sorted(column["parts"], key=lambda s: (s["bbox"].y0, s["bbox"].x0)):
            title = title + span["text"] if title.endswith("-") else (title + " " + span["text"]).strip()
        column["title"] = title
    return columns, groups


def _columns_from_gaps(lines: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Columns as the runs of x covered by text across all lines (white-space rivers between)."""
    columns: list[dict[str, Any]] = []
    for x0, x1 in sorted((s["bbox"].x0, s["bbox"].x1) for line in lines for s in line):
        if columns and x0 <= columns[-1]["x1"] + 1:
            columns[-1]["x1"] = max(columns[-1]["x1"], x1)
        else:
            columns.append({"x0": x0, "x1": x1, "title": ""})
    return columns


def _column_of(span: dict[str, Any], columns: list[dict[str, Any]]) -> tuple[int, bool]:
    """(column index, whether the span actually overlaps it)."""
    r = span["bbox"]
    best = max(range(len(columns)), key=lambda i: _overlap(columns[i]["x0"], columns[i]["x1"], r.x0, r.x1))
    if _overlap(columns[best]["x0"], columns[best]["x1"], r.x0, r.x1) > 0:
        return best, True
    center = (r.x0 + r.x1) / 2
    return min(range(len(columns)), key=lambda i: abs((columns[i]["x0"] + columns[i]["x1"]) / 2 - center)), False


def _data_block(lines: list[list[dict[str, Any]]]) -> tuple[int, int] | None:
    """The longest run of lines that carry a number and more than one cell.

    A one-cell line between such lines (a section label like "Cost of Sales")
    stays inside the run.
    """
    data = [any(numeric(s["text"]) for s in line) and len(line) >= 2 for line in lines]
    best: tuple[int, int] | None = None
    i = 0
    while i < len(lines):
        if data[i]:
            j = i
            while j < len(lines) and (data[j] or (len(lines[j]) == 1 and j + 1 < len(lines) and data[j + 1])):
                j += 1
            if best is None or j - i > best[1] - best[0]:
                best = (i, j)
            i = j
        else:
            i += 1
    return best


def rebuild_table(lines: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Rebuild a typeset table from its printed lines, or None when it is not one."""
    continues = bool(lines) and bool(CONTINUATION_RE.match(_line_text(lines[-1])))
    if continues:
        lines = lines[:-1]
    spans = [s for line in lines for s in line]
    if not spans:
        return None
    share = sum(numeric(s["text"]) for s in spans) / len(spans)
    block = _data_block(lines)
    if share >= GRID_NUMERIC_SHARE and block and block[1] - block[0] >= MIN_TABLE_ROWS:
        first, end = block
        start = first
        while start > 0 and not any(numeric(s["text"]) for s in lines[start - 1]):
            start -= 1
        header_lines, body_lines = lines[start:first], lines[first:]
        group_headings: list[list[str]] = []
        skeleton = _columns_from_gaps([line for line in body_lines if len(line) >= 2])
        from_skeleton = bool(header_lines) and len(skeleton) >= 3 and _header_merges_columns(header_lines, skeleton)
        if from_skeleton:
            # A heading over several columns ("VALUE STREAMS") or a stacked heading
            # would join the data columns under it: the data rows set the columns,
            # each heading word goes to the column it sits over, and a heading
            # spanning columns is kept as a group heading above the table.
            columns, group_headings = _columns_under_skeleton(header_lines, skeleton)
        elif header_lines:
            columns = _columns_from_header(header_lines)
        else:
            columns = _columns_from_gaps(body_lines)
        if header_lines and not from_skeleton:
            # Text under no heading (a row-label column the header leaves blank)
            # gets its own untitled columns rather than joining the nearest heading.
            loose = [[s for s in line if not _column_of(s, columns)[1]] for line in body_lines]
            extra = _columns_from_gaps([line for line in loose if line])
            for column in extra:
                if not any(_overlap(c["x0"], c["x1"], column["x0"], column["x1"]) > 0 for c in columns):
                    columns.append(dict(column, title=""))
            columns.sort(key=lambda c: c["x0"])
        if len(columns) < 2:
            return None
        # Headings are narrower than their column's text: widen columns to the
        # labels assigned to them, then assign again (a stray TOTALS finds its column).
        for line in body_lines:
            for span in line:
                if not numeric(span["text"]):
                    column = columns[_column_of(span, columns)[0]]
                    column["x0"], column["x1"] = min(column["x0"], span["bbox"].x0), max(column["x1"], span["bbox"].x1)
        rows: list[list[str]] = []
        tops: list[float] = []
        previous_bottom = None
        for line in body_lines:
            cells = [""] * len(columns)
            for span in line:
                index = _column_of(span, columns)[0]
                cells[index] = re.sub(r"\$\s+", "$", (cells[index] + " " + span["text"]).strip())
            wrapped = (rows and not cells[0] and not any(numeric(s["text"]) for s in line)
                       and previous_bottom is not None and line[0]["bbox"].y0 - previous_bottom < WRAP_GAP * line[0]["size"])
            if wrapped:
                rows[-1] = [(a + " " + b).strip() for a, b in zip(rows[-1], cells)]
            else:
                rows.append(cells)
                tops.append(line[0]["bbox"].y0)
            previous_bottom = line[0]["bbox"].y1
        numeric_columns = [
            i for i in range(len(columns))
            if sum(numeric(r[i]) for r in rows if r[i]) >= 0.6 * max(1, sum(1 for r in rows if r[i]))
        ]
        return {
            "mode": "grid", "header": [c["title"] for c in columns] if header_lines else None,
            "meta": [[s["text"] for s in line] for line in lines[:start]] + group_headings, "rows": rows, "row_tops": tops,
            "numeric_columns": numeric_columns, "continues": continues,
        }
    header_lines = [line for line in lines if all(s["bold"] for s in line)]
    body_lines = [line for line in lines if not all(s["bold"] for s in line)]
    if not header_lines or not body_lines or any(lines.index(h) > lines.index(body_lines[0]) for h in header_lines):
        return None
    columns = _columns_from_header(header_lines)
    if len(columns) < 2:
        return None
    items: list[list[str]] = [[] for _ in columns]
    last: list[float | None] = [None] * len(columns)
    for line in body_lines:
        for span in line:
            index, overlaps = _column_of(span, columns)
            if not overlaps and abs(span["bbox"].x0 - columns[index]["x0"]) > 6:
                return None  # text outside every heading's column: not a list table
            gap = None if last[index] is None else span["bbox"].y0 - last[index]
            if gap is not None and gap < WRAP_GAP * span["size"]:
                items[index][-1] += " " + span["text"]
            else:
                items[index].append(span["text"])
            last[index] = span["bbox"].y1
    if sum(len(column) >= 2 for column in items) < 2:
        return None
    return {"mode": "lists", "header": [c["title"] for c in columns], "meta": [], "items": items,
            "rows": [["<br>".join(column) for column in items]], "numeric_columns": [], "continues": continues}


def _value(text: str) -> tuple[float, bool] | None:
    """(amount, is_percent) of a numeric cell, or None; "(3,000)" is negative."""
    text = text.replace("$", "").replace(",", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    if text in ("—", "-"):
        return 0.0, False
    try:
        amount = float(text.rstrip("%"))
    except ValueError:
        return None
    return (-amount if negative else amount), text.endswith("%")


# A total off by more than this share of itself sums different rows than the block
# above it (profit = revenue - costs); only a smaller miss looks like a misprint.
MISPRINT_MAX_SHARE = 0.25


def merged_cells(rows: list[list[str]]) -> list[str]:
    """Cells holding two numbers side by side: columns the rebuild could not tell
    apart (a stacked header shifted them).  Characters are all there, structure is not."""
    # Lines of one cell (a list set with <br>) are judged one by one.
    return [cell for row in rows for cell in row
            if any(sum(1 for word in line.split() if numeric(word)) >= 2 for line in cell.split("<br>"))]


def table_checks(table: dict[str, Any], region_texts: list[str]) -> dict[str, Any]:
    """Every character of the region is in the table; totals agree with their rows.

    A total that disagrees beyond rounding (one unit per summed row) is reported
    as a possible source misprint; the numbers themselves are never changed here.
    """
    cells = [c for row in table["rows"] for c in row] + [c for row in table["meta"] for c in row] + (table["header"] or [])
    flat = [part for cell in cells for part in cell.split("<br>")]
    source, rebuilt = chars(region_texts), chars(flat + (["continues"] if table["continues"] else []))
    report: dict[str, Any] = {
        "characters_missing": "".join(sorted((source - rebuilt).elements()))[:80],
        "characters_extra": "".join(sorted((rebuilt - source).elements()))[:80],
        "numbers": sum(number_tokens(region_texts).values()),
        "totals": [],
    }
    if table["mode"] != "grid":
        return report
    report["merged_cells"] = merged_cells([table["header"] or []] + table["rows"])
    rows = table["rows"]
    total_index = next((i for i, row in enumerate(rows) if any(TOTAL_RE.match(c) for c in row if c)), None)
    if total_index is None:
        return report
    tops = table["row_tops"]
    pitches = sorted(b - a for a, b in zip(tops, tops[1:]))
    pitch = pitches[len(pitches) // 2] if pitches else 0.0
    start = 0
    for r in range(1, total_index):
        if tops[r] - tops[r - 1] > 1.3 * pitch:
            start = r  # a wider gap (revenue set apart above the costs) starts the summed block
    for col in table["numeric_columns"]:
        total = _value(rows[total_index][col])
        if total is None:
            continue
        stated, percent = total
        # Amounts and percentages set in one column are summed separately.
        values = [v[0] for r in range(start, total_index) if (v := _value(rows[r][col])) is not None and v[1] == percent]
        if len(values) < 2:
            continue  # one value above a total (a growth-rate column) is not a sum
        computed = sum(values)
        diff = abs(computed - stated)
        if diff < 0.01:
            verdict = "exact"
        elif diff <= len(values) * (0.1 if percent else 1):
            verdict = "rounding"
        elif stated and diff <= MISPRINT_MAX_SHARE * abs(stated):
            verdict = "mismatch"
        else:
            verdict = "not_checkable"
        report["totals"].append({
            "column": (table["header"] or [f"column {col + 1}" for col in range(len(rows[0]))])[col],
            "stated": stated, "sum_of_rows": round(computed, 2), "verdict": verdict,
        })
    return report


# --- diagrams ------------------------------------------------------------------------

def label_lines(page: Any, crop: Any, caption: Any, body_size: float) -> list[dict[str, Any]]:
    """Text lines inside a figure crop that are figure labels (not the caption, not body text)."""
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            glyphs = [c for s in line["spans"] for c in s["chars"] if c["c"].strip()]
            spans = [s for s in line["spans"] if "".join(c["c"] for c in s["chars"]).strip()]
            if not glyphs:
                continue
            fitz = _fitz()
            box = fitz.Rect(line["bbox"])
            box.x0, box.x1 = min(c["bbox"][0] for c in glyphs), max(c["bbox"][2] for c in glyphs)
            if not crop.contains(box) or box.intersects(caption):
                continue
            size = round(spans[0]["size"], 1)
            # Body-size text in the crop is prose beside the figure; a larger one is a title in it.
            if (body_size and body_size * LABEL_MAX_SIZE_RATIO <= size < body_size * LABEL_TITLE_SIZE_RATIO
                    and _direction(line) == (1, 0)):
                continue
            out.append({
                "text": "".join(c["c"] for s in line["spans"] for c in s["chars"]).strip(),
                "bbox": box, "size": size, "font": spans[0]["font"], "color": spans[0]["color"],
                "dir": _direction(line),
                # The reading direction in degrees (0 horizontal, -90 reading upward, -45 slanting up)
                # and each glyph's box: a rotated label is measured, erased and redrawn by them.
                "angle": round(math.degrees(math.atan2(line["dir"][1], line["dir"][0])), 1),
                "char_boxes": [fitz.Rect(c["bbox"]) for c in glyphs],
                "char_quads": [fitz.recover_char_quad(line["dir"], s, c)
                               for s in line["spans"] for c in s["chars"] if c["c"].strip()],
            })
    return out


def reading_box(line: dict[str, Any]) -> Any:
    """A label line's box in its own reading frame (turned so its text runs left to right),
    which is where a rotated label is grouped and placed; a horizontal line's own box."""
    if line["dir"] == (1, 0):
        return line["bbox"]
    fitz = _fitz()
    turn = fitz.Matrix(-line["angle"])
    # The glyphs' own (rotated) quads: their upright boxes would overshoot a slanted line's ends.
    corners = [corner * turn for quad in line["char_quads"] for corner in (quad.ul, quad.ur, quad.ll, quad.lr)]
    return fitz.Rect(min(p.x for p in corners), min(p.y for p in corners),
                     max(p.x for p in corners), max(p.y for p in corners))


def boxes_in(page: Any, crop: Any) -> list[Any]:
    """Closed upright shapes inside the figure, smallest first: the boxes labels sit in.

    A stroked line's bounding rectangle, or a thick arrow (a filled shape with
    slanted sides), would swallow every label beside it, so neither counts.
    """
    boxes = []
    for drawing in page.get_drawings():
        items = drawing["items"]
        if len(items) > 1 and all(item[0] in ("re", "qu") for item in items):
            # One path made of several rectangles (a row of boxes) is several boxes;
            # its overall rectangle would put every label of the row in one box.
            rects = [item[1] if item[0] == "re" else item[1].rect for item in items]
        else:
            closed = drawing.get("closePath") or drawing.get("fill") is not None or any(
                item[0] in ("re", "qu") for item in items)
            upright = all(
                item[0] in ("re", "qu", "c")
                or (item[0] == "l" and (abs(item[1].x - item[2].x) < 0.5 or abs(item[1].y - item[2].y) < 0.5))
                for item in items)
            rects = [drawing["rect"]] if closed and upright else []
        for rect in rects:
            if (crop.contains(rect) and rect.width > 20 and rect.height > 10
                    and rect.get_area() < 0.5 * crop.get_area()):
                boxes.append(rect)
    return sorted(boxes, key=lambda r: r.get_area())


def group_labels(lines: list[dict[str, Any]], boxes: list[Any]) -> list[dict[str, Any]]:
    """Merge label lines into phrases: same style, same box, closely stacked, sharing a
    left edge or a centre.  A neighbouring label set just below (a fishbone branch, a
    metric line in the same box) does not share them.  A list item ("• …", "FR1—…")
    starts its own label, and its wrapped lines, indented under the item's text, join it.
    A rotated line is measured in its own reading frame, and joins only lines of its angle;
    rotated labels follow the horizontal ones."""
    groups: list[dict[str, Any]] = []
    for line in lines:
        line["rbox"] = reading_box(line)

    def order(line: dict[str, Any]) -> tuple[Any, ...]:
        if line["dir"] == (1, 0):
            return (0, 0.0, line["bbox"].y0, line["bbox"].x0)
        return (1, line.get("angle", 0.0), line["rbox"].y0, line["rbox"].x0)

    for line in sorted(lines, key=order):
        probe = line["bbox"] + (1, 1, -1, -1)
        line["box"] = next((i for i, box in enumerate(boxes) if box.contains(probe)), None)
        target = None
        # A slanted line (an axis label set at 45°) is a label of its own: parallel neighbours
        # look stacked in the reading frame without belonging together.
        if LIST_MARKER_RE.match(line["text"]) or line.get("angle", 0.0) % 90:
            groups.append({"lines": [line]})
            continue
        here = line["rbox"]
        for group in groups:
            last, first = group["lines"][-1], group["lines"][0]
            there = last["rbox"]
            gap = here.y0 - there.y1
            same_left = abs(here.x0 - there.x0) < 1.5
            same_center = abs((here.x0 + here.x1) - (there.x0 + there.x1)) / 2 < 2.5
            indent = here.x0 - first["rbox"].x0
            hanging = (len(group["lines"]) == 1 and bool(LIST_MARKER_RE.match(first["text"]))
                       and 1.5 <= indent <= LIST_HANG_MAX * line["size"])
            if (last["font"] == line["font"] and abs(last["size"] - line["size"]) < 0.2 and last["color"] == line["color"]
                    and last["box"] == line["box"] and last["dir"] == line["dir"]
                    and last.get("angle", 0.0) == line.get("angle", 0.0)
                    and -1.5 <= gap < LABEL_WRAP_GAP * line["size"] and (same_left or same_center or hanging)):
                target = group
        if target:
            target["lines"].append(line)
        else:
            groups.append({"lines": [line]})
    for group in groups:
        group["text"] = " ".join(l["text"] for l in group["lines"])
        group["bbox"] = group["lines"][0]["bbox"]
        group["rbox"] = group["lines"][0]["rbox"]
        for l in group["lines"][1:]:
            group["bbox"] = group["bbox"] | l["bbox"]
            group["rbox"] = group["rbox"] | l["rbox"]
        first = group["lines"][0]
        group.update(box=first["box"], size=first["size"], font=first["font"], color=first["color"], dir=first["dir"],
                     angle=first.get("angle", 0.0))
    return groups


# Indexed symbols in a run: "R8 . . . Rn", "V1 … Vn" (a subscript letter is not a word).
SYMBOL_RUN_RE = re.compile(r"[A-Z][a-z]?\d*(?:[\s.…⋮]+[A-Z][a-z]?\d*)+")


def keeps_source_form(text: str) -> bool:
    """Labels that read the same in any language: numbers, codes, symbols."""
    return (not re.search(r"[A-Za-z]{2,}", text) or bool(re.fullmatch(r"[A-Z]{1,4}\d*", text))
            or bool(SYMBOL_RUN_RE.fullmatch(text) and re.search(r"\d", text)))


def _slanted_art(page: Any, region: Any) -> bool:
    """Diagonal strokes or arrowheads in the region: drawn relations, not table rules."""
    for drawing in page.get_drawings():
        if not region.intersects(drawing["rect"]):
            continue
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.x - b.x) > 2 and abs(a.y - b.y) > 2 and abs(a - b) > 8:
                    return True
    return False


# --- inventory -----------------------------------------------------------------------

def _captions(page: Any) -> list[dict[str, Any]]:
    found = []
    for line in text_lines(spans_of(page)):
        text = _line_text(line)
        match = SOURCE_CAPTION_RE.match(text)
        if not match or LIST_LEADER_RE.search(text) or line[0]["size"] > 12:
            continue
        box = line[0]["bbox"]
        for span in line[1:]:
            box = box | span["bbox"]
        found.append({"key": _caption_key(match.group(2)), "label": match.group(1), "text": text, "bbox": box})
    return found


def _table_lines(page: Any, caption: dict[str, Any], body: tuple[str, float]) -> tuple[list[list[dict[str, Any]]], str]:
    """The run of non-body lines attached to the caption, on the side that has more of them."""
    lines = text_lines(spans_of(page))
    index = next(i for i, line in enumerate(lines) if abs(line[0]["bbox"].y0 - caption["bbox"].y0) < 1.5)

    def stop(line: list[dict[str, Any]]) -> bool:
        is_body = all(s["font"] == body[0] and abs(s["size"] - body[1]) < 0.3 for s in line)
        # Text larger than the body is a heading after the exhibit, never part of it.
        is_heading = any(s["size"] > body[1] + 0.5 for s in line)
        return is_body or is_heading or bool(SOURCE_CAPTION_RE.match(_line_text(line)))

    def caption_tail(line: list[dict[str, Any]]) -> bool:
        return bool(CONTINUATION_RE.match(_line_text(line)))

    below: list[list[dict[str, Any]]] = []
    for line in lines[index + 1:]:
        if stop(line):
            break
        below.append(line)
    above: list[list[dict[str, Any]]] = []
    for line in reversed(lines[:index]):
        if stop(line) or line[0]["bbox"].y0 < page.rect.height * 0.1:
            break
        above.insert(0, line)
    # "Continued" set right under a continuation caption belongs to the caption.
    if below and caption_tail(below[0]):
        below = below[1:]
    # Orientation names where the exhibit sits relative to its caption, as figures.py does.
    return (below, "below_caption") if len(below) >= len(above) else (above, "above_caption")


def _region(lines: list[list[dict[str, Any]]]) -> Any:
    region = None
    for line in lines:
        for span in line:
            region = span["bbox"] if region is None else region | span["bbox"]
    return region


def classify(page: Any, caption: dict[str, Any], body: tuple[str, float], avoid: list[Any]) -> dict[str, Any]:
    """One exhibit's treatment, with the evidence for it."""
    fitz = _fitz()
    lines, orientation = _table_lines(page, caption, body)
    region = _region(lines)
    table = rebuild_table(lines) if lines else None
    if table and region is not None and not _slanted_art(page, region):
        # Body text inside the table's area would be lost from the rebuilt table.
        strays = [
            _line_text(line) for line in text_lines(spans_of(page))
            if region.contains(_region([line])) and line not in lines
            and not SOURCE_CAPTION_RE.match(_line_text(line)) and not CONTINUATION_RE.match(_line_text(line))
        ]
        region_texts = [s["text"] for line in lines for s in line]
        checks = table_checks(table, region_texts)
        if not strays and not checks["characters_missing"] and not checks["characters_extra"]:
            return {"classification": f"table_{table['mode']}", "orientation": orientation,
                    "region": [round(v, 2) for v in region], "table": _public_table(table), "checks": checks,
                    "reason": f"{len(table['rows'])} rows rebuilt; every character of the region accounted for"}
        reason = f"table rebuild incomplete (strays={len(strays)}, missing={checks['characters_missing']!r})"
    else:
        reason = "no table structure" if not table else "slanted line art in the table region"
    crop_info = _figure_crop_box(page, list(caption["bbox"]), avoid=avoid)
    if crop_info is None:
        return {"classification": "uncertain", "reason": reason + "; no adjacent figure geometry"}
    crop = crop_info[0]
    labels = label_lines(page, crop, caption["bbox"], body[1])
    images = [rect for image in page.get_images(full=True) for rect in page.get_image_rects(image[0])]
    covered = sum((rect & crop).get_area() for rect in images if rect.intersects(crop))
    if not labels:
        if covered > 0.3 * crop.get_area():
            return {"classification": "raster", "crop": [round(v, 2) for v in crop],
                    "reason": "embedded picture without a text layer; kept as is"}
        return {"classification": "uncertain", "crop": [round(v, 2) for v in crop], "reason": reason + "; no labels"}
    groups = group_labels(labels, boxes_in(page, crop))
    return {
        "classification": "diagram", "crop": [round(v, 2) for v in crop], "orientation": crop_info[1],
        "labels": [{"text": g["text"], "keep": keeps_source_form(g["text"]), "lines": len(g["lines"]),
                    "bbox": [round(v, 2) for v in g["bbox"]], **({"angle": g["angle"]} if g["angle"] else {})}
                   for g in groups],
        "reason": f"{len(groups)} label(s) on vector line art",
    }


def _public_table(table: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in table.items() if key != "row_tops"} | {"row_tops": [round(t, 2) for t in table.get("row_tops", [])]}


def build_inventory(source_pdf: Path) -> dict[str, Any]:
    """Classify every captioned exhibit in a PDF (one entry per exhibit page)."""
    fitz = _fitz()
    exhibits: list[dict[str, Any]] = []
    with fitz.open(source_pdf) as document:
        body = body_style(document)
        for number in range(1, document.page_count + 1):
            page, rotation, _holder = upright_page(document, number)
            claimed: list[Any] = []
            for caption in _captions(page):
                decision = classify(page, caption, body, claimed)
                if decision.get("crop"):
                    claimed.append(fitz.Rect(decision["crop"]))
                exhibits.append({
                    "key": caption["key"], "label": caption["label"], "page": number, "rotation": rotation,
                    "caption": caption["text"], "caption_bbox": [round(v, 2) for v in caption["bbox"]],
                    "body_size": body[1], **decision,
                })
    keys = Counter(item["key"] for item in exhibits)
    for item in exhibits:
        item["part"] = sum(1 for other in exhibits if other["key"] == item["key"] and other["page"] <= item["page"])
        item["parts"] = keys[item["key"]]
    return {"schema_version": 1, "body_style": {"font": body[0], "size": body[1]}, "exhibits": exhibits}

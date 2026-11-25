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

_NUMBER = r"(?:\d[\d,]*(?:\.\d+)?%?|—|-)"
# A number, "(3,000)" or "$(5,061.07)" negative; a lone closing bracket ("100%)") ends a heading, not a number.
NUMERIC_RE = re.compile(rf"^(?:\$\s?)?(?:\((?:\$\s?)?{_NUMBER}\)|{_NUMBER})$")
# A number with its unit word ("4–6 weeks", "$30 million", "2 hours–2 days"): data, though not summable.
QUANTITY_RE = re.compile(r"^\$?\s?\d[\d,.]*\s*(?:[–-]\s*\d[\d,.]*\s*)?[a-z]+(?:\s*[–-]\s*\d[\d,.]*\s*[a-z]+)?$")
# A footnote printed under a table.
NOTE_RE = re.compile(r"^(?:[*†‡]|Notes?\b|Sources?\b)")
FOLIO_TEXT_RE = re.compile(r"\d+|[ivxlc]+")
# A single line this wide across the table region is a sentence about the table, not a heading.
SENTENCE_SHARE = 0.4
# A table reaching this far down its page may run on to the next one.
RUNS_ON_SHARE = 0.75
# The part on the next page must start within this share of that page's height.
CONTINUES_AT_TOP_SHARE = 0.3
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
BULLET_RE = re.compile(r"^[•▪●◦]")
LIST_HANG_MAX = 4.0          # a wrapped item line is indented under its text by at most this many font sizes
NUMBER_GAP = 0.8               # numbers in one span this many font sizes apart sit in separate columns


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
                for piece in _numbers_apart(span["chars"], span["size"]) or [glyphs]:
                    box = fitz.Rect(min(c["bbox"][0] for c in piece), span["bbox"][1],
                                    max(c["bbox"][2] for c in piece), span["bbox"][3])
                    out.append({"text": "".join(c["c"] for c in piece) if piece is not glyphs else text,
                                "bbox": box, "size": round(span["size"], 1), "font": span["font"],
                                "bold": "Bold" in span["font"], "color": span["color"]})
    return out


def _numbers_apart(chars: list[dict[str, Any]], size: float) -> list[list[dict[str, Any]]] | None:
    """The words of a span that prints numbers a column gap apart as one span
    ("($364,399)   ($57,940)" across two columns), so each is its own cell; None
    for any other span (a word that is not a number, or words set close)."""
    words: list[list[dict[str, Any]]] = []
    for char in chars:
        if char["c"].strip():
            if not words or words[-1] is None:
                words.append([])
            words[-1].append(char)
        elif words and words[-1] is not None:
            words.append(None)  # a space: the next glyph starts a word
    words = [word for word in words if word]
    if len(words) < 2 or not all(numeric("".join(c["c"] for c in word)) for word in words):
        return None
    gaps = [after[0]["bbox"][0] - before[-1]["bbox"][2] for before, after in zip(words, words[1:])]
    return words if min(gaps) >= NUMBER_GAP * size else None


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


def quantity(text: str) -> bool:
    """A number, or a number with its unit word: what makes a line a data row."""
    return numeric(text) or bool(QUANTITY_RE.match(text))


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


def _join_heading(title: str, text: str) -> str:
    """A heading word broken at a hyphen ("Non-Value" / "-Add", "Responsive-" / "ness") joins without a space."""
    if title.endswith("-") or text.startswith("-"):
        return title + text
    return (title + " " + text).strip()


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
            title = _join_heading(title, span["text"])
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
            title = _join_heading(title, span["text"])
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


def _unbridged(lines: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """The lines less any word set across two columns of numbers ("Opening inventory"
    over two value-stream columns, labelling a total beside them): finding columns
    by the gaps, such a label would join the columns it straddles.  It still goes
    to the column it overlaps most."""
    numbers = _columns_from_gaps([[s for s in line if quantity(s["text"])] for line in lines])
    return [[s for s in line if quantity(s["text"])
             or sum(_overlap(c["x0"], c["x1"], s["bbox"].x0, s["bbox"].x1) > 0 for c in numbers) < 2]
            for line in lines]


def _column_of(span: dict[str, Any], columns: list[dict[str, Any]]) -> tuple[int, bool]:
    """(column index, whether the span actually overlaps it)."""
    r = span["bbox"]
    best = max(range(len(columns)), key=lambda i: _overlap(columns[i]["x0"], columns[i]["x1"], r.x0, r.x1))
    if _overlap(columns[best]["x0"], columns[best]["x1"], r.x0, r.x1) > 0:
        return best, True
    center = (r.x0 + r.x1) / 2
    return min(range(len(columns)), key=lambda i: abs((columns[i]["x0"] + columns[i]["x1"]) / 2 - center)), False


def _is_data(line: list[dict[str, Any]]) -> bool:
    return len(line) >= 2 and any(quantity(s["text"]) for s in line)


def _data_runs(lines: list[list[dict[str, Any]]]) -> list[tuple[int, int]]:
    """The runs of lines that carry a number and more than one cell, top to bottom.

    A one-cell line between such lines (a section label like "Cost of Sales")
    stays inside the run.
    """
    data = [_is_data(line) for line in lines]
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if data[i]:
            j = i
            while j < len(lines) and (data[j] or (len(lines[j]) == 1 and j + 1 < len(lines) and data[j + 1])):
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _without_lone_labels(columns: list[dict[str, Any]], lines: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The columns, less any that hold a single word and no number (a TOTALS set left
    of its column): that word joins the nearest column instead of opening its own."""
    kept = []
    for column in columns:
        members = [s for line in lines for s in line
                   if _overlap(column["x0"], column["x1"], s["bbox"].x0, s["bbox"].x1) > 0]
        if len(columns) > 2 and len(members) == 1 and not quantity(members[0]["text"]):
            continue
        kept.append(column)
    return kept


def _sections(lines: list[list[dict[str, Any]]], runs: list[tuple[int, int]]
              ) -> tuple[int, list[tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]], list[str]]]]:
    """Split a table region into the tables set one under another.

    Returns where the first table's heading starts (the lines above are text about
    the table) and, per table, its heading lines, data lines and footnotes.  A run
    of data under a new heading of several cells is a new table; a run under
    one-cell lines continues the table above; a short run with no heading above
    the first table is text about it.
    """
    bounds: list[list[int]] = []  # [data start, data end, heading start]
    previous_end = 0
    for i, j in runs:
        headed = any(len(line) >= 2 for line in lines[previous_end:i] if not NOTE_RE.match(_line_text(line)))
        if bounds and not headed:
            bounds[-1][1] = j
        elif headed or j - i >= MIN_TABLE_ROWS:
            bounds.append([i, j, previous_end])
        previous_end = j
    # The first heading: the lines without numbers right above the data, up to a
    # sentence set across the table (an instruction, kept above it as text).
    first = bounds[0][0]
    start = first
    while start > bounds[0][2] and not any(quantity(s["text"]) for s in lines[start - 1]):
        start -= 1
    width = _region(lines).width
    for k in range(first - 1, start - 1, -1):
        if len(lines[k]) == 1 and lines[k][0]["bbox"].width >= SENTENCE_SHARE * width:
            x0 = lines[k][0]["bbox"].x0
            start = k + 1
            while start < first and len(lines[start]) == 1 and abs(lines[start][0]["bbox"].x0 - x0) < 1.5:
                start += 1  # the sentence's last words, wrapped under it
            break
    out: list[tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]], list[str]]] = []
    for n, (i, j, _head) in enumerate(bounds):
        heading = lines[start:i] if n == 0 else lines[bounds[n - 1][1]:i]
        if n:
            # Footnote lines under a table come before the next table's heading.
            k = 0
            while k < len(heading) and NOTE_RE.match(_line_text(heading[k])):
                k += 1
            out[-1][2].extend(_line_text(line) for line in heading[:k])
            heading = heading[k:]
        if n + 1 < len(bounds):
            body, notes = lines[i:j], []
        else:
            # Under the last data: footnotes, and before them closing rows that are still the table.
            tail = lines[j:]
            at = next((k for k, line in enumerate(tail) if NOTE_RE.match(_line_text(line))), len(tail))
            body, notes = lines[i:j] + tail[:at], [_line_text(line) for line in tail[at:]]
        out.append((heading, body, notes))
    return start, out


def _grid(header_lines: list[list[dict[str, Any]]], body_lines: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
    """One table: columns from its heading lines (or from the data), rows from the lines under them."""
    group_headings: list[list[str]] = []
    skeleton = _without_lone_labels(_columns_from_gaps(_unbridged([line for line in body_lines if len(line) >= 2])),
                                    body_lines)
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
        extra = _without_lone_labels(_columns_from_gaps([line for line in loose if line]), loose)
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
        close = previous_bottom is not None and line[0]["bbox"].y0 - previous_bottom < WRAP_GAP * line[0]["size"]
        lead = next((k for k, cell in enumerate(cells) if cell), 0)
        # A row label wrapped onto the line that carries the row's values ("Manage legal,
        # military, and academic / records and relationships  $6,033,439"): the line goes
        # on in lower case under the label of a row that has no values yet.
        label_runs_on = (bool(rows) and lead > 0 and cells[lead][:1].islower() and bool(rows[-1][lead])
                         and not numeric(rows[-1][lead]) and not any(numeric(c) for c in rows[-1][1:]))
        wrapped = rows and not cells[0] and close and (
            not any(numeric(s["text"]) for s in line) or label_runs_on)
        if wrapped:
            rows[-1] = [(a + " " + b).strip() for a, b in zip(rows[-1], cells)]
        else:
            rows.append(cells)
            tops.append(line[0]["bbox"].y0)
        previous_bottom = line[0]["bbox"].y1
    header = [c["title"] for c in columns] if header_lines else None
    if header is not None:
        header, rows = _merge_complementary(header, rows)
    numeric_columns = [
        i for i in range(len(header or rows[0]))
        if sum(numeric(r[i]) for r in rows if r[i]) >= 0.6 * max(1, sum(1 for r in rows if r[i]))
    ]
    return {"header": header, "meta": group_headings, "rows": rows, "row_tops": tops, "numeric_columns": numeric_columns}


def _merge_complementary(header: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Fold an untitled text column into the titled column beside it when no row fills both.

    Words set left of their heading ("N/A" under "Value-Adding Activity", a TOTALS
    row label) open a column of their own; they belong to the heading's column.
    A column holding numbers, or one that shares any row with its neighbour,
    stays apart.
    """
    i = 0
    while i < len(header):
        if header[i] or any(numeric(row[i]) for row in rows if row[i]):
            i += 1
            continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(header) and header[j] and not any(row[i] and row[j] for row in rows):
                rows = [row[:j] + [row[j] or row[i]] + row[j + 1:] for row in rows]
                rows = [row[:i] + row[i + 1:] for row in rows]
                header = header[:i] + header[i + 1:]
                break
        else:
            i += 1
    return header, rows


def rebuild_table(lines: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Rebuild a typeset table from its printed lines, or None when it is not one.

    Tables set one under another in the same exhibit become ``sections`` after
    the first; footnotes under a table become its ``notes``.
    """
    continues = bool(lines) and bool(CONTINUATION_RE.match(_line_text(lines[-1])))
    if continues:
        lines = lines[:-1]
    spans = [s for line in lines for s in line]
    if not spans:
        return None
    share = sum(numeric(s["text"]) for s in spans) / len(spans)
    runs = _data_runs(lines)
    if share >= GRID_NUMERIC_SHARE and runs and max(j - i for i, j in runs) >= MIN_TABLE_ROWS:
        start, parts = _sections(lines, runs)
        built = []
        for heading, body, notes in parts:
            section = _grid(heading, body)
            if section is None:
                return None
            built.append(section | ({"notes": notes} if notes else {}))
        first = built[0]
        table: dict[str, Any] = {
            "mode": "grid", "header": first["header"],
            "meta": [[s["text"] for s in line] for line in lines[:start]] + first["meta"],
            "rows": first["rows"], "row_tops": first["row_tops"], "numeric_columns": first["numeric_columns"],
            "continues": continues,
        }
        if "notes" in first:
            table["notes"] = first["notes"]
        if len(built) > 1:
            table["sections"] = built[1:]
        return table
    return (_headed_lists(lines, continues) or _bulleted_lists(lines, continues) or _item_list(lines, continues)
            or _numbered_rows(lines, continues))


ROW_NUMBER_RE = re.compile(r"\d{1,2}")
ROW_GAP = 1.3  # a line this many times the usual line pitch below the last starts a row


def _numbered_rows(lines: list[list[dict[str, Any]]], continues: bool = False,
                   columns: list[dict[str, Any]] | None = None, rows: list[list[str]] | None = None
                   ) -> dict[str, Any] | None:
    """A table of text whose rows are numbered 1, 2, 3 … down the left edge, each
    row running over several lines.

    The columns are the runs of x the rows cover, titled by the heading words over
    them (a heading over two columns is a group heading).  A row starts at its
    number, or below a wider gap than the lines of a row (unnumbered rows under a
    heading).  Within a cell, a line set at the column's left edge starts a new
    item (joined with ``<br>``) unless it goes on in lower case; an indented line
    goes on with the item above.  On a page the table runs on to,
    ``columns`` and ``rows`` carry on from the page before: lines above the next
    number still belong to the last row.
    """
    left = min(line[0]["bbox"].x0 for line in lines)
    starts = [n for n, line in enumerate(lines)
              if ROW_NUMBER_RE.fullmatch(line[0]["text"]) and line[0]["bbox"].x0 - left < 1 and len(line) >= 2]
    numbers = [int(lines[n][0]["text"]) for n in starts]
    carried = [int(row[0]) for row in rows or [] if ROW_NUMBER_RE.fullmatch(row[0])]
    first_number = carried[-1] + 1 if carried else 1
    if not starts or numbers != list(range(first_number, first_number + len(numbers))):
        return None
    groups: list[list[str]] = []
    if columns is None:
        header_lines, body_lines = lines[:starts[0]], lines[starts[0]:]
        if not header_lines or len(starts) < 2:
            return None
        skeleton = _without_lone_labels(_columns_from_gaps(_unbridged(body_lines)), body_lines)
        columns, groups = _columns_under_skeleton(header_lines, skeleton)
        for column in columns:
            column["base"] = min((s["bbox"].x0 for line in body_lines for s in line
                                  if _overlap(column["x0"], column["x1"], s["bbox"].x0, s["bbox"].x1) > 0), default=column["x0"])
    else:
        body_lines = lines
        if rows is None or len(rows[-1]) != len(columns):
            return None
    rows = [list(row) for row in rows or []]
    tops: list[float] = []
    steps = sorted(b[0]["bbox"].y0 - a[0]["bbox"].y0 for a, b in zip(body_lines, body_lines[1:]))
    pitch = steps[len(steps) // 4] if steps else 0.0  # the line pitch inside a row (rows may be one line each)
    for n, line in enumerate(body_lines):
        numbered = bool(ROW_NUMBER_RE.fullmatch(line[0]["text"])) and line[0]["bbox"].x0 - left < 1 and len(line) >= 2
        # Rows stand a wider gap apart than the lines of one row (unnumbered rows under a heading too).
        apart = n > 0 and line[0]["bbox"].y0 - body_lines[n - 1][0]["bbox"].y0 > ROW_GAP * pitch
        if numbered or apart:
            rows.append([""] * len(columns))
            tops.append(line[0]["bbox"].y0)
        elif not rows:
            return None
        touched: set[int] = set()
        for span in line:
            index, overlaps = _column_of(span, columns)
            if not overlaps:
                return None
            cell = rows[-1][index]
            if not cell or index in touched:
                joiner = " " if cell else ""
            else:
                at_edge = abs(span["bbox"].x0 - columns[index]["base"]) < 2
                joiner = "<br>" if at_edge and not span["text"][:1].islower() else " "
            rows[-1][index] = cell + joiner + span["text"]
            touched.add(index)
    titles = [column["title"] for column in columns]
    return {"mode": "grid", "header": titles, "meta": groups, "rows": rows, "row_tops": tops,
            "numeric_columns": [0], "continues": continues,
            "_columns": [{"x0": c["x0"], "x1": c["x1"], "base": c["base"], "title": c["title"]} for c in columns]}


def _headed_lists(lines: list[list[dict[str, Any]]], continues: bool) -> dict[str, Any] | None:
    """Lists side by side, each under a bold heading."""
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


def _bulleted_lists(lines: list[list[dict[str, Any]]], continues: bool) -> dict[str, Any] | None:
    """Bulleted lists side by side under headings in the body face.

    The headings are the lines above the first bullet; the columns are the runs
    of x the list lines cover.  A bullet starts an item, any other text goes on
    with the item above it in its column (bullets set as tight as wrapped lines
    leave nothing else to go by).
    """
    first = next((n for n, line in enumerate(lines) if any(BULLET_RE.match(s["text"]) for s in line)), None)
    if not first:
        return None
    columns: list[list[float]] = []
    for x0, x1, size in sorted((s["bbox"].x0, s["bbox"].x1, s["size"]) for line in lines[first:] for s in line):
        if columns and x0 <= columns[-1][1] + 2 * size:
            columns[-1][1] = max(columns[-1][1], x1)
        else:
            columns.append([x0, x1])
    if len(columns) < 2:
        return None

    def column_of(span: dict[str, Any]) -> int | None:
        hits = [c for c, (x0, x1) in enumerate(columns) if _overlap(x0, x1, span["bbox"].x0, span["bbox"].x1) > 0]
        return hits[0] if len(hits) == 1 else None

    titles = [""] * len(columns)
    for line in lines[:first]:
        for span in line:
            c = column_of(span)
            if c is None:
                return None
            titles[c] = _join_heading(titles[c], span["text"])
    items: list[list[str]] = [[] for _ in columns]
    for line in lines[first:]:
        for span in line:
            c = column_of(span)
            if c is None:
                return None
            if BULLET_RE.match(span["text"]):
                items[c].append(span["text"])
            elif items[c]:
                items[c][-1] += " " + span["text"]
            else:
                return None  # text above the column's first bullet
    if any(len(column) < 2 for column in items):
        return None
    return {"mode": "lists", "header": titles, "meta": [], "items": items,
            "rows": [["<br>".join(column) for column in items]], "numeric_columns": [], "continues": continues}


def _item_list(lines: list[list[dict[str, Any]]], continues: bool) -> dict[str, Any] | None:
    """One list of marked items ("FR1—Produce …", "• …", "1. …"), each wrapped
    under its own text; a line that is neither starts nothing and is no list."""
    items: list[str] = []
    start = 0.0
    for line in lines:
        text, x0, size = _line_text(line), line[0]["bbox"].x0, line[0]["size"]
        if any(b["bbox"].x0 - a["bbox"].x1 > 2 * size for a, b in zip(line, line[1:])):
            return None  # set in columns
        if LIST_MARKER_RE.match(text):
            items.append(text)
            start = x0
        elif items and start - 1 <= x0 <= start + LIST_HANG_MAX * size:
            items[-1] += " " + text
        else:
            return None
    if len(items) < 2 or continues:
        return None
    return {"mode": "list", "header": None, "meta": [], "rows": [[item] for item in items], "numeric_columns": [], "continues": False}


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
    # Lines of one cell (a list set with <br>) are judged one by one; numbers with
    # words between them are text ("Phases 1 and 2"), not two columns.
    return [cell for row in rows for cell in row
            if any(numeric(a) and numeric(b) for line in cell.split("<br>")
                   for a, b in zip(line.split(), line.split()[1:]))]


def table_checks(table: dict[str, Any], region_texts: list[str]) -> dict[str, Any]:
    """Every character of the region is in the table; totals agree with their rows.

    A total that disagrees beyond rounding (one unit per summed row) is reported
    as a possible source misprint; the numbers themselves are never changed here.
    """
    sections = table_sections(table)
    cells = [c for section in sections
             for c in [c for row in section["rows"] for c in row] + [c for row in section["meta"] for c in row]
             + (section["header"] or []) + section.get("notes", [])]
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
    report["merged_cells"] = [cell for section in sections
                              for cell in merged_cells([section["header"] or []] + section["rows"])]
    for section in sections:
        report["totals"] += _totals(section)
    return report


def table_sections(table: dict[str, Any]) -> list[dict[str, Any]]:
    """The tables an exhibit sets one under another: the first, then any ``sections``."""
    return [table] + table.get("sections", [])


def _totals(section: dict[str, Any]) -> list[dict[str, Any]]:
    """Each numeric column's TOTAL row against the sum of the rows above it."""
    totals: list[dict[str, Any]] = []
    rows = section["rows"]
    total_index = next((i for i, row in enumerate(rows) if any(TOTAL_RE.match(c) for c in row if c)), None)
    if total_index is None:
        return totals
    tops = section["row_tops"]
    pitches = sorted(b - a for a, b in zip(tops, tops[1:]))
    pitch = pitches[len(pitches) // 2] if pitches else 0.0
    start = 0
    for r in range(1, total_index):
        if tops[r] - tops[r - 1] > 1.3 * pitch:
            start = r  # a wider gap (revenue set apart above the costs) starts the summed block
    for col in section["numeric_columns"]:
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
        totals.append({
            "column": (section["header"] or [f"column {col + 1}" for col in range(len(rows[0]))])[col],
            "stated": stated, "sum_of_rows": round(computed, 2), "verdict": verdict,
        })
    return totals


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
    lines = text_lines(spans_of(page))
    for n, line in enumerate(lines):
        text = _line_text(line)
        match = SOURCE_CAPTION_RE.match(text)
        if not match or LIST_LEADER_RE.search(text) or line[0]["size"] > 12:
            continue
        if n and _continues_sentence(lines[n - 1], line):
            continue  # prose citing the exhibit, wrapped so the citation starts a line
        box = line[0]["bbox"]
        for span in line[1:]:
            box = box | span["bbox"]
        caption = {"key": _caption_key(match.group(2)), "label": match.group(1), "text": text, "bbox": box}
        # A title too long for one line goes on under itself, in its own face, set tight.
        title = line[-1]
        below = lines[n + 1] if n + 1 < len(lines) else None
        if (below is not None and len(below) == 1 and below[0]["font"] == title["font"]
                and abs(below[0]["size"] - title["size"]) < 0.3
                and -0.5 * title["size"] < below[0]["bbox"].y0 - box.y1 < 0.6 * title["size"]
                # under the title's first word, or flush with the label ("EXHIBIT 5.1")
                and min(abs(below[0]["bbox"].x0 - title["bbox"].x0), abs(below[0]["bbox"].x0 - box.x0)) < 2
                and not SOURCE_CAPTION_RE.match(below[0]["text"])):
            caption["tail"] = below[0]["text"]
            caption["bbox"] = box | below[0]["bbox"]
        found.append(caption)
    return found


def _continues_sentence(above: list[dict[str, Any]], line: list[dict[str, Any]]) -> bool:
    """``line`` goes on with an unfinished sentence set right above it, in the same type
    (a running line at least as wide as this one, so not a label of the figure)."""
    last, first = above[-1], line[0]
    return (last["font"] == first["font"] and abs(last["size"] - first["size"]) < 0.3
            and -0.5 * first["size"] < first["bbox"].y0 - last["bbox"].y1 < 0.6 * first["size"]
            and abs(above[0]["bbox"].x0 - first["bbox"].x0) < 2
            and last["bbox"].x1 >= line[-1]["bbox"].x1 - 2
            and not _line_text(above).rstrip().endswith((".", "!", "?", ":", ";")))


def _ends_exhibit(line: list[dict[str, Any]], body: tuple[str, float]) -> bool:
    """Body text, a heading (text larger than the body), or another caption."""
    is_body = all(s["font"] == body[0] and abs(s["size"] - body[1]) < 0.3 for s in line)
    is_heading = any(s["size"] > body[1] + 0.5 for s in line)
    return is_body or is_heading or bool(SOURCE_CAPTION_RE.match(_line_text(line)))


def vertical_lines(page: Any) -> list[dict[str, Any]]:
    """Text lines set reading up or down the page (a group label beside table rows)."""
    fitz = _fitz()
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if _direction(line) in ((0, 1), (0, -1)):
                text = " ".join(" ".join(span["text"] for span in line["spans"]).split())
                if text:
                    out.append({"text": text, "bbox": fitz.Rect(line["bbox"])})
    return out


def _place_group_labels(table: dict[str, Any], labels: list[dict[str, Any]]) -> list[str]:
    """Put each vertical group label at the first row of the block of rows beside it.

    Rows set apart by a wider gap form a block; the label goes in the first
    column when that column is empty for the block, else in a new first column.
    Returns the labels placed.
    """
    blocks = []  # (section, first row, last row, top, bottom)
    for section in table_sections(table):
        tops = section.get("row_tops", [])
        pitches = sorted(b - a for a, b in zip(tops, tops[1:]))
        pitch = pitches[len(pitches) // 2] if pitches else 0.0
        first = 0
        for r in range(1, len(tops) + 1):
            if r == len(tops) or tops[r] - tops[r - 1] > 1.3 * pitch:
                blocks.append((section, first, r - 1, tops[first], tops[r - 1] + pitch))
                first = r
    placed = []
    for label in sorted(labels, key=lambda item: item["bbox"].y0):
        overlaps = [(_overlap(top, bottom, label["bbox"].y0, label["bbox"].y1), block)
                    for block in blocks for top, bottom in [block[3:5]]]
        if not overlaps or max(amount for amount, _block in overlaps) <= 0:
            continue
        section, first, last, _top, _bottom = max(overlaps, key=lambda item: item[0])[1]
        if any(section["rows"][r][0] for r in range(first, last + 1)):
            section["rows"] = [[""] + row for row in section["rows"]]
            if section["header"] is not None:
                section["header"] = [""] + section["header"]
            section["numeric_columns"] = [c + 1 for c in section["numeric_columns"]]
        section["rows"][first][0] = label["text"]
        placed.append(label["text"])
    return placed


def _table_lines(page: Any, caption: dict[str, Any], body: tuple[str, float]) -> tuple[list[list[dict[str, Any]]], str]:
    """The run of non-body lines attached to the caption, on the side that has more of them."""
    lines = text_lines(spans_of(page))
    index = next(i for i, line in enumerate(lines) if abs(line[0]["bbox"].y0 - caption["bbox"].y0) < 1.5)

    def stop(line: list[dict[str, Any]]) -> bool:
        return _ends_exhibit(line, body)

    def caption_tail(line: list[dict[str, Any]]) -> bool:
        return bool(CONTINUATION_RE.match(_line_text(line)))

    below: list[list[dict[str, Any]]] = []
    for line in lines[index + 1:]:
        if line[0]["bbox"].y0 < caption["bbox"].y1 - 1:
            continue  # the caption's own second line
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
        region_texts = [s["text"] for line in lines for s in line]
        # A group label set up the side of a block of rows (OPERATIONAL, FINANCIAL),
        # in or just left of the table, names that block's first row.  A page number
        # printed up the edge of a sideways page is not one.
        reach = fitz.Rect(region.x0 - 2 * body[1], region.y0 - body[1], region.x1, region.y1 + body[1])
        beside = [v for v in vertical_lines(page)
                  if reach.contains(v["bbox"]) and not FOLIO_TEXT_RE.fullmatch(v["text"])]
        if table["mode"] == "grid" and beside:
            region_texts += _place_group_labels(table, beside)
        strays = _strays(page, lines, region) + [v["text"] for v in beside if v["text"] not in region_texts]
        checks = table_checks(table, region_texts)
        if not strays and not checks["characters_missing"] and not checks["characters_extra"]:
            kind = "text_list" if table["mode"] == "list" else f"table_{table['mode']}"
            return {"classification": kind, "orientation": orientation,
                    "region": [round(v, 2) for v in region], "table": _public_table(table), "checks": checks,
                    "reason": f"{len(table['rows'])} rows rebuilt; every character of the region accounted for",
                    "_texts": region_texts}
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


def _strays(page: Any, lines: list[list[dict[str, Any]]], region: Any) -> list[str]:
    """Text inside the table's area that the rebuild did not take (it would be lost)."""
    return [
        _line_text(line) for line in text_lines(spans_of(page))
        if region.contains(_region([line])) and line not in lines
        and not SOURCE_CAPTION_RE.match(_line_text(line)) and not CONTINUATION_RE.match(_line_text(line))
    ]


def _rounded(section: dict[str, Any]) -> dict[str, Any]:
    return section | {"row_tops": [round(t, 2) for t in section.get("row_tops", [])]}


def _public_table(table: dict[str, Any]) -> dict[str, Any]:
    out = _rounded(table)
    if "sections" in table:
        out["sections"] = [_rounded(section) for section in table["sections"]]
    return out


def _continue_table(item: dict[str, Any], texts: list[str], page: Any, number: int, body: tuple[str, float]) -> None:
    """Read the rest of a table that runs on to this page without a caption.

    The rows at the top of the page, down to the first body text or heading,
    become further sections of the table.  When they cannot be rebuilt whole,
    the exhibit becomes ``uncertain``: half a table is never spliced in.
    """
    lines = [line for line in text_lines(spans_of(page)) if line[0]["bbox"].y0 >= 0.1 * page.rect.height]
    run: list[list[dict[str, Any]]] = []
    for line in lines:
        if _ends_exhibit(line, body):
            break
        run.append(line)
    numbered = item.get("table", {}).get("_columns")
    if numbered:
        # A numbered table goes on where the page carries on its numbering.
        last = max((int(row[0]) for row in item["table"]["rows"] if ROW_NUMBER_RE.fullmatch(row[0])), default=0)
        goes_on = any(line[0]["text"] == str(last + 1) and len(line) >= 2 for line in run)
    else:
        goes_on = sum(_is_data(line) for line in run) >= MIN_TABLE_ROWS
    if not goes_on or run[0][0]["bbox"].y0 > CONTINUES_AT_TOP_SHARE * page.rect.height:
        return
    region = _region(run)
    run_texts = [s["text"] for line in run for s in line]
    if numbered:
        # Numbered rows carry on in the same columns, the first lines finishing the last row.
        more = _numbered_rows(run, columns=numbered, rows=item["table"]["rows"])
        if more is not None:
            checks = table_checks(item["table"] | {"rows": more["rows"]}, texts + run_texts)
            if not _strays(page, run, region) and not checks["characters_missing"] and not checks["characters_extra"]:
                item["table"]["rows"] = more["rows"]
                item["table"]["row_tops"] = item["table"]["row_tops"] + [round(t, 2) for t in more["row_tops"]]
                item["checks"] = checks
                item["continued"] = item.get("continued", []) + [{"page": number, "region": [round(v, 2) for v in region]}]
                item["reason"] = (f"{len(more['rows'])} numbered rows rebuilt over pages {item['page']}–{number}; "
                                  "every character accounted for")
                return
            more = None
    else:
        more = rebuild_table(run)
    checks = table_checks(more, run_texts) if more else None
    if (more is None or more["mode"] != "grid" or _strays(page, run, region)
            or checks["characters_missing"] or checks["characters_extra"]):
        item.update(classification="uncertain",
                    reason=f"the table runs on to page {number}, where its rows could not be rebuilt whole")
        item.pop("table", None)
        item.pop("checks", None)
        return
    table = item["table"]
    table["sections"] = table.get("sections", []) + [
        {key: section[key] for key in ("header", "meta", "rows", "row_tops", "numeric_columns", "notes") if key in section}
        for section in table_sections(_public_table(more))]
    item["checks"] = table_checks(table, texts + run_texts)
    item["continued"] = item.get("continued", []) + [{"page": number, "region": [round(v, 2) for v in region]}]
    rows = sum(len(section["rows"]) for section in table_sections(table))
    item["reason"] = f"{rows} rows rebuilt over pages {item['page']}–{number}; every character accounted for"


def build_inventory(source_pdf: Path) -> dict[str, Any]:
    """Classify every captioned exhibit in a PDF (one entry per exhibit page)."""
    fitz = _fitz()
    exhibits: list[dict[str, Any]] = []
    with fitz.open(source_pdf) as document:
        body = body_style(document)
        # A table at the foot of the last page, which may run on to this one.
        runs_on: tuple[dict[str, Any], list[str]] | None = None
        for number in range(1, document.page_count + 1):
            page, rotation, _holder = upright_page(document, number)
            captions = _captions(page)
            if runs_on is not None and not captions:
                _continue_table(*runs_on, page, number, body)
            runs_on = None
            claimed: list[Any] = []
            for caption in captions:
                decision = classify(page, caption, body, claimed)
                texts = decision.pop("_texts", None)
                if rotation and decision["classification"] == "diagram":
                    # Rendering in place works on upright pages only: say so before
                    # translation, not when the labels come back translated.
                    decision = decision | {"classification": "uncertain",
                                           "reason": "a diagram on a sideways page is not rendered in place"}
                if decision.get("crop"):
                    claimed.append(fitz.Rect(decision["crop"]))
                item = {
                    "key": caption["key"], "label": caption["label"], "page": number, "rotation": rotation,
                    "caption": caption["text"], "caption_bbox": [round(v, 2) for v in caption["bbox"]],
                    **({"caption_tail": caption["tail"]} if "tail" in caption else {}),
                    "body_size": body[1], **decision,
                }
                exhibits.append(item)
                runs_on = None
                if (item["classification"] == "table_grid" and not item["table"]["continues"]
                        and item["region"][3] > RUNS_ON_SHARE * page.rect.height):
                    runs_on = (item, texts)
    keys = Counter(item["key"] for item in exhibits)
    seen: Counter[str] = Counter()
    for item in exhibits:  # in page order, then in the order found on the page
        item.get("table", {}).pop("_columns", None)  # kept only to read a following page
        # Numbered one by one, so two exhibits with one number on a page (Figure 3.1 and
        # Table 3.1) are parts 1 and 2, never both part 2.
        seen[item["key"]] += 1
        item["part"] = seen[item["key"]]
        item["parts"] = keys[item["key"]]
    return {"schema_version": 1, "body_style": {"font": body[0], "size": body[1]}, "exhibits": exhibits}

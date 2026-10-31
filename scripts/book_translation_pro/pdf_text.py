"""PyMuPDF text analysis for PDF sources.

Heading recovery for converted PDFs, a fallback extractor, and a layout
cleaning pass shared by both extraction paths: ``clean_pdf`` writes a copy of
the source without running heads and folios, with vector figures rendered as
images (their labels included), and with ruled tables and linked footnotes
replaced by placeholder tokens; ``restore_layout_markup`` turns the tokens in
the converted Markdown back into pipe tables and Markdown footnotes and rejoins
paragraphs split by a page break.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .artifacts import BTPError, portable_path
from .figures import _figure_crop_box
from .structure import HEADING_RE, IMAGE_RE
from .visual import _block_text, _fitz, _reading_order, detect_layout


# A line is a heading candidate when its font is clearly larger than body text,
# or when it is a short, wholly bold line at body size.
HEADING_SIZE_RATIO = 1.15
MAX_HEADING_CHARS = 120
BOLD_HEADING_MAX_CHARS = 80
BOLD_HEADING_MAX_LINES = 2
MAX_HEADING_LEVELS = 3
# Running heads and folios live in these top/bottom page bands.
MARGIN_FRACTION = 0.08
RUNNING_HEAD_MIN_PAGES = 3
# Bold headings may be set a little smaller than body text.
BOLD_HEADING_SIZE_RATIO = 0.95
# Footnotes are small text in the lower part of a page whose marker also
# appears, set even smaller, in the body of the same page.
FOOTNOTE_SIZE_RATIO = 0.85
FOOTNOTE_TOP_FRACTION = 0.5
REFERENCE_SIZE_RATIO = 0.75
# A framed region holding more text than this is a boxed sidebar, not a figure.
FIGURE_MAX_LABEL_CHARS = 200
FIGURE_DPI = 180
# Pages mostly covered by an image are scans; their figures are left alone.
SCANNED_IMAGE_COVERAGE = 0.5
MIN_IMAGE_PIXELS = 32
PAGE_BREAK_PROBE_CHARS = 40
PAGE_BREAK_SEARCH_BLOCKS = 6
# Arabic folios, or a well-formed roman numeral below 200 (front matter); a
# word spelled with roman letters ("civil", "mix") is not a folio.
_FOLIO_RE = re.compile(r"^(?:\d{1,4}|(?=[ivxlc])c?(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3}))$", re.IGNORECASE)
_MARKUP_RE = re.compile(r"<[^>]+>|[*_`]")
# A bare label such as "Chapter 3" or "Part II" is joined with the title that follows it.
_LABEL_RE = re.compile(
    r"^(?:(?:chapter|part|book|section|appendix|lecture|lesson)\s+[\w.]+|第\s*[\d一二三四五六七八九十百]+\s*[章节部篇]|[\divxlc.]+)$",
    re.IGNORECASE,
)
_HEADING_END_RE = re.compile(r"[.,;:!?。，；：！？]$")
# A page whose last line ends like this does not continue on the next page.
_SENTENCE_END_RE = re.compile(r"[.!?:;。！？：；…\"'”’)\]）」』]$")
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_MARKER_RE = re.compile(r"^(?:\d{1,3}|[*†‡§¶]{1,3})$")
_FOOTNOTE_START_RE = re.compile(r"^(\d{1,3}|[*†‡§¶]{1,3})[.)]?\s+(?=\S)")
FIGURE_CAPTION_RE = re.compile(
    r"^(?:fig(?:ure)?\.?|exhibit|chart|plate|map|diagram|illustration|图|插图)\s*[0-9]+",
    re.IGNORECASE,
)
TABLE_CAPTION_RE = re.compile(r"^(?:table|表)\s*[0-9]+", re.IGNORECASE)
# The label in front of a chapter or part title: "chapter 1", "part iv", "第三章", "1".
_HEADING_LABEL_PREFIX_RE = re.compile(
    r"^(?:(?:chapter|part|book|section)\s+[\w.]+|第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章部节篇卷]|\d{1,3})"
    r"[\s.:：、\-–—]*",
    re.IGNORECASE,
)
# Tokens survive Calibre and PyMuPDF as plain text; Calibre may wrap one in a span.
_TOKEN_RE = re.compile(r"(<span\b[^>]*>\s*)?BTP(TB|FN)(\d{4})Z(?(1)\s*</span>)")
_EMPTY_BLOCK_RE = re.compile(r"^(?:\s|\xa0|</?div\b[^>]*>|<a id=\"[^\"]*\"></a>)*$")


def _normalize(text: str) -> str:
    return " ".join(_MARKUP_RE.sub("", text).split()).casefold()


def _line_text(line: dict[str, Any]) -> str:
    return " ".join(span.get("text", "").strip() for span in line.get("spans", []) if span.get("text", "").strip())


def _line_size(line: dict[str, Any]) -> float:
    return max((float(span.get("size", 0)) for span in line.get("spans", []) if span.get("text", "").strip()), default=0.0)


def _is_bold(span: dict[str, Any]) -> bool:
    return bool(int(span.get("flags", 0)) & 16) or "bold" in str(span.get("font", "")).casefold()


def _text_blocks(page: Any) -> list[dict[str, Any]]:
    return [block for block in page.get_text("dict")["blocks"] if block.get("type") == 0 and _block_text(block)]


def _parse(document: Any) -> list[tuple[Any, list[dict[str, Any]]]]:
    """Each page with its text blocks; layout parsing is the expensive step, so do it once."""
    return [(page, _text_blocks(page)) for page in document]


def _has_text_layer(parsed: list[tuple[Any, list[dict[str, Any]]]]) -> bool:
    """Scanned PDFs have none; they are left to the OCR preflight."""
    return any(blocks for _page, blocks in parsed)


def _body_size(parsed: list[tuple[Any, list[dict[str, Any]]]]) -> float:
    """Most common font size, weighted by character count."""
    sizes: Counter[float] = Counter()
    for _page, blocks in parsed:
        for block in blocks:
            for line in block["lines"]:
                for span in line["spans"]:
                    sizes[round(float(span.get("size", 0)) * 2) / 2] += len(span.get("text", "").strip())
    if not sizes:
        raise BTPError("The PDF has no extractable text layer")
    return sizes.most_common(1)[0][0]


def _heading_levels(parsed: list[tuple[Any, list[dict[str, Any]]]], body: float) -> dict[float, int]:
    sizes = sorted({
        round(_line_size(line) * 2) / 2
        for _page, blocks in parsed for block in blocks for line in block["lines"]
        if _is_heading_line(line, body)
    }, reverse=True)
    return {size: min(index + 1, MAX_HEADING_LEVELS) for index, size in enumerate(sizes)}


def _is_heading_line(line: dict[str, Any], body: float) -> bool:
    text = _line_text(line)
    if not re.search(r"[^\W\d_]", text):
        return False
    size = _line_size(line)
    if size >= body * HEADING_SIZE_RATIO:
        return len(text) <= MAX_HEADING_CHARS
    spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
    return (
        size >= body * BOLD_HEADING_SIZE_RATIO
        and len(text) <= BOLD_HEADING_MAX_CHARS
        and not _HEADING_END_RE.search(text)
        and all(_is_bold(span) for span in spans)
    )


def _is_heading_block(lines: list[dict[str, Any]], body: float) -> bool:
    """Large-font blocks are headings; a bold body-size block only when it is short."""
    if not lines or not all(_is_heading_line(line, body) for line in lines):
        return False
    return len(lines) <= BOLD_HEADING_MAX_LINES or any(_line_size(line) >= body * HEADING_SIZE_RATIO for line in lines)


def _heading_map(parsed: list[tuple[Any, list[dict[str, Any]]]], body: float, levels: dict[float, int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for _page, blocks in parsed:
        for block in blocks:
            if not _is_heading_block(block["lines"], body):
                continue
            for line in block["lines"]:
                result.setdefault(_normalize(_line_text(line)), levels[round(_line_size(line) * 2) / 2])
    return result


def _previous_content(blocks: list[str], index: int) -> int | None:
    for previous in range(index - 1, -1, -1):
        if not _EMPTY_BLOCK_RE.match(blocks[previous]):
            return previous
    return None


def promote_headings(markdown: str, headings: dict[str, int]) -> tuple[str, int]:
    """Give single-line paragraphs and headings that are PDF heading lines their PDF level.

    Calibre's PDF reflow emits most headings as styled paragraphs and guesses
    the level of the rest, which leaves the Book Map without consistent
    chapters; the source PDF's font sizes restore them.  A bare label heading
    ("Chapter 3") is merged with the title heading that follows it.
    """
    blocks = markdown.split("\n\n")
    promoted = 0
    in_fence = False
    for index, block in enumerate(blocks):
        fences = sum(1 for line in block.split("\n") if line.lstrip().startswith(("```", "~~~")))
        stripped = block.strip()
        if not in_fence and "\n" not in stripped and stripped and not stripped.startswith(("|", "!", "<img")):
            existing = HEADING_RE.match(stripped)
            title = " ".join(_MARKUP_RE.sub("", existing.group(2) if existing else stripped).split())
            level = headings.get(_normalize(title))
            if level:
                previous = _previous_content(blocks, index)
                label = HEADING_RE.match(blocks[previous].strip()) if previous is not None else None
                if label and _LABEL_RE.match(_normalize(label.group(2))):
                    level = min(level, len(label.group(1)))
                    title = f"{' '.join(_MARKUP_RE.sub('', label.group(2)).split())} {title}"
                    blocks[previous] = ""
                blocks[index] = "#" * level + " " + title
                if not existing:
                    promoted += 1
        if fences % 2:
            in_fence = not in_fence
    return "\n\n".join(block for block in blocks if block != ""), promoted


def _line_center_y(line: dict[str, Any]) -> float:
    return (float(line["bbox"][1]) + float(line["bbox"][3])) / 2


def _band(page: Any, line: dict[str, Any]) -> str | None:
    height = page.rect.height * MARGIN_FRACTION
    center = _line_center_y(line) - page.rect.y0
    if center <= height:
        return "top"
    if center >= page.rect.height - height:
        return "bottom"
    return None


def _margin_block_band(page: Any, block: dict[str, Any]) -> str | None:
    """The band a block lies in entirely; text that runs on into the body has none."""
    bands = {_band(page, line) for line in block["lines"] if _line_text(line)}
    return bands.pop() if len(bands) == 1 else None


_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def _folio_value(text: str) -> tuple[str, int] | None:
    """("arabic"|"roman", value) for a folio-shaped line, else None."""
    if not _FOLIO_RE.match(text):
        return None
    if text.isdigit():
        return "arabic", int(text)
    values = [_ROMAN_VALUES[char] for char in text.casefold()]
    return "roman", sum(-value if index + 1 < len(values) and value < values[index + 1] else value for index, value in enumerate(values))


def _running_keys(text: str, page_index: int) -> set[tuple[str, int | None]]:
    """Ways a running-head line may repeat: verbatim, or with a folio replaced by "#".

    A folio printed with the head ("12 A History of Work", "Page 3 of 120")
    advances with the page, so its offset from the page index is constant;
    a numbered label ("Problem 7") does not, and never forms a stable key.
    """
    candidates = []
    if match := re.fullmatch(r"(\d{1,4})\s+(.+)", text):
        candidates.append((match.group(2), int(match.group(1))))
    if match := re.fullmatch(r"(.+?)\s+(\d{1,4})", text):
        candidates.append((match.group(1), int(match.group(2))))
    for match in re.finditer(r"\d{1,4}(?=\s*(?:of|/)\s*\d)", text):
        candidates.append((text[:match.start()] + "#" + text[match.end():], int(match.group(0))))
    return {(text, None)} | {(rest, number - page_index) for rest, number in candidates}


def _margin_lines(parsed: list[tuple[Any, list[dict[str, Any]]]], body: float) -> Iterator[tuple[Any, str]]:
    """(page, normalized text) of each non-heading line in a block lying wholly in a margin band."""
    for page, blocks in parsed:
        for block in blocks:
            if _margin_block_band(page, block):
                for line in block["lines"]:
                    if _line_text(line) and _line_size(line) < body * HEADING_SIZE_RATIO:
                        yield page, _normalize(_line_text(line))


def _running_heads(parsed: list[tuple[Any, list[dict[str, Any]]]], body: float) -> set[tuple[str, int | None]]:
    """Margin-band line keys that occur on several pages."""
    occurrences = {
        (page.number, key)
        for page, text in _margin_lines(parsed, body)
        if not _folio_value(text)
        for key in _running_keys(text, page.number)
    }
    seen = Counter(key for _page, key in occurrences)
    return {key for key, count in seen.items() if key[0] and count >= RUNNING_HEAD_MIN_PAGES}


def _folio_offsets(parsed: list[tuple[Any, list[dict[str, Any]]]], body: float) -> set[tuple[str, int]]:
    """Numbering offsets (folio value minus page index) seen on several pages."""
    occurrences = {
        (page.number, (folio[0], folio[1] - page.number))
        for page, text in _margin_lines(parsed, body)
        if (folio := _folio_value(text))
    }
    seen = Counter(offset for _page, offset in occurrences)
    return {offset for offset, count in seen.items() if count >= RUNNING_HEAD_MIN_PAGES}


def _document_profile(parsed: list[tuple[Any, list[dict[str, Any]]]]) -> dict[str, Any]:
    body = _body_size(parsed)
    levels = _heading_levels(parsed, body)
    return {
        "body": body,
        "levels": levels,
        "heading_map": _heading_map(parsed, body, levels),
        "running": _running_heads(parsed, body),
        "folios": _folio_offsets(parsed, body),
        # Chapter-title running heads repeat a heading of the book, usually
        # without its label ("THE FACTORY" for "Chapter 1 The Factory").
        "headings": {
            form
            for _page, blocks in parsed for block in blocks for line in block["lines"]
            if _line_size(line) >= body * HEADING_SIZE_RATIO
            for form in _heading_forms(_normalize(_line_text(line)))
        },
    }


def _heading_forms(text: str) -> set[str]:
    """A heading and, when it carries a chapter or part label, its bare title."""
    bare = _HEADING_LABEL_PREFIX_RE.sub("", text, count=1).strip()
    return {text, bare} if len(bare) >= 3 else {text}


def _furniture_kind(line: dict[str, Any], block: dict[str, Any], page: Any, profile: dict[str, Any]) -> str | None:
    """Classify a running head ("header"/"footer") or folio ("page_number").

    Removal is destructive, so two signals are required: the line's block lies
    wholly in a margin band, and the line is a folio whose number advances with
    the page, repeats on several pages, or repeats a heading of the book.
    Position, font size, or a number alone is never enough: poems and
    epigraphs start at the top of a page in small type, and section numbers
    or years can stand alone at a page edge.
    """
    band = _margin_block_band(page, block)
    if band is None or _line_size(line) >= profile["body"] * HEADING_SIZE_RATIO:
        return None
    text = _normalize(_line_text(line))
    if not text:
        return None
    folio = _folio_value(text)
    if folio:
        return "page_number" if (folio[0], folio[1] - page.number) in profile["folios"] else None
    if _running_keys(text, page.number) & profile["running"] or text in profile["headings"]:
        return "header" if band == "top" else "footer"
    return None


def _join_lines(lines: list[str]) -> str:
    text = ""
    for line in lines:
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}".strip()
    return text


def _inner(rect: Any) -> Any:
    """Shrink a text box vertically so a redaction cannot touch neighbouring lines."""
    fitz = _fitz()
    return fitz.Rect(rect.x0 + 0.5, rect.y0 + rect.height * 0.25, rect.x1 - 0.5, rect.y1 - rect.height * 0.25)


def _pipe_table(rows: list[list[Any]], header: bool) -> str:
    """Pipe table; without a recognisable header row the header is left empty,
    as publication does, so Pandoc renders a headerless table."""

    def cell(value: Any) -> str:
        return " ".join(str(value or "").split()).replace("|", "\\|")

    width = max(len(row) for row in rows)
    grid = [[cell(row[index]) if index < len(row) else "" for index in range(width)] for row in rows]
    head, body = (grid[0], grid[1:]) if header else ([""] * width, grid)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _has_header(rows: list[list[Any]], first_row_spans: list[dict[str, Any]]) -> bool:
    """A first row is a header when it is set bold, or holds labels over numeric data."""
    first = [str(value or "") for value in rows[0]]
    if not any(value.strip() for value in first):
        return False
    if first_row_spans and all(_is_bold(span) for span in first_row_spans):
        return True

    def numeric(values: list[Any]) -> bool:
        return any(re.search(r"\d", str(value or "")) for value in values)

    return not numeric(first) and any(numeric(row) for row in rows[1:])


def _chars(text: str) -> Counter[str]:
    return Counter(char for char in text if not char.isspace())


def _char_diff(expected: Counter[str], actual: Counter[str]) -> str:
    missing = "".join(sorted((expected - actual).elements()))
    unexpected = "".join(sorted((actual - expected).elements()))
    return f"missing {missing[:40]!r}, unexpected {unexpected[:40]!r}"


def _log_removed(record: dict[str, Any], page_number: int, kind: str, text: str, bbox: Any) -> None:
    box = [round(float(value), 1) for value in bbox]
    record["removed"].append({"page": page_number, "kind": kind, "text": text, "bbox": box})


def _center(bbox: Any) -> Any:
    fitz = _fitz()
    return fitz.Point((float(bbox[0]) + float(bbox[2])) / 2, (float(bbox[1]) + float(bbox[3])) / 2)


def _token(kind: str, number: int) -> str:
    return f"BTP{kind}{number:04d}Z"


def _page_layout(page: Any, text_blocks: list[dict[str, Any]], profile: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Find furniture, tables, figures, and linked footnotes on one page (read-only)."""
    fitz = _fitz()
    body = profile["body"]
    page_number = page.number + 1
    lines = [(block, line) for block in text_blocks for line in block["lines"] if _line_text(line)]
    layout: dict[str, Any] = {"redact": [], "image_redact": [], "tables": [], "figures": [], "tokens": []}
    removed: set[int] = set()

    def remove(line: dict[str, Any], kind: str) -> None:
        layout["redact"].append(_inner(fitz.Rect(line["bbox"])))
        removed.add(id(line))
        _log_removed(record, page_number, kind, _line_text(line), line["bbox"])

    for block, line in lines:
        kind = _furniture_kind(line, block, page, profile)
        if kind:
            remove(line, kind)

    try:
        found = page.find_tables().tables
    except Exception as exc:  # PyMuPDF raises assorted errors on malformed pages
        found = []
        record["warnings"].append(f"Table detection failed on page {page_number}: {exc}")
    table_rects = []
    for table in found:
        rows = table.extract()
        if len(rows) < 2 or max(len(row) for row in rows) < 2:
            continue
        rect = fitz.Rect(table.bbox)
        inside = [line for _block, line in lines if id(line) not in removed and rect.contains(_center(line["bbox"]))]
        page_chars = _chars(" ".join(_line_text(line) for line in inside))
        cell_chars = _chars(" ".join(str(value or "") for row in rows for value in row))
        if page_chars != cell_chars:
            # The cells would replace the page text, so they must hold exactly that text.
            record["warnings"].append(
                f"Table on page {page_number} left as text: its cells do not match the page text "
                f"({_char_diff(page_chars, cell_chars)})"
            )
            continue
        row_box = fitz.Rect(table.rows[0].bbox)
        first_row_spans = [
            span for line in inside for span in line["spans"]
            if span.get("text", "").strip() and row_box.contains(_center(span["bbox"]))
        ]
        table_rects.append(rect)
        number = len(record["tables"]) + 1
        record["tables"][f"{number:04d}"] = {"page": page_number, "markdown": _pipe_table(rows, _has_header(rows, first_row_spans))}
        for line in inside:
            remove(line, "table")
        layout["tokens"].append((fitz.Point(rect.x0, rect.y0 + 9), _token("TB", number), 8))

    image_rects = []
    for image in page.get_images(full=True):
        try:
            image_rects.extend(page.get_image_rects(image[0]))
        except (AttributeError, ValueError):
            continue
    page_area = abs(page.rect.width * page.rect.height)
    scanned = any(rect.get_area() >= page_area * SCANNED_IMAGE_COVERAGE for rect in image_rects)
    claimed: list[Any] = list(table_rects)
    for _block, line in lines if not scanned else []:
        if id(line) in removed or not FIGURE_CAPTION_RE.match(_line_text(line)) or _line_size(line) >= body * HEADING_SIZE_RATIO:
            continue
        inferred = _figure_crop_box(page, list(line["bbox"]), avoid=claimed)
        if inferred is None:
            continue
        crop = inferred[0]
        if any(not (crop & other).is_empty and (crop & other).get_area() > 0 for other in claimed):
            continue
        inside = [
            item for item in lines
            if item[1] is not line and crop.contains(_center(item[1]["bbox"]))
        ]
        if sum(len(_line_text(item[1])) for item in inside) > FIGURE_MAX_LABEL_CHARS:
            continue
        drawings = [drawing["rect"] for drawing in page.get_drawings() if drawing.get("rect") is not None and crop.intersects(drawing["rect"])]
        if not inside and not drawings:
            continue  # a plain embedded image: both extractors keep it natively
        claimed.append(crop)
        layout["figures"].append(crop)
        layout["image_redact"].extend(rect for rect in image_rects if crop.contains(rect) or (crop & rect).get_area() > 0.5 * rect.get_area())
        for _block, label in inside:
            remove(label, "figure_label")
        record["figures"].append({"page": page_number, "caption": _line_text(line), "crop_bbox": [round(float(value), 2) for value in crop]})

    notes: list[dict[str, Any]] = []
    for block in sorted(text_blocks, key=lambda item: item["bbox"][1]):
        block_lines = [line for line in block["lines"] if _line_text(line) and id(line) not in removed]
        if not block_lines or block["bbox"][1] < page.rect.y0 + page.rect.height * FOOTNOTE_TOP_FRACTION:
            continue
        if not all(_line_size(line) <= body * FOOTNOTE_SIZE_RATIO for line in block_lines):
            continue
        text = _join_lines([_line_text(line) for line in block_lines])
        start = _FOOTNOTE_START_RE.match(text)
        if start:
            notes.append({"marker": start.group(1), "text": text[start.end():], "lines": block_lines, "bottom": block["bbox"][3]})
        elif notes and block["bbox"][1] - notes[-1]["bottom"] < body * 1.5:
            notes[-1]["text"] += " " + text
            notes[-1]["lines"].extend(block_lines)
            notes[-1]["bottom"] = block["bbox"][3]
    note_lines = {id(line) for note in notes for line in note["lines"]}
    references: list[tuple[str, Any]] = []
    for _block, line in lines:
        if id(line) in removed or id(line) in note_lines:
            continue
        for span in line["spans"]:
            marker = span.get("text", "").strip()
            if _MARKER_RE.match(marker) and float(span.get("size", 0)) <= body * REFERENCE_SIZE_RATIO:
                references.append((marker, fitz.Rect(span["bbox"])))
    for note in notes:
        reference = next((item for item in references if item[0] == note["marker"]), None)
        if reference is None:
            record["stats"]["footnotes_unlinked"] += 1
            continue
        references.remove(reference)
        number = len(record["footnotes"]) + 1
        record["footnotes"][f"{number:04d}"] = {"page": page_number, "marker": note["marker"], "text": note["text"]}
        layout["redact"].append(_inner(reference[1]))
        _log_removed(record, page_number, "footnote_marker", reference[0], reference[1])
        for line in note["lines"]:
            remove(line, "footnote")
        layout["tokens"].append((fitz.Point(reference[1].x0, reference[1].y1 - 1), _token("FN", number), 1))

    flow = [
        [line for line in block["lines"] if _line_text(line) and id(line) not in removed]
        for block in _reading_order(text_blocks, detect_layout(text_blocks, page.rect.width), page.rect.width)
    ]
    flow = [block for block in flow if block]
    # Whole first/last paragraphs: a last line alone ("ho-") is too short to find reliably.
    layout["head"] = _join_lines([_line_text(line) for line in flow[0]]) if flow else None
    layout["tail"] = _join_lines([_line_text(line) for line in flow[-1]]) if flow and not _is_heading_block(flow[-1], body) else None
    return layout


def _continues(tail: str, head: str) -> bool:
    """Does the page ending with ``tail`` run on into the page starting with ``head``?"""
    if _SENTENCE_END_RE.search(tail) or FIGURE_CAPTION_RE.match(tail) or TABLE_CAPTION_RE.match(tail):
        return False
    if _CJK_RE.match(tail[-1:]):
        return bool(_CJK_RE.match(head[:1]))
    return head[:1].islower()


def clean_pdf(source: Path, destination: Path) -> dict[str, Any]:
    """Write the layout-cleaned copy of ``source`` and return its layout record."""
    fitz = _fitz()
    record: dict[str, Any] = {
        "schema_version": 1,
        "tables": {},
        "footnotes": {},
        "figures": [],
        "page_breaks": [],
        # Every line or marker taken out of the cleaned copy, with its text and box.
        "removed": [],
        # Normalized heading line -> level, for promoting Calibre's paragraphs.
        "headings": {},
        "warnings": [],
        "stats": {"footnotes_unlinked": 0},
    }
    text_none = getattr(fitz, "PDF_REDACT_TEXT_NONE", 1)
    unbalanced: list[tuple[int, str]] = []
    with fitz.open(source) as document:
        parsed = _parse(document)
        has_text = _has_text_layer(parsed)
        profile = _document_profile(parsed) if has_text else {}
        record["headings"] = profile.get("heading_map", {})
        previous_tail: str | None = None
        for page, blocks in parsed if has_text else []:
            original_chars = _chars(page.get_text())
            first_removed = len(record["removed"])
            layout = _page_layout(page, blocks, profile, record)
            if previous_tail and layout["head"] and _continues(previous_tail, layout["head"]):
                record["page_breaks"].append({
                    "page": page.number + 1,
                    "tail": _normalize(previous_tail)[-PAGE_BREAK_PROBE_CHARS:],
                    "head": _normalize(layout["head"])[:PAGE_BREAK_PROBE_CHARS],
                })
            previous_tail = layout["tail"]
            scale = FIGURE_DPI / 72
            renders = [(crop, page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=crop, alpha=False)) for crop in layout["figures"]]
            if layout["image_redact"]:
                for rect in layout["image_redact"]:
                    page.add_redact_annot(rect)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=text_none)
            if layout["redact"]:
                for rect in layout["redact"]:
                    page.add_redact_annot(rect)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            for point, token, size in layout["tokens"]:
                page.insert_text(point, token, fontsize=size)
            for crop, pixmap in renders:
                page.insert_image(crop, pixmap=pixmap)
            # Conservation: what is left plus what was logged must be exactly the page text.
            left = _chars(page.get_text())
            for _point, token, _size in layout["tokens"]:
                left.subtract(_chars(token))
            logged = _chars("".join(item["text"] for item in record["removed"][first_removed:]))
            if +(left + logged) != original_chars:
                unbalanced.append((page.number, _char_diff(original_chars, +(left + logged))))
        if unbalanced:
            with fitz.open(source) as original:
                for index, detail in unbalanced:
                    document.insert_pdf(original, from_page=index, to_page=index, start_at=index)
                    document.delete_page(index + 1)
                    _forget_page(record, index + 1)
                    record["warnings"].append(
                        f"Layout cleaning was undone on page {index + 1}: the text removed from it "
                        f"does not match the removal log ({detail})"
                    )
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(destination, garbage=3, deflate=True)
    record["stats"].update({
        "furniture_lines_removed": sum(item["kind"] in {"header", "footer", "page_number"} for item in record["removed"]),
        "tables": len(record["tables"]),
        "footnotes_linked": len(record["footnotes"]),
        "figures_rendered": len(record["figures"]),
        "pages_restored_unchanged": len(unbalanced),
    })
    return record


def _forget_page(record: dict[str, Any], page_number: int) -> None:
    """Drop the layout entries of a page whose original was put back."""
    for key in ("tables", "footnotes"):
        record[key] = {number: item for number, item in record[key].items() if item["page"] != page_number}
    for key in ("figures", "removed"):
        record[key] = [item for item in record[key] if item["page"] != page_number]


def _block_starts(block: str, probe: str) -> bool:
    return bool(probe) and _normalize(block).startswith(probe)


def _block_ends(block: str, probe: str) -> bool:
    return bool(probe) and _normalize(block).endswith(probe)


def _movable(block: str) -> bool:
    """Blocks a page-break join may step over: figures, tables, and their captions."""
    plain = _normalize(block)
    return bool(IMAGE_RE.search(block)) or block.lstrip().startswith("|") or bool(
        FIGURE_CAPTION_RE.match(plain) or TABLE_CAPTION_RE.match(plain)
    )


def _join_blocks(first: str, second: str) -> str:
    first, second = first.rstrip(), second.strip()
    plain_first, plain_second = _normalize(first), _normalize(second)
    if re.search(r"[^\W\d_]-$", first) and plain_second[:1].islower():
        return first[:-1] + second
    if _CJK_RE.match(plain_first[-1:]) and _CJK_RE.match(plain_second[:1]):
        return first + second
    return f"{first} {second}"


def restore_layout_markup(markdown: str, record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Replace layout tokens with tables and footnotes and rejoin page-split paragraphs."""
    stats = {"tables_restored": 0, "footnotes_restored": 0, "page_breaks_joined": 0, "missing_tokens": []}
    seen: set[tuple[str, str]] = set()

    def footnote_ref(match: re.Match[str]) -> str:
        kind, number = match.group(2), match.group(3)
        seen.add((kind, number))
        return f"[^fn{int(number)}]" if kind == "FN" else match.group(0)

    blocks: list[str] = []
    for block in re.sub(r"[ \t]*" + _TOKEN_RE.pattern, footnote_ref, markdown).split("\n\n"):
        if re.fullmatch(r"\s*(?:\[\^fn\d+\]\s*)+", block) and blocks:
            blocks[-1] = blocks[-1].rstrip() + block.strip()  # PyMuPDF puts a raised marker on its own line
            continue
        pieces = _TOKEN_RE.split(block)
        if len(pieces) == 1:
            blocks.append(block)
            continue
        # pieces: text, (span, kind, number, text)*
        blocks.append(pieces[0].strip())
        for index in range(1, len(pieces), 4):
            kind, number, after = pieces[index + 1], pieces[index + 2], pieces[index + 3]
            table = record["tables"].get(number) if kind == "TB" else None
            if table:
                seen.add((kind, number))
                blocks.append(table["markdown"])
                stats["tables_restored"] += 1
            else:
                stats["missing_tokens"].append(f"unknown token {kind}{number}")
            blocks.append(after.strip())
    blocks = [block for block in blocks if block.strip()]

    for page_break in record["page_breaks"]:
        for index, block in enumerate(blocks):
            if not _block_ends(block, page_break["tail"]):
                continue
            moved: list[int] = []
            for following in range(index + 1, min(len(blocks), index + 1 + PAGE_BREAK_SEARCH_BLOCKS)):
                if _block_starts(blocks[following], page_break["head"]):
                    joined = _join_blocks(block, blocks[following])
                    carried = [blocks[item] for item in moved]
                    blocks[index:following + 1] = [joined, *carried]
                    stats["page_breaks_joined"] += 1
                    break
                if _EMPTY_BLOCK_RE.match(blocks[following]):
                    if "<a id=" in blocks[following]:
                        moved.append(following)  # a link target is carried, never dropped
                    continue  # other layout residue between the halves is dropped
                if _movable(blocks[following]):
                    moved.append(following)
                    continue
                break
            break

    for number, note in record["footnotes"].items():
        label = f"[^fn{int(number)}]"
        holder = next((index for index, block in enumerate(blocks) if label in block), None)
        if holder is None:
            stats["missing_tokens"].append(f"footnote {int(number)} (page {note['page']})")
            blocks.append(f"{note['marker']} {note['text']}")
            continue
        blocks.insert(holder + 1, f"{label}: {note['text']}")
        stats["footnotes_restored"] += 1
    for number, table in record["tables"].items():
        if ("TB", number) not in seen:
            stats["missing_tokens"].append(f"table {int(number)} (page {table['page']})")
            blocks.append(table["markdown"])
    return "\n\n".join(blocks).rstrip() + "\n", stats


# Markup in the working draft that is not source text: tags, link targets, footnote labels.
_DRAFT_MARKUP_RE = re.compile(r"<[^>]+>|\]\([^)]*\)|\[\^fn\d+\]:?")
# Removed text that does not come back into the draft (tables and notes do).
_DROPPED_KINDS = {"header", "footer", "page_number", "figure_label", "footnote_marker"}
_WORD_RE = re.compile(r"[^\W_]+")


def _alnum(text: str) -> Counter[str]:
    return Counter(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def missing_source_text(source: Path, markdown: str, record: dict[str, Any]) -> dict[str, Any]:
    """Letters and digits of the PDF text layer that are neither in the draft nor in the removal log.

    clean_pdf proves its own removals balance; this checks the whole pipeline
    (cleaning, Calibre or PyMuPDF, Pandoc, token restoration) end to end.
    Comparing characters rather than words keeps hyphenation, line breaks,
    and CJK spacing from counting as losses.
    """
    fitz = _fitz()
    with fitz.open(source) as document:
        # The default text flags (as clean_pdf and the extractors use), plus dehyphenation.
        source_text = "\n".join(page.get_text(flags=fitz.TEXTFLAGS_TEXT | fitz.TEXT_DEHYPHENATE) for page in document)
    # Extra text in the draft would mask losses, so only text that is really
    # gone is subtracted: dropped furniture and labels, and each note's own
    # leading marker (its definition line starts with a label instead).
    dropped = [item["text"] for item in record.get("removed", []) if item["kind"] in _DROPPED_KINDS]
    dropped += [note["marker"] for note in record.get("footnotes", {}).values()]
    removed_text = " ".join(item["text"] for item in record.get("removed", []))
    draft = _DRAFT_MARKUP_RE.sub(" ", markdown)
    missing = _alnum(source_text) - _alnum(" ".join(dropped)) - _alnum(draft)
    if not missing:
        return {"missing_characters": 0, "words_not_found": []}
    # Words that occur nowhere in the draft, even inside a longer word (so the
    # halves of a rejoined "ho-urs" are not listed), hint at what was lost.
    known = unicodedata.normalize("NFKC", f"{draft} {removed_text}").casefold()
    known_words = set(_WORD_RE.findall(known))
    candidates = set(_WORD_RE.findall(unicodedata.normalize("NFKC", source_text).casefold())) - known_words
    absent = sorted(word for word in candidates if word not in known)
    return {"missing_characters": sum(missing.values()), "words_not_found": absent[:20]}


def extract_markdown(pdf_path: Path, project: Path, image_dir: Path) -> tuple[str, dict[str, Any]]:
    """Extract a PDF's text layer to Markdown with headings, reading order, and images."""
    fitz = _fitz()
    parts: list[str] = []
    stats = {"pages": 0, "headings": 0, "images": 0, "margin_lines_removed": 0}
    with fitz.open(pdf_path) as document:
        profile = _document_profile(_parse(document))
        body, levels = profile["body"], profile["levels"]
        for page_number, page in enumerate(document, 1):
            stats["pages"] += 1
            blocks = [block for block in page.get_text("dict")["blocks"] if block.get("type") in {0, 1}]
            layout = detect_layout([block for block in blocks if block.get("type") == 0], page.rect.width)
            previous_heading = False
            for block in _reading_order(blocks, layout, page.rect.width):
                if block.get("type") == 1:
                    if min(block.get("width", 0), block.get("height", 0)) < MIN_IMAGE_PIXELS:
                        continue
                    stats["images"] += 1
                    path = image_dir / f"page-{page_number:04d}-{stats['images']:03d}.{block.get('ext', 'png')}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(block["image"])
                    parts.append(f"![]({portable_path(path, project)})")
                    previous_heading = False
                    continue
                lines = []
                for line in block["lines"]:
                    if _furniture_kind(line, block, page, profile):
                        stats["margin_lines_removed"] += 1
                    elif _line_text(line):
                        lines.append(line)
                if not lines:
                    continue
                if _is_heading_block(lines, body):
                    level = min(levels[round(_line_size(line) * 2) / 2] for line in lines)
                    title = _join_lines([_line_text(line) for line in lines])
                    if previous_heading and _LABEL_RE.match(parts[-1].split(" ", 1)[1]):
                        # "Chapter 3" followed by its title is one heading.
                        old_level, old_title = parts.pop().split(" ", 1)
                        level = min(level, len(old_level))
                        title = f"{old_title} {title}"
                    else:
                        stats["headings"] += 1
                    parts.append("#" * level + " " + title)
                    previous_heading = True
                else:
                    parts.append(_join_lines([_line_text(line) for line in lines]))
                    previous_heading = False
    return "\n\n".join(parts) + "\n", stats

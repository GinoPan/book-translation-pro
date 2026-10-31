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
from collections import Counter
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
RUNNING_HEAD_MIN_PAGES = 2
# A margin-band line set smaller than this (relative to body text) at the top
# of a page is a running head even when it appears only once.
RUNNING_HEAD_SIZE_RATIO = 0.95
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
_FOLIO_RE = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,8})$", re.IGNORECASE)
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


def _has_text_layer(document: Any) -> bool:
    """Scanned PDFs have none; they are left to the OCR preflight."""
    return any(_text_blocks(page) for page in document)


def _body_size(document: Any) -> float:
    """Most common font size, weighted by character count."""
    sizes: Counter[float] = Counter()
    for page in document:
        for block in _text_blocks(page):
            for line in block["lines"]:
                for span in line["spans"]:
                    sizes[round(float(span.get("size", 0)) * 2) / 2] += len(span.get("text", "").strip())
    if not sizes:
        raise BTPError("The PDF has no extractable text layer")
    return sizes.most_common(1)[0][0]


def _heading_levels(document: Any, body: float) -> dict[float, int]:
    sizes = sorted({
        round(_line_size(line) * 2) / 2
        for page in document for block in _text_blocks(page) for line in block["lines"]
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
        size >= body * RUNNING_HEAD_SIZE_RATIO
        and len(text) <= BOLD_HEADING_MAX_CHARS
        and not _HEADING_END_RE.search(text)
        and all(_is_bold(span) for span in spans)
    )


def _is_heading_block(lines: list[dict[str, Any]], body: float) -> bool:
    """Large-font blocks are headings; a bold body-size block only when it is short."""
    if not lines or not all(_is_heading_line(line, body) for line in lines):
        return False
    return len(lines) <= BOLD_HEADING_MAX_LINES or any(_line_size(line) >= body * HEADING_SIZE_RATIO for line in lines)


def heading_lines(pdf_path: Path) -> dict[str, int]:
    """Map each heading line of the PDF (normalized) to a heading level."""
    fitz = _fitz()
    with fitz.open(pdf_path) as document:
        if not _has_text_layer(document):
            return {}
        body = _body_size(document)
        levels = _heading_levels(document, body)
        result: dict[str, int] = {}
        for page in document:
            for block in _text_blocks(page):
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


def _running_heads(document: Any, body: float) -> set[str]:
    """Margin-band lines that repeat (ignoring digits) on several pages."""
    seen: Counter[str] = Counter()
    for page in document:
        keys = set()
        for block in _text_blocks(page):
            for line in block["lines"]:
                if _band(page, line) and _line_size(line) < body * HEADING_SIZE_RATIO:
                    keys.add(re.sub(r"\d+", "#", _normalize(_line_text(line))))
        seen.update(keys)
    return {key for key, count in seen.items() if key and count >= RUNNING_HEAD_MIN_PAGES}


def _document_profile(document: Any) -> dict[str, Any]:
    body = _body_size(document)
    return {
        "body": body,
        "levels": _heading_levels(document, body),
        "running": _running_heads(document, body),
        # Chapter-title running heads repeat a heading of the book.
        "headings": {
            _normalize(_line_text(line))
            for page in document for block in _text_blocks(page) for line in block["lines"]
            if _line_size(line) >= body * HEADING_SIZE_RATIO
        },
    }


def _furniture_kind(line: dict[str, Any], page: Any, profile: dict[str, Any]) -> str | None:
    """Classify a margin-band running head ("header"/"footer") or folio ("page_number")."""
    band = _band(page, line)
    size = _line_size(line)
    if band is None or size >= profile["body"] * HEADING_SIZE_RATIO:
        return None
    text = _normalize(_line_text(line))
    if not text:
        return None
    if _FOLIO_RE.match(text):
        return "page_number"
    if (
        re.sub(r"\d+", "#", text) in profile["running"]
        or text in profile["headings"]
        or (band == "top" and size < profile["body"] * RUNNING_HEAD_SIZE_RATIO)
    ):
        return "header" if band == "top" else "footer"
    return None


def page_furniture(pdf_path: Path) -> list[dict[tuple[float, ...], str]]:
    """Per page, the rounded line boxes of running heads and folios and their kind."""
    fitz = _fitz()
    with fitz.open(pdf_path) as document:
        if not _has_text_layer(document):
            return [{} for _page in document]
        profile = _document_profile(document)
        return [
            {
                tuple(round(float(value), 1) for value in line["bbox"]): kind
                for block in _text_blocks(page) for line in block["lines"]
                if (kind := _furniture_kind(line, page, profile))
            }
            for page in document
        ]


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


def _pipe_table(rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        return " ".join(str(value or "").split()).replace("|", "\\|")

    width = max(len(row) for row in rows)
    grid = [[cell(row[index]) if index < len(row) else "" for index in range(width)] for row in rows]
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * width]
    lines.extend("| " + " | ".join(row) + " |" for row in grid[1:])
    return "\n".join(lines)


def _token(kind: str, number: int) -> str:
    return f"BTP{kind}{number:04d}Z"


def _page_layout(page: Any, profile: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Find furniture, tables, figures, and linked footnotes on one page (read-only)."""
    fitz = _fitz()
    body = profile["body"]
    page_number = page.number + 1
    text_blocks = _text_blocks(page)
    lines = [(block, line) for block in text_blocks for line in block["lines"] if _line_text(line)]
    layout: dict[str, Any] = {"redact": [], "image_redact": [], "tables": [], "figures": [], "tokens": []}
    removed: set[int] = set()

    for block, line in lines:
        kind = _furniture_kind(line, page, profile)
        if kind:
            layout["redact"].append(_inner(fitz.Rect(line["bbox"])))
            removed.add(id(line))
            record["stats"]["furniture_lines_removed"] += 1

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
        table_rects.append(rect)
        number = len(record["tables"]) + 1
        record["tables"][f"{number:04d}"] = {"page": page_number, "markdown": _pipe_table(rows)}
        layout["redact"].append(rect + (1, 1, -1, -1))
        layout["tokens"].append((fitz.Point(rect.x0, rect.y0 + 9), _token("TB", number), 8))
        for _block, line in lines:
            if rect.contains(fitz.Point(line["bbox"][0], _line_center_y(line))):
                removed.add(id(line))

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
            if item[1] is not line and crop.contains(fitz.Point((item[1]["bbox"][0] + item[1]["bbox"][2]) / 2, _line_center_y(item[1])))
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
            layout["redact"].append(_inner(fitz.Rect(label["bbox"])))
            removed.add(id(label))
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
        layout["redact"].extend(_inner(fitz.Rect(line["bbox"])) for line in note["lines"])
        layout["tokens"].append((fitz.Point(reference[1].x0, reference[1].y1 - 1), _token("FN", number), 1))
        removed.update(id(line) for line in note["lines"])

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
        "warnings": [],
        "stats": {"furniture_lines_removed": 0, "footnotes_unlinked": 0},
    }
    text_none = getattr(fitz, "PDF_REDACT_TEXT_NONE", 1)
    with fitz.open(source) as document:
        has_text = _has_text_layer(document)
        profile = _document_profile(document) if has_text else {}
        previous_tail: str | None = None
        for page in document if has_text else []:
            layout = _page_layout(page, profile, record)
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(destination, garbage=3, deflate=True)
    record["stats"].update({
        "tables": len(record["tables"]),
        "footnotes_linked": len(record["footnotes"]),
        "figures_rendered": len(record["figures"]),
    })
    return record


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
    first, second = first.rstrip(), second.lstrip()
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
                    continue  # layout residue between the halves is dropped
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


def extract_markdown(pdf_path: Path, project: Path, image_dir: Path) -> tuple[str, dict[str, Any]]:
    """Extract a PDF's text layer to Markdown with headings, reading order, and images."""
    fitz = _fitz()
    parts: list[str] = []
    stats = {"pages": 0, "headings": 0, "images": 0, "margin_lines_removed": 0}
    with fitz.open(pdf_path) as document:
        profile = _document_profile(document)
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
                    if _furniture_kind(line, page, profile):
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

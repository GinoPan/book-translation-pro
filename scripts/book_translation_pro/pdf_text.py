"""PyMuPDF text analysis: heading recovery for converted PDFs and a fallback extractor."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import BTPError, portable_path
from .visual import _block_text, _fitz, _reading_order, detect_layout


# A line is a heading candidate when its font is clearly larger than body text.
HEADING_SIZE_RATIO = 1.15
MAX_HEADING_CHARS = 120
MAX_HEADING_LEVELS = 3
# Running heads and folios live in these top/bottom page bands.
MARGIN_FRACTION = 0.08
RUNNING_HEAD_MIN_PAGES = 3
MIN_IMAGE_PIXELS = 32
_FOLIO_RE = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,8})$", re.IGNORECASE)
_MARKUP_RE = re.compile(r"<[^>]+>|[*_`]")
# A bare label such as "Chapter 3" or "Part II" is joined with the title that follows it.
_LABEL_RE = re.compile(
    r"^(?:(?:chapter|part|book|section|appendix|lecture|lesson)\s+[\w.]+|第\s*[\d一二三四五六七八九十百]+\s*[章节部篇]|[\divxlc.]+)$",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return " ".join(_MARKUP_RE.sub("", text).split()).casefold()


def _line_text(line: dict[str, Any]) -> str:
    return " ".join(span.get("text", "").strip() for span in line.get("spans", []) if span.get("text", "").strip())


def _line_size(line: dict[str, Any]) -> float:
    return max((float(span.get("size", 0)) for span in line.get("spans", []) if span.get("text", "").strip()), default=0.0)


def _text_blocks(page: Any) -> list[dict[str, Any]]:
    return [block for block in page.get_text("dict")["blocks"] if block.get("type") == 0 and _block_text(block)]


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
    return (
        _line_size(line) >= body * HEADING_SIZE_RATIO
        and len(text) <= MAX_HEADING_CHARS
        and bool(re.search(r"[^\W\d_]", text))
    )


def heading_lines(pdf_path: Path) -> dict[str, int]:
    """Map each large-font line of the PDF (normalized) to a heading level."""
    fitz = _fitz()
    with fitz.open(pdf_path) as document:
        body = _body_size(document)
        levels = _heading_levels(document, body)
        result: dict[str, int] = {}
        for page in document:
            for block in _text_blocks(page):
                for line in block["lines"]:
                    if _is_heading_line(line, body):
                        result.setdefault(_normalize(_line_text(line)), levels[round(_line_size(line) * 2) / 2])
    return result


def promote_headings(markdown: str, headings: dict[str, int]) -> tuple[str, int]:
    """Turn single-line paragraphs that are large-font PDF lines into Markdown headings.

    Calibre's PDF reflow emits headings as styled paragraphs, which leaves the
    Book Map without chapters; the source PDF's font sizes restore them.
    """
    blocks = markdown.split("\n\n")
    promoted = 0
    in_fence = False
    for index, block in enumerate(blocks):
        fences = sum(1 for line in block.split("\n") if line.lstrip().startswith(("```", "~~~")))
        stripped = block.strip()
        if not in_fence and "\n" not in stripped and stripped and not stripped.startswith(("#", "|", "!", "<img")):
            level = headings.get(_normalize(stripped))
            if level:
                blocks[index] = "#" * level + " " + " ".join(_MARKUP_RE.sub("", stripped).split())
                promoted += 1
        if fences % 2:
            in_fence = not in_fence
    return "\n\n".join(blocks), promoted


def _running_heads(document: Any) -> set[str]:
    """Margin-band lines that repeat (ignoring digits) across pages, plus bare folios."""
    seen: Counter[str] = Counter()
    for page in document:
        band = page.rect.height * MARGIN_FRACTION
        keys = set()
        for block in _text_blocks(page):
            if block["bbox"][1] > band and block["bbox"][3] < page.rect.height - band:
                continue
            for line in block["lines"]:
                keys.add(re.sub(r"\d+", "#", _normalize(_line_text(line))))
        seen.update(keys)
    threshold = max(RUNNING_HEAD_MIN_PAGES, len(document) // 3)
    return {key for key, count in seen.items() if count >= threshold}


def _is_margin_noise(line: dict[str, Any], block: dict[str, Any], page: Any, running: set[str]) -> bool:
    band = page.rect.height * MARGIN_FRACTION
    if band < block["bbox"][1] and block["bbox"][3] < page.rect.height - band:
        return False
    text = _normalize(_line_text(line))
    return bool(_FOLIO_RE.match(text)) or re.sub(r"\d+", "#", text) in running


def _join_lines(lines: list[str]) -> str:
    text = ""
    for line in lines:
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}".strip()
    return text


def extract_markdown(pdf_path: Path, project: Path, image_dir: Path) -> tuple[str, dict[str, Any]]:
    """Extract a PDF's text layer to Markdown with headings, reading order, and images."""
    fitz = _fitz()
    parts: list[str] = []
    stats = {"pages": 0, "headings": 0, "images": 0, "margin_lines_removed": 0}
    with fitz.open(pdf_path) as document:
        body = _body_size(document)
        levels = _heading_levels(document, body)
        running = _running_heads(document)
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
                    if _is_margin_noise(line, block, page, running):
                        stats["margin_lines_removed"] += 1
                    elif _line_text(line):
                        lines.append(line)
                if not lines:
                    continue
                if all(_is_heading_line(line, body) for line in lines):
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

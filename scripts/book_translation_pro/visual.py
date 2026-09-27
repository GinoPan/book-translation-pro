"""PDF page evidence, layout risk, OCR, crops, contact sheets, and Visual QA."""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .artifacts import (
    BTPError,
    atomic_write_json,
    atomic_write_text,
    hash_data,
    load_json,
    portable_path,
    safe_project_path,
    sha256_file,
    validate_schema,
)
from .runtime import find_binary


TOKEN_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)
FORMULA_RE = re.compile(r"(?:[=∑∫√±≤≥]|\b(?:sin|cos|tan|log|exp)\s*\()")
CAPTION_RE = re.compile(r"^(?:fig(?:ure)?|table|图|表)\s*[\dA-Z一二三四五六七八九十.-]+", re.IGNORECASE)
OCR_LANGUAGE_MAP = {
    "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por",
    "ja": "jpn", "ko": "kor", "zh": "chi_sim", "zh-hans": "chi_sim", "zh-hant": "chi_tra",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fitz() -> Any:
    try:
        import fitz
    except ImportError as exc:
        raise BTPError("PyMuPDF is required for V1.0 PDF visual processing") from exc
    return fitz


def _norm_box(rect: Any, page_rect: Any) -> dict[str, float]:
    x0 = max(0.0, min(float(rect.x0), float(page_rect.width)))
    y0 = max(0.0, min(float(rect.y0), float(page_rect.height)))
    x1 = max(x0, min(float(rect.x1), float(page_rect.width)))
    y1 = max(y0, min(float(rect.y1), float(page_rect.height)))
    return {
        "x": round(x0 / page_rect.width, 6),
        "y": round(y0 / page_rect.height, 6),
        "width": round(max(0.000001, (x1 - x0) / page_rect.width), 6),
        "height": round(max(0.000001, (y1 - y0) / page_rect.height), 6),
    }


def _block_text(block: dict[str, Any]) -> str:
    return " ".join(
        span.get("text", "").strip()
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ).strip()


def _font_sizes(block: dict[str, Any]) -> list[float]:
    return [
        float(span.get("size", 0))
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]


def _median(values: list[float], default: float = 10.0) -> float:
    if not values:
        return default
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _vertical_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    overlap = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    denominator = max(1.0, min(a[3] - a[1], b[3] - b[1]))
    return overlap / denominator


def detect_layout(text_blocks: list[dict[str, Any]], page_width: float) -> str:
    """Detect two-column reading regions while ignoring full-width headings."""
    candidates = []
    for block in text_blocks:
        bbox = tuple(float(value) for value in block["bbox"])
        if len(_block_text(block)) < 20 or bbox[2] - bbox[0] > page_width * 0.7:
            continue
        candidates.append((block, bbox))
    left = [item for item in candidates if (item[1][0] + item[1][2]) / 2 < page_width * 0.46]
    right = [item for item in candidates if (item[1][0] + item[1][2]) / 2 > page_width * 0.54]
    paired = any(_vertical_overlap(a[1], b[1]) >= 0.2 for a in left for b in right)
    if not paired:
        return "single_column"
    full_width = any(float(block["bbox"][2]) - float(block["bbox"][0]) > page_width * 0.72 for block in text_blocks)
    return "mixed" if full_width else "multi_column"


def _reading_order(blocks: list[dict[str, Any]], layout: str, page_width: float) -> list[dict[str, Any]]:
    if layout not in {"multi_column", "mixed"}:
        return sorted(blocks, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    full = [item for item in blocks if item["bbox"][2] - item["bbox"][0] > page_width * 0.72]
    narrow = [item for item in blocks if item not in full]
    left = [item for item in narrow if (item["bbox"][0] + item["bbox"][2]) / 2 < page_width / 2]
    right = [item for item in narrow if item not in left]
    header = [item for item in full if item["bbox"][1] < min((n["bbox"][1] for n in narrow), default=page_width)]
    footer = [item for item in full if item not in header]
    key = lambda item: (item["bbox"][1], item["bbox"][0])
    return sorted(header, key=key) + sorted(left, key=key) + sorted(right, key=key) + sorted(footer, key=key)


def _role_and_risk(block: dict[str, Any], page_rect: Any, median_size: float, layout: str) -> tuple[str, int, list[str], str]:
    if block.get("type") == 1:
        return "figure", 1, ["embedded_image"], "preserve_asset"
    text = _block_text(block)
    sizes = _font_sizes(block)
    size = sum(sizes) / len(sizes) if sizes else median_size
    x0, y0, x1, y1 = (float(value) for value in block["bbox"])
    width = x1 - x0
    if FORMULA_RE.search(text) and len(text) < 300:
        return "formula", 3, ["formula_layout"], "needs_review"
    if CAPTION_RE.search(text):
        return "caption", 1, ["caption"], "reflow"
    if y0 > page_rect.height * 0.82 and size < median_size * 0.9:
        return "footnote", 2, ["note_dense_bottom_region"], "needs_review"
    if size >= median_size * 1.3 and len(text) <= 180:
        return "heading", 0, [], "reflow"
    if width < page_rect.width * 0.38 and layout == "single_column" and len(text) > 40:
        return "sidebar", 2, ["narrow_sidebar"], "needs_review"
    risk = 2 if layout in {"multi_column", "mixed"} else 0
    reasons = ["column_reading_order"] if risk else []
    return "body", risk, reasons, "needs_review" if risk else "reflow"


def _tokens(text: str) -> Counter[str]:
    return Counter(token.casefold() for token in TOKEN_RE.findall(text))


def _overlap_score(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = sum((left & right).values())
    return overlap / max(1, min(sum(left.values()), sum(right.values())))


def align_pages_to_units(page_texts: list[str], unit_texts: dict[str, str]) -> tuple[list[list[str]], dict[str, list[int]]]:
    unit_tokens = {unit_id: _tokens(text) for unit_id, text in unit_texts.items()}
    page_units: list[list[str]] = []
    unit_pages = {unit_id: [] for unit_id in unit_texts}
    scores_by_page: list[dict[str, float]] = []
    for text in page_texts:
        page_tokens = _tokens(text)
        scores = {unit_id: _overlap_score(page_tokens, tokens) for unit_id, tokens in unit_tokens.items()}
        scores_by_page.append(scores)
        best = max(scores.values(), default=0.0)
        selected = [unit_id for unit_id, score in scores.items() if score >= 0.12 and score >= best * 0.55]
        page_units.append(selected[:4])
    for page_index, selected in enumerate(page_units, 1):
        for unit_id in selected:
            unit_pages[unit_id].append(page_index)
    for unit_id in unit_texts:
        if unit_pages[unit_id] or not scores_by_page:
            continue
        best_index = max(range(len(scores_by_page)), key=lambda index: scores_by_page[index].get(unit_id, 0.0))
        if scores_by_page[best_index].get(unit_id, 0.0) > 0:
            page_units[best_index].append(unit_id)
            unit_pages[unit_id].append(best_index + 1)
    return page_units, unit_pages


def _tesseract_languages(executable: Path | None) -> set[str]:
    if not executable:
        return set()
    result = subprocess.run(
        [str(executable), "--list-langs"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    return {line.strip() for line in result.stdout.splitlines()[1:] if line.strip()}


def _ocr_language(source_language: str, available: set[str]) -> str | None:
    normalized = source_language.casefold()
    wanted = OCR_LANGUAGE_MAP.get(normalized, OCR_LANGUAGE_MAP.get(normalized.split("-")[0], normalized))
    if source_language == "auto":
        wanted = "eng"
    return wanted if wanted in available else None


def _configured_ocr_language(config: dict[str, Any], available: set[str]) -> str | None:
    requested = config["visual"].get("ocr_languages", [])
    if requested:
        normalized = [OCR_LANGUAGE_MAP.get(item.casefold(), item) for item in requested]
        usable = [item for item in normalized if item in available]
        return "+".join(usable) if usable else None
    return _ocr_language(config["source"].get("language", "auto"), available)


def _run_ocr(image_path: Path, executable: Path, language: str) -> tuple[str, float | None]:
    result = subprocess.run(
        [str(executable), str(image_path), "stdout", "-l", language, "tsv"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if result.returncode:
        raise BTPError((result.stderr or "Tesseract OCR failed").strip().splitlines()[-1])
    words: list[str] = []
    confidences: list[float] = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split("\t", 11)
        if len(fields) < 12:
            continue
        word = fields[11].strip()
        if not word:
            continue
        words.append(word)
        try:
            confidence = float(fields[10])
            if confidence >= 0:
                confidences.append(confidence)
        except ValueError:
            pass
    return " ".join(words), (sum(confidences) / len(confidences) if confidences else None)


def augment_markdown_with_scanned_page_ocr(
    project: Path, config: dict[str, Any], markdown_path: Path
) -> dict[str, Any]:
    """Append OCR text for image-only PDF pages before structural chunking."""
    source = safe_project_path(project, config["source"]["path"])
    if config["source"].get("format") != "pdf" and source.suffix.casefold() != ".pdf":
        return {"applicable": False, "scanned_pages": [], "recovered_pages": [], "issues": []}
    fitz = _fitz()
    executable = find_binary("tesseract")
    available = _tesseract_languages(executable)
    language = _configured_ocr_language(config, available)
    minimum_text = int(config["visual"].get("minimum_text_characters", 20))
    dpi = int(config["visual"]["render_dpi"])
    page_dir = project / "assets" / "page-images"
    ocr_dir = project / "source-work" / "ocr"
    page_dir.mkdir(parents=True, exist_ok=True)
    ocr_dir.mkdir(parents=True, exist_ok=True)
    recovered: list[tuple[int, str]] = []
    scanned_pages: list[int] = []
    issues: list[str] = []
    with fitz.open(source) as document:
        for page_index, page in enumerate(document, 1):
            blocks = page.get_text("dict").get("blocks", [])
            text = "\n".join(_block_text(block) for block in blocks if block.get("type") == 0).strip()
            image_blocks = [block for block in blocks if block.get("type") == 1]
            coverage = min(1.0, sum(
                max(0.0, (block["bbox"][2] - block["bbox"][0]) * (block["bbox"][3] - block["bbox"][1]))
                for block in image_blocks
            ) / max(1.0, page.rect.width * page.rect.height))
            if len(text) >= minimum_text or coverage < 0.5:
                continue
            scanned_pages.append(page_index)
            if config["visual"].get("ocr_mode", "auto") == "off":
                issues.append(f"page-{page_index:04d}:ocr_disabled")
                continue
            if not executable:
                issues.append(f"page-{page_index:04d}:tesseract_unavailable")
                continue
            if not language:
                issues.append(f"page-{page_index:04d}:ocr_language_unavailable")
                continue
            image_path = page_dir / f"page-{page_index:04d}.png"
            page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False).save(image_path)
            try:
                ocr_text, _ = _run_ocr(image_path, executable, language)
            except (BTPError, subprocess.SubprocessError) as exc:
                issues.append(f"page-{page_index:04d}:ocr_failed:{exc}")
                continue
            if len(ocr_text.strip()) < minimum_text:
                issues.append(f"page-{page_index:04d}:ocr_output_too_short")
                continue
            ocr_path = ocr_dir / f"page-{page_index:04d}.txt"
            atomic_write_text(ocr_path, ocr_text.strip() + "\n")
            recovered.append((page_index, ocr_text.strip()))
    if issues and config["visual"].get("ocr_mode") == "required":
        raise BTPError("Required OCR could not recover all scanned pages: " + "; ".join(issues))
    if recovered:
        original = markdown_path.read_text(encoding="utf-8").rstrip()
        appendix = ["", "# OCR Recovered Source Pages", ""]
        for page_index, text in recovered:
            appendix.extend([f"## Source Page {page_index}", "", text, ""])
        atomic_write_text(markdown_path, original + "\n" + "\n".join(appendix).rstrip() + "\n")
    return {
        "applicable": True,
        "scanned_pages": scanned_pages,
        "recovered_pages": [page for page, _ in recovered],
        "issues": issues,
        "language": language,
    }


def _crop_region(image_path: Path, box: dict[str, float], destination: Path) -> tuple[int, int, float]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as image:
        left = max(0, math.floor(box["x"] * image.width) - 4)
        top = max(0, math.floor(box["y"] * image.height) - 4)
        right = min(image.width, math.ceil((box["x"] + box["width"]) * image.width) + 4)
        bottom = min(image.height, math.ceil((box["y"] + box["height"]) * image.height) + 4)
        crop = image.crop((left, top, right, bottom)).convert("RGB")
        extrema = crop.convert("L").getextrema()
        contrast = float(extrema[1] - extrema[0]) if extrema else 0.0
        crop.save(destination, "PNG")
        return crop.width, crop.height, contrast


def _contact_sheets(project: Path, pages: list[dict[str, Any]]) -> list[str]:
    output_dir = project / "qa" / "contact-sheets"
    output_dir.mkdir(parents=True, exist_ok=True)
    refs: list[str] = []
    for batch_start in range(0, len(pages), 40):
        batch = pages[batch_start:batch_start + 40]
        cell_w, cell_h, columns = 260, 360, 4
        rows = math.ceil(len(batch) / columns)
        sheet = Image.new("RGB", (cell_w * columns, cell_h * rows), "white")
        draw = ImageDraw.Draw(sheet)
        for offset, page in enumerate(batch):
            image_path = safe_project_path(project, page["image_ref"])
            with Image.open(image_path) as image:
                thumb = image.convert("RGB")
                thumb.thumbnail((cell_w - 20, cell_h - 42))
            x = (offset % columns) * cell_w + 10
            y = (offset // columns) * cell_h + 28
            sheet.paste(thumb, (x + (cell_w - 20 - thumb.width) // 2, y))
            color = {0: "#2f855a", 1: "#2b6cb0", 2: "#c05621", 3: "#c53030"}[page["risk"]]
            draw.rectangle((x, y - 22, x + cell_w - 20, y - 2), fill=color)
            draw.text((x + 4, y - 20), f"Page {page['page_index']}  Risk {page['risk']}", fill="white")
        destination = output_dir / f"contact-sheet-{batch_start // 40 + 1:03d}.png"
        sheet.save(destination, "PNG")
        refs.append(portable_path(destination, project))
    return refs


def semantic_page(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_index": page["page_index"],
        "layout": page["layout"],
        "reading_order_confidence": page["reading_order_confidence"],
        "ocr_status": page.get("ocr_status"),
        "ocr_text_sha256": page.get("ocr_text_sha256"),
        "source_unit_ids": page.get("source_unit_ids", []),
        "regions": [
            {key: region.get(key) for key in ("id", "role", "box", "reading_order", "source_segment_ids", "risk", "risk_reasons", "disposition")}
            for region in page["regions"]
        ],
    }


def visual_dependency_hash(project: Path, page_indices: list[int]) -> str:
    path = project / "analysis" / "page-map.json"
    if not path.is_file() or not page_indices:
        return hash_data({"visual": "not_applicable"})
    page_map = load_json(path)
    wanted = set(page_indices)
    pages = [semantic_page(page) for page in page_map["pages"] if page["page_index"] in wanted]
    findings = []
    for index in sorted(wanted):
        review_path = project / "qa" / "visual-reviews" / f"page-{index:04d}.json"
        if review_path.is_file():
            review = load_json(review_path)
            findings.extend(
                finding for finding in review.get("findings", [])
                if finding.get("affects_translation") and finding.get("status") in {"resolved", "exception_approved"}
            )
    return hash_data({"pages": pages, "translation_findings": findings})


def prepare_visual(
    project: Path,
    config: dict[str, Any],
    units: list[dict[str, Any]],
    book_map: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Render and analyze a PDF, returning page-linked units and Book Map."""
    source = safe_project_path(project, config["source"]["path"])
    if config["source"].get("format") != "pdf" and source.suffix.casefold() != ".pdf":
        return units, book_map, {"applicable": False, "page_count": 0, "required_review_pages": []}
    if config["visual"].get("renderer", "auto") not in {"auto", "pymupdf"}:
        raise BTPError("V1.0 Page Map generation supports renderer=auto or renderer=pymupdf")

    fitz = _fitz()
    dpi = int(config["visual"]["render_dpi"])
    scale = dpi / 72
    page_dir = project / "assets" / "page-images"
    crop_dir = project / "assets" / "display-crops"
    ocr_dir = project / "source-work" / "ocr"
    review_dir = project / "qa" / "visual-reviews"
    for directory in (page_dir, crop_dir, ocr_dir, review_dir):
        directory.mkdir(parents=True, exist_ok=True)

    tesseract = find_binary("tesseract")
    languages = _tesseract_languages(tesseract)
    ocr_language = _configured_ocr_language(config, languages)
    page_records: list[dict[str, Any]] = []
    page_texts: list[str] = []
    raw_pages: list[tuple[Any, list[dict[str, Any]], str, list[Any]]] = []

    try:
        document = fitz.open(source)
    except Exception as exc:
        raise BTPError(f"Cannot open PDF for visual processing: {exc}") from exc
    with document:
        if len(document) == 0:
            raise BTPError("PDF has no pages")
        for page_number, page in enumerate(document, 1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image_path = page_dir / f"page-{page_number:04d}.png"
            pixmap.save(image_path)
            page_dict = page.get_text("dict")
            blocks = page_dict.get("blocks", [])
            text_blocks = [block for block in blocks if block.get("type") == 0 and _block_text(block)]
            image_blocks = [block for block in blocks if block.get("type") == 1]
            text = "\n".join(_block_text(block) for block in text_blocks)
            cached_ocr = ocr_dir / f"page-{page_number:04d}.txt"
            alignment_text = cached_ocr.read_text(encoding="utf-8") if not text.strip() and cached_ocr.is_file() else text
            page_texts.append(alignment_text)
            try:
                table_boxes = [table.bbox for table in page.find_tables().tables] if hasattr(page, "find_tables") else []
            except Exception:
                table_boxes = []
            raw_pages.append((page.rect, blocks, text, table_boxes))

    unit_texts = {
        unit["id"]: (project / "source-work" / "work-units" / f"{unit['id']}.md").read_text(encoding="utf-8")
        for unit in units
    }
    page_units, unit_pages = align_pages_to_units(page_texts, unit_texts)
    unit_segments = {unit["id"]: unit["segment_ids"] for unit in units}
    asset_records: list[dict[str, Any]] = []
    minimum_text = int(config["visual"].get("minimum_text_characters", 20))
    threshold = int(config["visual"]["minimum_review_risk"])

    for page_number, ((page_rect, blocks, text, table_boxes), selected_units) in enumerate(zip(raw_pages, page_units), 1):
        image_path = page_dir / f"page-{page_number:04d}.png"
        text_blocks = [block for block in blocks if block.get("type") == 0 and _block_text(block)]
        image_blocks = [block for block in blocks if block.get("type") == 1]
        layout = detect_layout(text_blocks, page_rect.width)
        image_coverage = min(1.0, sum(
            max(0.0, (block["bbox"][2] - block["bbox"][0]) * (block["bbox"][3] - block["bbox"][1]))
            for block in image_blocks
        ) / max(1.0, page_rect.width * page_rect.height))
        scanned = len(text.strip()) < minimum_text and image_coverage >= 0.5
        if scanned:
            layout = "fixed_layout"
        sizes = [value for block in text_blocks for value in _font_sizes(block)]
        median_size = _median(sizes)
        ordered = _reading_order(blocks, layout, page_rect.width)
        segment_ids = sorted({segment for unit_id in selected_units for segment in unit_segments.get(unit_id, [])})
        regions: list[dict[str, Any]] = []
        for order, block in enumerate(ordered):
            role, risk, reasons, disposition = _role_and_risk(block, page_rect, median_size, layout)
            if scanned and block.get("type") == 1:
                risk, reasons, disposition = 3, ["scanned_page_image"], "preserve_asset"
            region = {
                "id": f"p{page_number:04d}-r{order + 1:03d}",
                "role": role,
                "box": _norm_box(fitz.Rect(block["bbox"]), page_rect),
                "reading_order": order,
                "source_segment_ids": segment_ids,
                "risk": risk,
                "risk_reasons": reasons,
                "disposition": disposition,
            }
            regions.append(region)

        for table_number, table_box in enumerate(table_boxes, 1):
            region = {
                "id": f"p{page_number:04d}-table-{table_number:02d}",
                "role": "table",
                "box": _norm_box(fitz.Rect(table_box), page_rect),
                "reading_order": len(regions),
                "source_segment_ids": segment_ids,
                "risk": 2,
                "risk_reasons": ["detected_table"],
                "disposition": "needs_review",
            }
            regions.append(region)

        ocr_status = "not_needed"
        ocr_issues: list[str] = []
        ocr_ref: str | None = None
        ocr_digest: str | None = None
        ocr_confidence: float | None = None
        if scanned:
            ocr_status = "pending"
            if config["visual"].get("ocr_mode", "auto") == "off":
                ocr_status, ocr_issues = "unavailable", ["ocr_disabled"]
            elif not tesseract:
                ocr_status, ocr_issues = "unavailable", ["tesseract_unavailable"]
            elif not ocr_language:
                ocr_status, ocr_issues = "unavailable", ["ocr_language_unavailable"]
            else:
                try:
                    ocr_text, ocr_confidence = _run_ocr(image_path, tesseract, ocr_language)
                    if len(ocr_text.strip()) < minimum_text:
                        ocr_status, ocr_issues = "failed", ["ocr_output_too_short"]
                    else:
                        ocr_path = ocr_dir / f"page-{page_number:04d}.txt"
                        atomic_write_text(ocr_path, ocr_text.strip() + "\n")
                        ocr_ref = portable_path(ocr_path, project)
                        ocr_digest = sha256_file(ocr_path)
                        ocr_status = "completed"
                except (BTPError, subprocess.SubprocessError) as exc:
                    ocr_status, ocr_issues = "failed", [f"ocr_failed:{exc}"]

        page_risk_reasons: list[str] = []
        if layout in {"multi_column", "mixed"}:
            page_risk_reasons.append("multi_column_reading_order")
        if scanned:
            page_risk_reasons.append("scanned_page")
        if ocr_status in {"unavailable", "failed"}:
            page_risk_reasons.extend(ocr_issues)
        page_condition_risk = 3 if scanned or ocr_status in {"unavailable", "failed"} else 2 if layout in {"multi_column", "mixed"} else 0
        page_risk = max([region["risk"] for region in regions] + [page_condition_risk])
        confidence = 0.55 if layout in {"multi_column", "mixed"} else 1.0
        if scanned:
            confidence = 0.65 if ocr_status == "completed" else 0.0
        page_kind = "blank" if not text.strip() and not image_blocks else "cover" if page_number == 1 else "body"

        for region in regions:
            if region["risk"] < threshold or region["disposition"] == "preserve_asset":
                continue
            crop_path = crop_dir / f"{region['id']}.png"
            width, height, contrast = _crop_region(image_path, region["box"], crop_path)
            region["asset_ref"] = portable_path(crop_path, project)
            asset_records.append({
                "id": region["id"], "kind": "crop", "page_index": page_number,
                "path": region["asset_ref"], "sha256": sha256_file(crop_path),
                "pixel_width": width, "pixel_height": height, "contrast": contrast,
            })

        with Image.open(image_path) as rendered:
            pixel_width, pixel_height = rendered.size
        page_record: dict[str, Any] = {
            "page_index": page_number,
            "image_ref": portable_path(image_path, project),
            "image_sha256": sha256_file(image_path),
            "pixel_width": pixel_width,
            "pixel_height": pixel_height,
            "page_kind": page_kind,
            "layout": layout,
            "reading_order_confidence": confidence,
            "text_characters": len(text.strip()),
            "text_sha256": hash_data(text),
            "source_unit_ids": sorted(set(selected_units)),
            "ocr_status": ocr_status,
            "ocr_issues": ocr_issues,
            "regions": regions,
            "risk": page_risk,
            "risk_reasons": sorted(set(page_risk_reasons + [reason for region in regions for reason in region["risk_reasons"]])),
            "review_status": "pending" if page_risk >= threshold else "not_required",
        }
        if ocr_ref:
            page_record.update({"ocr_text_ref": ocr_ref, "ocr_text_sha256": ocr_digest, "ocr_confidence": ocr_confidence})
        page_records.append(page_record)
        asset_records.append({
            "id": f"page-{page_number:04d}", "kind": "page", "page_index": page_number,
            "path": page_record["image_ref"], "sha256": page_record["image_sha256"],
            "pixel_width": pixel_width, "pixel_height": pixel_height,
        })

    for page in page_records:
        existing_review_path = review_dir / f"page-{page['page_index']:04d}.json"
        if not existing_review_path.is_file():
            continue
        try:
            existing_review = load_json(existing_review_path)
            validate_schema(existing_review, "visual-review.schema.json")
        except BTPError:
            continue
        if (
            existing_review["page_hash"] == hash_data(semantic_page(page))
            and existing_review["status"] in {"approved", "exception_approved"}
        ):
            page["review_status"] = "reviewed" if existing_review["status"] == "approved" else "exception_approved"

    page_map = {
        "schema_version": 1,
        "source_fingerprint": sha256_file(source),
        "page_count": len(page_records),
        "render": {"renderer": "pymupdf", "dpi": dpi},
        "pages": page_records,
    }
    validate_schema(page_map, "page-map.schema.json")
    atomic_write_json(project / "analysis" / "page-map.json", page_map)

    for unit in units:
        unit["page_indices"] = sorted(unit_pages.get(unit["id"], []))
    node_pages: dict[str, list[int]] = {}
    for unit in units:
        node_pages.setdefault(unit["book_node_id"], []).extend(unit["page_indices"])
    for node in book_map["nodes"]:
        pages = sorted(set(node_pages.get(node["id"], [])))
        if pages:
            node["source_span"]["page_start"] = pages[0]
            node["source_span"]["page_end"] = pages[-1]
    validate_schema(book_map, "book-map.schema.json")
    atomic_write_json(project / "analysis" / "book-map.json", book_map)

    contact_refs = _contact_sheets(project, page_records)
    visual_assets = {
        "schema_version": 1,
        "source_fingerprint": page_map["source_fingerprint"],
        "contact_sheets": contact_refs,
        "assets": sorted(asset_records, key=lambda item: (item["page_index"], item["kind"], item["id"])),
    }
    atomic_write_json(project / "analysis" / "visual-assets.json", visual_assets)

    for page in page_records:
        if page["review_status"] != "pending":
            continue
        review = {
            "schema_version": 1,
            "page_index": page["page_index"],
            "page_hash": hash_data(semantic_page(page)),
            "status": "pending",
            "reviewer": "",
            "reviewed_at": "",
            "notes": "",
            "findings": [],
        }
        atomic_write_json(review_dir / f"page-{page['page_index']:04d}.json", review)

    required = [page["page_index"] for page in page_records if page["review_status"] == "pending"]
    summary = {
        "applicable": True,
        "page_count": len(page_records),
        "required_review_pages": required,
        "risk_counts": {str(risk): sum(page["risk"] == risk for page in page_records) for risk in range(4)},
        "ocr": {"tesseract": str(tesseract) if tesseract else None, "language": ocr_language, "available_languages": sorted(languages)},
        "contact_sheets": contact_refs,
    }
    atomic_write_json(project / "analysis" / "visual-summary.json", {"schema_version": 1, **summary})
    return units, book_map, summary


def show_page(project: Path, page_index: int) -> dict[str, Any]:
    page_map = load_json(project / "analysis" / "page-map.json")
    validate_schema(page_map, "page-map.schema.json")
    try:
        page = next(item for item in page_map["pages"] if item["page_index"] == page_index)
    except StopIteration as exc:
        raise BTPError(f"Unknown page index: {page_index}") from exc
    review_path = project / "qa" / "visual-reviews" / f"page-{page_index:04d}.json"
    return {
        "schema_version": 1,
        "page": page,
        "page_image": str(safe_project_path(project, page["image_ref"])),
        "crop_images": [str(safe_project_path(project, region["asset_ref"])) for region in page["regions"] if region.get("asset_ref")],
        "review_file": str(review_path),
        "review": load_json(review_path) if review_path.is_file() else None,
    }


def record_visual_review(project: Path, input_path: Path) -> dict[str, Any]:
    review = load_json(input_path)
    validate_schema(review, "visual-review.schema.json")
    if review["status"] == "pending" or not review["reviewer"].strip() or not review["reviewed_at"].strip():
        raise BTPError("A recorded visual review needs a final status, reviewer, and reviewed_at")
    page_map = load_json(project / "analysis" / "page-map.json")
    try:
        page = next(item for item in page_map["pages"] if item["page_index"] == review["page_index"])
    except StopIteration as exc:
        raise BTPError(f"Visual review references unknown page {review['page_index']}") from exc
    if review["page_hash"] != hash_data(semantic_page(page)):
        raise BTPError("Visual review is stale because the Page Map changed")
    valid_regions = {region["id"] for region in page["regions"]}
    valid_units = set(page.get("source_unit_ids", []))
    for finding in review["findings"]:
        if not set(finding["region_ids"]).issubset(valid_regions):
            raise BTPError(f"Finding {finding['id']} references an unknown region")
        if not set(finding["unit_ids"]).issubset(valid_units):
            raise BTPError(f"Finding {finding['id']} references a unit not mapped to this page")
        if finding["status"] == "open" and review["status"] == "approved":
            raise BTPError("An approved visual review cannot contain open findings")
    destination = project / "qa" / "visual-reviews" / f"page-{review['page_index']:04d}.json"
    atomic_write_json(destination, review)
    page["review_status"] = (
        "exception_approved" if review["status"] == "exception_approved"
        else "reviewed" if review["status"] == "approved"
        else "pending"
    )
    validate_schema(page_map, "page-map.schema.json")
    atomic_write_json(project / "analysis" / "page-map.json", page_map)

    manifest_path = project / "state" / "manifest.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        manifest["page_map_hash"] = hash_data(page_map)
        validate_schema(manifest, "manifest.schema.json")
        atomic_write_json(manifest_path, manifest)

    from .state import load_state, save_state

    state = load_state(project)
    for name in ("qa", "build"):
        stage = state["stages"][name]
        stage.update({"status": "invalidated", "reason_codes": ["visual_review_changed"], "updated_at": now_utc()})
    pending = []
    threshold = load_json(project / "resolved-config.json")["visual"]["minimum_review_risk"]
    for candidate in page_map["pages"]:
        if candidate["risk"] < threshold:
            continue
        path = project / "qa" / "visual-reviews" / f"page-{candidate['page_index']:04d}.json"
        if not path.is_file() or load_json(path).get("status") not in {"approved", "exception_approved"}:
            pending.append(candidate["page_index"])
    for item in state.get("human_reviews", []):
        if item["checkpoint"] == "visual_exceptions":
            item.update({
                "status": "pending" if pending else "approved",
                "reviewer": "page-review-coordinator" if not pending else item.get("reviewer", ""),
                "recorded_at": now_utc(),
                "notes": f"Pending page reviews: {pending}" if pending else "All required page reviews recorded.",
            })
    save_state(project, state)
    return {"recorded": "visual-review", "page_index": review["page_index"], "pending_pages": pending}


def run_visual_qa(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    source = safe_project_path(project, config["source"]["path"])
    if config["source"].get("format") != "pdf" and source.suffix.casefold() != ".pdf":
        report = {"schema_version": 1, "applicable": False, "passed": True, "failure_count": 0, "warning_count": 0, "checks": []}
        atomic_write_json(project / "qa" / "visual.json", report)
        return report
    page_map_path = project / "analysis" / "page-map.json"
    checks: list[dict[str, Any]] = []

    def add(check_id: str, status: str, message: str, page_index: int | None = None, region_id: str | None = None) -> None:
        item: dict[str, Any] = {"id": check_id, "status": status, "message": message}
        if page_index is not None:
            item["page_index"] = page_index
        if region_id:
            item["region_id"] = region_id
        checks.append(item)

    if not page_map_path.is_file():
        add("visual.page_map_missing", "fail", "PDF project has no Page Map")
        page_map = {"pages": [], "page_count": 0}
    else:
        try:
            page_map = load_json(page_map_path)
            validate_schema(page_map, "page-map.schema.json")
        except BTPError as exc:
            add("visual.page_map_invalid", "fail", str(exc))
            page_map = {"pages": [], "page_count": 0}
        else:
            if page_map.get("source_fingerprint") != sha256_file(source):
                add("visual.source_fingerprint", "fail", "Page Map belongs to different source bytes")
    indices = [page["page_index"] for page in page_map.get("pages", [])]
    if indices != list(range(1, page_map.get("page_count", 0) + 1)):
        add("visual.page_coverage", "fail", "Page Map indices are missing, duplicated, or out of order")
    try:
        with _fitz().open(source) as document:
            if len(document) != page_map.get("page_count", 0):
                add("visual.source_page_count", "fail", f"Source has {len(document)} pages but Page Map has {page_map.get('page_count', 0)}")
    except Exception as exc:
        add("visual.source_open", "fail", f"Cannot reopen source PDF: {exc}")

    seen_hashes: dict[str, int] = {}
    threshold = int(config["visual"]["minimum_review_risk"])
    for page in page_map.get("pages", []):
        index = page["page_index"]
        try:
            image_path = safe_project_path(project, page["image_ref"])
            if not image_path.is_file() or sha256_file(image_path) != page["image_sha256"]:
                add("visual.page_image", "fail", "Page image is missing or changed", index)
            elif min(page["pixel_width"], page["pixel_height"]) < 100:
                add("visual.page_image_size", "fail", "Rendered page image is too small", index)
        except BTPError as exc:
            add("visual.page_image", "fail", str(exc), index)
        if page["text_characters"] > 0 and page["image_sha256"] in seen_hashes:
            add("visual.duplicate_page", "fail", f"Rendered page duplicates page {seen_hashes[page['image_sha256']]}", index)
        seen_hashes[page["image_sha256"]] = index
        if page.get("ocr_status") in {"unavailable", "failed"}:
            add("visual.ocr_unresolved", "fail", f"OCR is {page['ocr_status']}: {page.get('ocr_issues', [])}", index)
        if page.get("ocr_status") == "completed":
            try:
                ocr_path = safe_project_path(project, page["ocr_text_ref"])
                if not ocr_path.is_file() or sha256_file(ocr_path) != page.get("ocr_text_sha256"):
                    add("visual.ocr_text", "fail", "OCR text is missing or changed", index)
            except (BTPError, KeyError) as exc:
                add("visual.ocr_text", "fail", str(exc), index)
        if page["page_kind"] not in {"blank", "cover"} and not page.get("source_unit_ids"):
            add("visual.page_unmapped", "fail", "Nonblank source page is not mapped to a work unit", index)
        for region in page["regions"]:
            box = region["box"]
            if box["x"] + box["width"] > 1.000001 or box["y"] + box["height"] > 1.000001:
                add("visual.crop_bounds", "fail", "Region box extends beyond the page", index, region["id"])
            if region.get("asset_ref"):
                crop_path = safe_project_path(project, region["asset_ref"])
                if not crop_path.is_file():
                    add("visual.crop_missing", "fail", "Required crop is missing", index, region["id"])
                else:
                    with Image.open(crop_path) as image:
                        if min(image.size) < 32:
                            # A one-line text or footnote crop naturally has a
                            # small height at ordinary render DPI. The 32 px
                            # minimum is meaningful for fixed visual assets,
                            # not for reflowable text evidence.
                            if region.get("role") in {"body", "footnote", "sidebar"}:
                                add("visual.crop_text_small", "warn", "Text evidence crop is smaller than 32 pixels; source page remains available", index, region["id"])
                            else:
                                add("visual.crop_too_small", "fail", "Crop is smaller than 32 pixels", index, region["id"])
                        extrema = image.convert("L").getextrema()
                        if not extrema or extrema[1] - extrema[0] < 2:
                            add("visual.crop_blank", "fail", "Crop appears blank", index, region["id"])
                coverage = box["width"] * box["height"]
                if coverage > 0.95 and page["layout"] != "fixed_layout":
                    add("visual.crop_whole_page", "fail", "Crop is an unapproved whole-page substitute", index, region["id"])
        if page["risk"] >= threshold:
            review_path = project / "qa" / "visual-reviews" / f"page-{index:04d}.json"
            if not review_path.is_file():
                add("visual.review_pending", "fail", "Required visual review is missing", index)
            else:
                try:
                    review = load_json(review_path)
                    validate_schema(review, "visual-review.schema.json")
                    if review["page_hash"] != hash_data(semantic_page(page)):
                        add("visual.review_stale", "fail", "Visual review predates the current Page Map", index)
                    if review["status"] not in {"approved", "exception_approved"}:
                        add("visual.review_pending", "fail", "Required visual review is not finalized", index)
                    elif page["reading_order_confidence"] < 0.6:
                        add("visual.reading_order_reviewed", "warn", "Low-confidence reading order was accepted by visual review", index)
                    for finding in review["findings"]:
                        if not set(finding["region_ids"]).issubset({region["id"] for region in page["regions"]}):
                            add("visual.finding_region", "fail", f"Finding {finding['id']} references an unknown region", index)
                        if not set(finding["unit_ids"]).issubset(set(page.get("source_unit_ids", []))):
                            add("visual.finding_unit", "fail", f"Finding {finding['id']} references an unmapped unit", index)
                        if finding["status"] == "open":
                            add("visual.finding_open", "fail", finding["description"], index)
                except BTPError as exc:
                    add("visual.review_invalid", "fail", str(exc), index)

    failures = [item for item in checks if item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] == "warn"]
    report = {
        "schema_version": 1,
        "applicable": True,
        "passed": not failures,
        "checked_at": now_utc(),
        "page_count": page_map.get("page_count", 0),
        "review_threshold": threshold,
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "checks": checks,
    }
    atomic_write_json(project / "qa" / "visual.json", report)
    lines = [
        "# Visual QA", "", f"Result: {'PASS' if report['passed'] else 'FAIL'}", f"Pages: {report['page_count']}",
        f"Failures: {len(failures)}", f"Warnings: {len(warnings)}", "",
        "| Status | Check | Page | Region | Message |", "|---|---|---:|---|---|",
    ]
    for item in checks:
        lines.append(f"| {item['status']} | {item['id']} | {item.get('page_index', '')} | {item.get('region_id', '')} | {item['message'].replace('|', '/')} |")
    atomic_write_text(project / "qa" / "visual.md", "\n".join(lines) + "\n")
    return report

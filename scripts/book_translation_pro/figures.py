"""Source-PDF figure inventory and target-asset auditing.

PDF extraction can preserve an exhibit caption while losing the vector drawing
that sits above it.  That loss is invisible to a Markdown image-reference
comparison because the source representation contains no ``<img>`` tag.  This
module inventories source exhibit captions directly from the PDF and checks
that each one has a target asset or an explicit, auditable reconstruction
exception.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import (
    BTPError,
    atomic_write_json,
    atomic_write_text,
    portable_path,
    safe_project_path,
)
from .structure import IMAGE_RE


SOURCE_CAPTION_RE = re.compile(
    r"^\s*(EXHIBIT|FIGURE|TABLE)\s+([0-9]+(?:\.[0-9]+)*[A-Z]?)\s*$",
    re.IGNORECASE,
)
TARGET_CAPTION_RE = re.compile(
    r"(?:图表|图|表)\s*([0-9]+(?:\.[0-9]+)*[A-Z]?)",
    re.IGNORECASE,
)
MARKED_TARGET_CAPTION_RE = re.compile(
    r"\*\*\s*(?:图表|图|表)\s*([0-9]+(?:\.[0-9]+)*[A-Z]?)(?=[\s*／/（(])",
    re.IGNORECASE,
)
PRINT_LAYOUT_LINE_RE = re.compile(
    r"\b[A-Za-z0-9_.-]+\.(?:qxd|indd)\s+\d{1,2}/\d{1,2}/\d{2,4}\s+"
    r"\d{1,2}:\d{2}\s+(?:AM|PM)\s+Page\s+(?:[ivxlcdm]+|\d+)\b",
    re.IGNORECASE,
)
CALIBRE_SPAN_RE = re.compile(r"<span\b[^>]*class\s*=\s*['\"][^'\"]*calibre\d+[^'\"]*['\"][^>]*>", re.IGNORECASE)

# These are explicit extraction/reconstruction observations used by existing
# projects.  They do not claim that a native image exists; they keep a known
# structured reconstruction visible as a warning rather than allowing it to
# disappear from the audit entirely.
STRUCTURED_RECONSTRUCTION_PREFIXES = (
    "source_chart_",
    "source_figure_fragmented",
    "source_figure_only",
    "source_figure_image_fragmented",
    "source_figure_ocr_fragment",
    "source_figure_legend_fragmented",
    "source_process_map_fragmented",
    "figure_text_fragment",
    "figure_image",
    "source_table_",
)


def _meaningful_image_refs(project: Path, markdown: str) -> Counter[str]:
    """Return only image references whose local payload is a real visual."""
    refs = Counter(IMAGE_RE.findall(markdown))
    try:
        from PIL import Image
    except ImportError:
        return refs
    meaningful: Counter[str] = Counter()
    for ref, count in refs.items():
        try:
            resource = safe_project_path(project, ref.replace("\\", "/"))
            with Image.open(resource) as image:
                if min(image.size) < 32:
                    continue
        except (BTPError, OSError, ValueError):
            # Keep missing resources in the inventory; the normal resource
            # check will provide the precise failure.
            meaningful[ref] += count
            continue
        meaningful[ref] += count
    return meaningful


def _source_exhibits(project: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    if config.get("source", {}).get("format") != "pdf":
        return []
    try:
        import fitz
    except ImportError as exc:
        raise BTPError("PyMuPDF is required to audit PDF figures") from exc

    source_path = safe_project_path(project, config["source"]["path"])
    exhibits: list[dict[str, Any]] = []
    with fitz.open(source_path) as document:
        for page_number, page in enumerate(document, 1):
            matches: list[dict[str, Any]] = []
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = " ".join(str(span.get("text", "")).split())
                        match = SOURCE_CAPTION_RE.match(text)
                        if not match or float(span.get("size", 99)) > 12:
                            continue
                        matches.append({
                            "kind": match.group(1).casefold(),
                            "key": match.group(2).upper(),
                            "bbox": [round(float(value), 2) for value in span["bbox"]],
                            "font_size": round(float(span.get("size", 0)), 2),
                        })
            if not matches:
                continue
            drawings = page.get_drawings()
            images = page.get_images(full=True)
            for match in matches:
                exhibits.append({
                    "key": match["key"],
                    "kind": match["kind"],
                    "source_pages": [page_number],
                    "caption_bbox": match["bbox"],
                    "source_visual": "embedded_image" if images else "vector_drawing" if len(drawings) >= 3 else "text_only",
                    "source_image_count": len(images),
                    "source_drawing_count": len(drawings),
                })
    return exhibits


def _figure_asset_name(key: str) -> str:
    return f"exhibit-{key.replace('.', '-').casefold()}.png"


def _target_root(project: Path, config: dict[str, Any]) -> Path:
    return project / "target" / ("edited" if config["passes"]["edit"] else "draft")


def _plain_target_line(line: str) -> str:
    value = re.sub(r"<[^>]+>", "", line)
    value = re.sub(r"[`*_]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _is_figure_fragment_line(line: str) -> bool:
    """Recognize extraction-only figure labels without touching prose/tables.

    Vector figures often arrive as a tail of calibre-wrapped labels after a
    page folio.  The wrapper is the important signal: ordinary translated
    prose is not wrapped, and native table reconstructions use pipe rows.
    This intentionally conservative predicate is only applied in the small
    window immediately before a target figure caption.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("<!--") or stripped.startswith(("|", "![", "#")):
        return False
    if not CALIBRE_SPAN_RE.search(stripped):
        return False
    plain = _plain_target_line(stripped)
    if not plain:
        return False
    # A full paragraph may end in an extracted footnote span.  It is prose,
    # not a figure fragment; require the visible content to be inside the
    # calibre span wrapper and keep long paragraph-like lines intact.
    without_spans = re.sub(r"<span\b[^>]*>.*?</span>", "", stripped, flags=re.IGNORECASE)
    if _plain_target_line(without_spans):
        return False
    if len(plain) > 140:
        return False
    # Running folios and source production stamps are cleaned elsewhere, not
    # treated as figure labels.  A numeric list/paragraph remains untouched.
    if PRINT_LAYOUT_LINE_RE.search(plain) or re.fullmatch(r"\d{1,4}(?:\s+.*)?", plain):
        return False
    if re.match(r"^(?:\d{1,3}[.、]|见注释|注释)\s*", plain):
        return False
    if re.search(r"\*\*[^*]+\*\*.*\*\*\d{1,4}\*\*", stripped):
        return False
    return True


def _has_caption_or_image(text: str) -> bool:
    return bool(TARGET_CAPTION_RE.search(re.sub(r"<[^>]+>", "", text))) or bool(IMAGE_RE.search(text))


def _strip_orphan_figure_fragments(
    lines: list[str],
    *,
    placeholder: str,
) -> tuple[list[str], int]:
    """Remove a trailing OCR-label block from one target unit.

    A unit containing only the labels is retained as an HTML comment because
    the edit manifest requires every unit to remain nonblank.  A unit with
    prose keeps the prose and loses only the trailing calibre-wrapped block.
    """
    meaningful = [line for line in lines if line.strip()]
    if meaningful and all(_is_figure_fragment_line(line) for line in meaningful):
        return [placeholder], len(meaningful)

    index = len(lines) - 1
    removed = 0
    while index >= 0:
        if not lines[index].strip():
            index -= 1
            continue
        if not _is_figure_fragment_line(lines[index]):
            break
        removed += 1
        index -= 1
    if not removed:
        return lines, 0
    kept = lines[: index + 1]
    while kept and not kept[-1].strip():
        kept.pop()
    kept.extend(["", placeholder])
    return kept, removed


def _apply_target_figure_repairs(
    project: Path,
    config: dict[str, Any],
    asset_keys: set[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Insert reconstructed assets and remove their duplicate OCR labels.

    This is deliberately idempotent.  It works after translation/editing,
    when translated captions exist, and leaves unresolved figures in the
    report for the audit gate instead of guessing a target location.  A
    forced run (explicit ``--keys``) is an editorial repair: the authoritative
    crop is inserted even when the unit already carries a textual digest
    table, which then stays in place as accompanying prose.
    """
    root = _target_root(project, config)
    paths = sorted(root.glob("unit-*.md"))
    texts = {path: path.read_text(encoding="utf-8") for path in paths}
    targets = _target_caption_units(project, config)
    caption_units = {entry["unit_id"] for entries in targets.values() for entry in entries}
    repaired: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for key in sorted(asset_keys, key=lambda value: tuple(
        int(part.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        for part in value.split(".")
        if part.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").isdigit()
    )):
        asset = f"assets/original/images/{_figure_asset_name(key)}"
        entries = targets.get(key, [])
        if not entries:
            unresolved.append({"key": key, "reason": "target_caption_not_found"})
            continue

        caption = entries[0]
        caption_path = safe_project_path(project, caption["path"])
        caption_text = texts.get(caption_path, caption_path.read_text(encoding="utf-8"))
        caption_index = paths.index(caption_path)
        # An exhibit counts as already visualized only when an image sits
        # within a few lines of its own caption.  Several captions can share
        # one unit, so a sibling exhibit's image must not block this one.
        caption_lines = caption_text.splitlines()
        caption_positions = [
            index for index, line in enumerate(caption_lines)
            if key in {
                match.group(1).upper()
                for match in MARKED_TARGET_CAPTION_RE.finditer(re.sub(r"<[^>]+>", "", line))
            }
        ]
        has_target_asset = any(
            f"![]({asset})" in "\n".join(caption_lines[max(0, position - 2):position + 3])
            for position in caption_positions
        )
        other_images: Counter[str] = Counter()
        for position in caption_positions:
            window = caption_lines[max(0, position - 3):position + 4]
            for reference in IMAGE_RE.findall("\n".join(window)):
                if reference.replace("\\", "/") != asset:
                    other_images[reference] += 1
        nearby_table_lines = sum(
            1 for line in caption_lines if line.count("|") >= 2
        )
        # A native table reconstruction is always written in the caption's own
        # unit, directly beside the caption line.  Tables in the next unit
        # belong to the next exhibit and must not block this insertion.
        if other_images and not has_target_asset:
            repaired.append({
                "key": key,
                "asset": asset,
                "caption_unit": caption["unit_id"],
                "asset_inserted": False,
                "skipped_reason": "existing_target_visual",
                "ocr_fragment_units": [],
                "ocr_fragment_lines_removed": 0,
            })
            continue
        # A native table reconstruction lives in the caption's own unit:
        # header, separator, and data rows all carry pipes, so a genuine
        # reconstruction yields at least three pipe lines beside the caption.
        # Forced editorial runs bypass this skip so a diagram whose textual
        # digest was mistaken for a faithful table still gets its figure.
        if nearby_table_lines >= 3 and not has_target_asset and not force:
            repaired.append({
                "key": key,
                "asset": asset,
                "caption_unit": caption["unit_id"],
                "asset_inserted": False,
                "skipped_reason": "native_table_reconstruction",
                "ocr_fragment_units": [],
                "ocr_fragment_lines_removed": 0,
            })
            continue
        # A declared structured reconstruction (for example
        # ``source_chart_fragmented``) records how the extraction degraded,
        # not an editorial decision to ship prose instead of the exhibit.
        # When an authoritative crop exists it is inserted here and the
        # translated description remains in place as accompanying prose.
        asset_inserted = False
        if asset not in caption_text:
            lines = caption_text.splitlines()
            inserted = False
            for index, line in enumerate(lines):
                plain = re.sub(r"<[^>]+>", "", line)
                # Match this exhibit's own caption number.  Several captions
                # can share one unit, so inserting before the first marked
                # caption of any key would plant the image above the wrong
                # exhibit.
                own_marked = any(
                    match.group(1).upper() == key
                    for match in MARKED_TARGET_CAPTION_RE.finditer(plain)
                )
                own_heading = (
                    line.lstrip().startswith("#")
                    and any(
                        match.group(1).upper() == key
                        for match in TARGET_CAPTION_RE.finditer(plain)
                    )
                )
                if own_marked or own_heading:
                    lines[index:index] = [f"![]({asset})", ""]
                    inserted = True
                    break
            if inserted:
                caption_text = "\n".join(lines).rstrip() + "\n"
                caption_path.write_text(caption_text, encoding="utf-8")
                texts[caption_path] = caption_text
                asset_inserted = True

        cursor = caption_index - 1
        changed_units: list[str] = []
        removed_lines = 0
        while cursor >= 0:
            path = paths[cursor]
            text = texts[path]
            if path.stem in caption_units:
                break
            if _has_caption_or_image(text):
                break
            lines = text.splitlines()
            cleaned, removed = _strip_orphan_figure_fragments(
                lines,
                placeholder=f"<!-- 图表 {key} 的源图已重建；本单元的源图 OCR 碎片已移除。 -->",
            )
            if removed:
                updated = "\n".join(cleaned).rstrip() + "\n"
                path.write_text(updated, encoding="utf-8")
                texts[path] = updated
                changed_units.append(path.stem)
                removed_lines += removed
            cursor -= 1
        repaired.append({
            "key": key,
            "asset": asset,
            "caption_unit": caption["unit_id"],
            "asset_inserted": asset_inserted,
            "ocr_fragment_units": changed_units,
            "ocr_fragment_lines_removed": removed_lines,
        })
    return {"repaired": repaired, "unresolved": unresolved}


def _visual_geometry(page: Any) -> list[Any]:
    """Collect usable vector and embedded-image rectangles from a PDF page.

    Degenerate clip rectangles (zero-area, far outside the page) and full-page
    frames are dropped: a naive union that includes them turns every crop into
    the whole page, which is how header rules and body text previously leaked
    into reconstructed exhibit assets.
    """
    rectangles: list[Any] = []
    page_area = abs(page.rect.width * page.rect.height)
    for drawing in page.get_drawings():
        rectangle = drawing.get("rect")
        if rectangle is None or rectangle.is_empty:
            continue
        if rectangle.x0 < page.rect.x0 - 2 or rectangle.y0 < page.rect.y0 - 2:
            continue
        if rectangle.x1 > page.rect.x1 + 2 or rectangle.y1 > page.rect.y1 + 2:
            continue
        if rectangle.width < 2 or rectangle.height < 2:
            continue
        if rectangle.width * rectangle.height > page_area * 0.9:
            continue
        rectangles.append(rectangle)
    for image in page.get_images(full=True):
        try:
            rects = list(page.get_image_rects(image[0]))
        except (AttributeError, ValueError):
            continue
        for rectangle in rects:
            if rectangle.is_empty or rectangle.width < 2 or rectangle.height < 2:
                continue
            rectangles.append(rectangle)
    return rectangles


def _cluster_rects(rectangles: list[Any], gap: float) -> list[list[Any]]:
    """Group geometry rectangles into clusters whose boxes touch when padded."""
    clusters: list[list[Any]] = []
    boxes: list[Any] = []
    for rectangle in sorted(rectangles, key=lambda rect: rect.get_area(), reverse=True):
        padded = rectangle + (-gap, -gap, gap, gap)
        target = None
        for index, box in enumerate(boxes):
            if box.intersects(padded):
                target = index
                break
        if target is None:
            clusters.append([rectangle])
            boxes.append(rectangle)
            continue
        clusters[target].append(rectangle)
        boxes[target] |= rectangle
    return clusters


def _figure_crop_box(
    page: Any,
    caption_bbox: list[float],
    avoid: list[Any] | None = None,
) -> tuple[Any, str] | None:
    """Infer a crop around the visual adjacent to a source exhibit caption.

    This is deliberately geometry-only.  It never attempts to OCR, translate,
    or redraw a figure; it preserves the authoritative source labels and lets
    the editorial pass decide whether a native target reconstruction is more
    appropriate.  Both sides of the caption are evaluated and the cluster
    nearest the caption wins — captions in the middle of a page may have
    their exhibit either above or below them, and pages can host several
    exhibits, in which case ``avoid`` lists crops already claimed by earlier
    exhibits on the same page.
    """
    import fitz

    caption = fitz.Rect(*caption_bbox)
    geometry = _visual_geometry(page)
    if not geometry:
        return None

    candidates_by_orientation: list[tuple[Any, str, float]] = []
    for orientation in ("below_caption", "above_caption"):
        header_zone = page.rect.y0 + page.rect.height * 0.15
        if orientation == "below_caption":
            # A small tolerance lets exhibits whose frame starts at the
            # caption's own line (boxed exhibits) qualify as below-caption.
            side = [
                rect for rect in geometry
                if rect.y0 >= caption.y1 - 12 and not (rect.height <= 24 and rect.y1 < header_zone)
            ]
        else:
            side = [
                rect for rect in geometry
                if rect.y1 <= caption.y0 + 2 and not (rect.height <= 24 and rect.y1 < header_zone)
            ]
        for cluster in _cluster_rects(side, gap=24.0):
            left = max(page.rect.x0, min(rect.x0 for rect in cluster) - 4)
            top = max(page.rect.y0, min(rect.y0 for rect in cluster) - 4)
            right = min(page.rect.x1, max(rect.x1 for rect in cluster) + 4)
            bottom = min(page.rect.y1, max(rect.y1 for rect in cluster) + 4)
            crop = fitz.Rect(left, top, right, bottom)
            if crop.width < 20 or crop.height < 48:  # a thin strip is a rule, not an exhibit
                continue
            distance = (crop.y0 - caption.y1) if orientation == "below_caption" else (caption.y0 - crop.y1)
            candidates_by_orientation.append((crop, orientation, float(distance)))
    if not candidates_by_orientation:
        return None
    candidates_by_orientation.sort(key=lambda item: item[2])

    def conflicts(crop: Any) -> bool:
        for claimed in avoid or []:
            overlap = crop & claimed
            if not overlap.is_empty and overlap.get_area() > 0.5 * min(crop.get_area(), claimed.get_area()):
                return True
        return False

    for crop, orientation, _distance in candidates_by_orientation:
        if not conflicts(crop):
            return crop, orientation
    crop, orientation, _distance = candidates_by_orientation[0]
    return crop, orientation


def _trim_scan_border(image: Any) -> Any:
    """Crop uniform dark/white scan borders from an extracted page image.

    Image-only PDF pages arrive as full-page scans whose edges carry the
    photocopier's black frame and the printed running head.  Trimming uniform
    edge bands keeps those artifacts out of the exhibit asset.  The guard
    keeps at least 40 percent of the image so a genuinely dark photograph is
    never cropped to nothing.
    """
    grayscale = image.convert("L")
    width, height = grayscale.size
    pixels = grayscale.load()
    if not width or not height:
        return image

    def row_is_uniform(y: int) -> bool:
        values = [pixels[x, y] for x in range(0, width, max(1, width // 256))]
        return max(values) - min(values) <= 12

    def column_is_uniform(x: int) -> bool:
        values = [pixels[x, y] for y in range(0, height, max(1, height // 256))]
        return max(values) - min(values) <= 12

    top = 0
    while top < height // 3 and row_is_uniform(top):
        top += 1
    bottom = height - 1
    while bottom > height * 2 // 3 and row_is_uniform(bottom):
        bottom -= 1
    left = 0
    while left < width // 3 and column_is_uniform(left):
        left += 1
    right = width - 1
    while right > width * 2 // 3 and column_is_uniform(right):
        right -= 1
    if (right - left) * (bottom - top) < 0.4 * width * height:
        return image
    if (left, top, right + 1, bottom + 1) == (0, 0, width, height):
        return image
    return image.crop((left, top, right + 1, bottom + 1))


def looks_like_inverted_lineart(image: Any) -> bool:
    """Detect light-ink-on-dark scans that must be flipped back to paper.

    Scanned line art (maps, diagrams, charts) with an inverted decode array
    arrives as white strokes on a black field.  Photographs keep a broad
    mid-tone band, so the mid-tone guard prevents flipping legitimate dark
    photos such as covers.
    """
    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = sum(histogram)
    if not total:
        return False
    dark = sum(histogram[:64]) / total
    mid = sum(histogram[80:176]) / total
    return dark > 0.55 and mid < 0.15


def normalize_image_background(path: Path) -> bool:
    """Flip an inverted dark-background line-art image to dark-on-white.

    Idempotent by construction: a normalized image has a light background and
    therefore fails the ``looks_like_inverted_lineart`` test on reruns.  The
    source handle is closed before saving — on Windows a same-path save with
    the file still open can silently fail.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise BTPError("Pillow is required for image normalization") from exc
    with Image.open(path) as image:
        if not looks_like_inverted_lineart(image):
            return False
        image.load()
        target_mode = image.mode if image.mode in {"L", "RGB"} else "L"
        flipped = ImageOps.invert(image.convert("L")).convert(target_mode)
    if path.suffix.casefold() in {".jpg", ".jpeg"}:
        flipped.save(path, quality=95)
    else:
        flipped.save(path)
    with Image.open(path) as saved:
        saved.load()
        if not looks_like_inverted_lineart(saved):
            return True
    # The overwrite silently failed (file lock or similar); report it so the
    # caller's audit does not claim a normalization that never landed.
    raise BTPError(f"Image normalization did not persist: {path}")


def normalize_image_backgrounds(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Normalize scanned-PDF source images whose decode is inverted.

    Inverted light-ink-on-dark scans are a PDF-scan phenomenon: CHM and EPUB
    sources carry images exactly as the publisher authored them, including
    legitimate dark photographs, so the heuristic must not touch them.  For
    non-PDF sources the command reports ``not_applicable`` without modifying
    anything.  For PDFs it scans the whole ``assets/original`` tree — CHM and
    EPUB extractions keep their topic folders, PDFs use ``images/`` — skipping
    the configured cover, and already-bright images are left untouched so the
    command is safe to rerun.
    """
    image_root = project / "assets" / "original"
    report: dict[str, Any] = {
        "schema_version": 1,
        "normalized": [],
        "skipped": 0,
        "missing_dir": not image_root.is_dir(),
    }
    if config.get("source", {}).get("format") != "pdf":
        report["status"] = "not_applicable"
        atomic_write_json(project / "qa" / "image-normalization.json", report)
        return report
    if not image_root.is_dir():
        atomic_write_json(project / "qa" / "image-normalization.json", report)
        return report
    cover_ref = str(config.get("metadata", {}).get("cover", "")).strip()
    cover_name = Path(cover_ref.replace("\\", "/")).name if cover_ref else ""
    for path in sorted(image_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp"}:
            continue
        if cover_name and path.name == cover_name:
            report["skipped"] += 1
            continue
        try:
            if normalize_image_background(path):
                report["normalized"].append(portable_path(path, project))
            else:
                report["skipped"] += 1
        except OSError:
            report["skipped"] += 1
    report["normalized_count"] = len(report["normalized"])
    atomic_write_json(project / "qa" / "image-normalization.json", report)
    return report


_HEADER_LINE_LIMIT = 0.115  # running heads sit above ~11.5% of the page height


def _scrub_header_band(page: Any, crop: Any, image: Any) -> Any:
    """White-out source running heads captured at the top of a crop.

    The crop rectangle comes from exhibit geometry and can start above the
    printed folio/title line.  Text lines ending in the top band of the page
    are running-head material; erasing their band removes the folio, the
    title, and the rule above the exhibit without touching exhibit labels,
    which always start lower on these pages.
    """
    from PIL import ImageDraw

    band_bottom = min(crop.y1, page.rect.y0 + page.rect.height * _HEADER_LINE_LIMIT)
    header_lines = []
    for block in page.get_text("dict", clip=crop)["blocks"]:
        for line in block.get("lines", []):
            text = " ".join(str(span.get("text", "")) for span in line.get("spans", [])).strip()
            if text and float(line["bbox"][3]) <= band_bottom:
                header_lines.append(line["bbox"])
    if not header_lines:
        return image
    scale_x = image.width / crop.width
    scale_y = image.height / crop.height
    draw = ImageDraw.Draw(image)
    top = max(0.0, crop.y0)
    bottom = min(band_bottom, max(bbox[3] for bbox in header_lines) + 12)
    draw.rectangle(
        [
            0,
            max(0, int((top - crop.y0) * scale_y)),
            image.width,
            min(image.height, int((bottom - crop.y0) * scale_y) + 1),
        ],
        fill="white",
    )
    return image


def _page_body_crop_fallback(document: Any, page: Any, destination: Path) -> dict[str, Any] | None:
    """Crop a rotated full-page exhibit straight from the page body.

    Landscape tables printed sideways on the page are drawn as one or two
    large filled rectangles (or pure text), so adjacent-geometry inference
    has nothing to cluster.  When the page carries almost no vector geometry,
    the exhibit is page-dominant: crop the body area between the running
    head and the folio, then trim scan borders.  Pages with real text bodies
    keep enough geometry to be handled by the normal crop path instead.
    """
    try:
        import fitz
        from PIL import Image
        import io

        if len(_visual_geometry(page)) > 3:
            return None
        height = page.rect.height
        clip = fitz.Rect(page.rect.x0, page.rect.y0 + height * 0.105, page.rect.x1, page.rect.y0 + height * 0.925)
        scale = 180 / 72
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, clip=clip)
        payload = Image.open(io.BytesIO(pixmap.tobytes("png")))
        # No border trim here: a rendered page body has no scan frame, and
        # trimming uniform-looking edge columns eats into dense rotated
        # tables that legitimately reach the margins.
        if looks_like_inverted_lineart(payload):
            from PIL import ImageOps

            payload = ImageOps.invert(payload.convert("L"))
        payload.save(destination)
        return {
            "render_method": "page_body_crop_180dpi",
            "pixel_box": [0, 0, payload.width, payload.height],
        }
    except BTPError:
        raise
    except Exception:
        return None


def _embedded_page_fallback(document: Any, page: Any, destination: Path) -> dict[str, Any] | None:
    """Rebuild an exhibit from the page's own embedded scan when geometry fails.

    Image-only exhibit pages (rotated landscape charts, photocopied tables)
    have no vector geometry beside the caption, so the crop inference cannot
    see them.  The largest embedded image that fills most of the page is the
    authoritative exhibit; extracting it directly keeps the native upright
    orientation, and the border trim removes the scan frame the crop would
    otherwise carry into the book.
    """
    try:
        from PIL import Image
        import io

        images = page.get_images(full=True)
        page_area = abs(page.rect.width * page.rect.height)
        best_xref = None
        best_ratio = 0.0
        for image in images:
            try:
                rects = page.get_image_rects(image[0])
            except (AttributeError, ValueError):
                continue
            for rect in rects:
                ratio = (rect.width * rect.height) / page_area if page_area else 0.0
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_xref = image[0]
        if best_xref is None or best_ratio < 0.45:
            return None
        base = document.extract_image(best_xref)
        payload = Image.open(io.BytesIO(base["image"]))
        rotated = payload
        if page.rotation % 360 in (90, 270):
            rotated = payload.transpose(Image.ROTATE_270 if page.rotation % 360 == 90 else Image.ROTATE_90)
        trimmed = _trim_scan_border(rotated)
        if looks_like_inverted_lineart(trimmed):
            from PIL import ImageOps

            trimmed = ImageOps.invert(trimmed.convert("L"))
        trimmed.save(destination)
        return {
            "render_method": "embedded_image_extract_trimmed",
            "source_image_xref": int(best_xref),
            "coverage_ratio": round(best_ratio, 3),
            "pixel_box": [0, 0, trimmed.width, trimmed.height],
        }
    except BTPError:
        raise
    except Exception:
        return None


def reconstruct_figures(
    project: Path,
    config: dict[str, Any],
    *,
    only_missing: bool = True,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """Create deterministic source-page crops for PDF exhibits.

    The command is safe to rerun: by default it preserves an existing asset,
    including an editor-approved crop, and only creates missing assets.  It
    writes an audit report so a later target edit can cite the exact source
    page, crop rectangle, and rendering method used.  ``keys`` limits the run
    to the named exhibit keys and force-regenerates exactly those assets, so
    a bad crop can be repaired without clobbering reviewed siblings.
    """
    source_exhibits = _source_exhibits(project, config)
    if not source_exhibits:
        report = {
            "schema_version": 1,
            "status": "not_applicable",
            "source_format": config.get("source", {}).get("format"),
            "generated": [],
            "skipped": [],
            "unavailable": [],
        }
        atomic_write_json(project / "qa" / "figure-reconstructions.json", report)
        return report

    try:
        import fitz
    except ImportError as exc:
        raise BTPError("PyMuPDF is required to reconstruct PDF figures") from exc

    source_path = safe_project_path(project, config["source"]["path"])
    image_dir = project / "assets" / "original" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    wanted = {key.upper() for key in keys} if keys else None
    unique: dict[str, dict[str, Any]] = {}
    for exhibit in source_exhibits:
        if wanted is not None and exhibit["key"] not in wanted:
            continue
        unique.setdefault(exhibit["key"], exhibit)

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    claimed_by_page: dict[int, list[Any]] = {}
    with fitz.open(source_path) as document:
        for key in sorted(unique, key=lambda value: tuple(
            int(part.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
            for part in value.split(".")
            if part.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").isdigit()
        )):
            exhibit = unique[key]
            destination = image_dir / _figure_asset_name(key)
            force = wanted is not None
            if only_missing and not force and destination.is_file() and destination.stat().st_size > 0:
                skipped.append({"key": key, "asset": portable_path(destination, project), "reason": "existing"})
                continue
            page_number = exhibit["source_pages"][0]
            page = document[page_number - 1]
            inferred = _figure_crop_box(page, exhibit["caption_bbox"], avoid=claimed_by_page.get(page_number, []))
            if inferred is not None:
                claimed_by_page.setdefault(page_number, []).append(inferred[0])
            if inferred is None:
                embedded = _embedded_page_fallback(document, page, destination)
                if embedded is None:
                    embedded = _page_body_crop_fallback(document, page, destination)
                if embedded is None:
                    unavailable.append({
                        "key": key,
                        "source_page": page_number,
                        "reason": "no_adjacent_vector_or_embedded_image_geometry",
                    })
                    continue
                normalize_image_background(destination)
                generated.append({
                    "key": key,
                    "source_page": page_number,
                    "caption_bbox": exhibit["caption_bbox"],
                    "crop_bbox": None,
                    "orientation": "embedded_image",
                    "asset": portable_path(destination, project),
                    **embedded,
                })
                continue
            crop, orientation = inferred
            page_image = project / "assets" / "page-images" / f"page-{page_number:04d}.png"
            if page_image.is_file() and page_image.stat().st_size > 0:
                from PIL import Image

                with Image.open(page_image) as image:
                    scale_x = image.width / page.rect.width
                    scale_y = image.height / page.rect.height
                    pixels = (
                        max(0, int(crop.x0 * scale_x)),
                        max(0, int(crop.y0 * scale_y)),
                        min(image.width, int(crop.x1 * scale_x + 0.999)),
                        min(image.height, int(crop.y1 * scale_y + 0.999)),
                    )
                    cropped = _scrub_header_band(page, crop, image.crop(pixels))
                    cropped.save(destination)
                render_method = "authoritative_page_image"
                pixel_box = list(pixels)
            else:
                scale = 180 / 72
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, clip=crop)
                pixmap.save(destination)
                render_method = "source_pdf_render_180dpi"
                pixel_box = [0, 0, pixmap.width, pixmap.height]
            normalize_image_background(destination)
            generated.append({
                "key": key,
                "source_page": page_number,
                "caption_bbox": exhibit["caption_bbox"],
                "crop_bbox": [round(float(value), 2) for value in crop],
                "pixel_box": pixel_box,
                "orientation": orientation,
                "asset": portable_path(destination, project),
                "render_method": render_method,
            })

    asset_keys = {
        item["key"] for item in generated
    } | {
        item["key"] for item in skipped
        if Path(project / item["asset"]).is_file()
    }
    if wanted is not None:
        # Repairs may only reference assets that actually exist.  Wanted keys
        # whose regeneration failed stay in the unavailable list instead.
        failed = {item["key"] for item in unavailable}
        asset_keys |= (wanted - failed) | {
            item["key"] for item in skipped
        }
        for key in sorted(wanted - set(unique)):
            unavailable.append({"key": key, "reason": "source_caption_not_found"})
    target_repairs = _apply_target_figure_repairs(project, config, asset_keys, force=wanted is not None)
    if wanted is not None:
        target_repairs["repaired"] = [
            item for item in target_repairs["repaired"] if item["key"] in wanted
        ]

    report = {
        "schema_version": 1,
        "status": "completed",
        "source_format": config["source"]["format"],
        "source_path": config["source"]["path"],
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "unavailable_count": len(unavailable),
        "generated": generated,
        "skipped": skipped,
        "unavailable": unavailable,
        "target_repairs": target_repairs,
    }
    atomic_write_json(project / "qa" / "figure-reconstructions.json", report)
    return report


def _target_caption_units(project: Path, config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    require_edit = bool(config["passes"]["edit"])
    root = project / "target" / ("edited" if require_edit else "draft")
    result: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(root.glob("unit-*.md")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            # Match only a marked caption, not a prose reference that happens
            # to contain a footnote span or another HTML wrapper.  Captions in
            # the target are emitted as bold text (possibly inside a span) or
            # as a Markdown heading; ordinary prose references are not a
            # reliable location for a figure asset.
            plain = re.sub(r"<[^>]+>", "", line)
            marked = list(MARKED_TARGET_CAPTION_RE.finditer(plain))
            if not marked and not line.lstrip().startswith("#"):
                continue
            matches = marked or list(TARGET_CAPTION_RE.finditer(plain))
            for match in matches:
                key = match.group(1).upper()
                item = {
                    "unit_id": path.stem,
                    "path": portable_path(path, project),
                    "caption_line": line.strip(),
                }
                if item not in result.setdefault(key, []):
                    result[key].append(item)
    return result


def _observation_issues(project: Path, unit_id: str) -> list[str]:
    path = project / "state" / "observations" / f"{unit_id}.json"
    if not path.is_file():
        return []
    import json

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item) for item in value.get("source_issues", [])]


def _declared_structured_reconstruction(issues: list[str]) -> list[str]:
    return [
        issue for issue in issues
        if any(issue.startswith(prefix) for prefix in STRUCTURED_RECONSTRUCTION_PREFIXES)
    ]


def run_figure_audit(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Inventory source exhibits and audit their target visual disposition."""
    source_exhibits = _source_exhibits(project, config)
    if not source_exhibits:
        report = {
            "schema_version": 1,
            "status": "not_applicable",
            "passed": True,
            "source_format": config.get("source", {}).get("format"),
            "figures": [],
        }
        atomic_write_json(project / "qa" / "figure-audit.json", report)
        atomic_write_text(project / "qa" / "figure-audit.md", "# Figure Audit\n\nNot applicable to this source format.\n")
        return report

    targets = _target_caption_units(project, config)
    unique: dict[str, dict[str, Any]] = {}
    for item in source_exhibits:
        key = item["key"]
        current = unique.setdefault(key, {
            "key": key,
            "kind": item["kind"],
            "source_pages": [],
            "source_visuals": [],
            "source_image_count": 0,
            "source_drawing_count": 0,
        })
        current["source_pages"].extend(item["source_pages"])
        current["source_visuals"].append(item["source_visual"])
        current["source_image_count"] += item["source_image_count"]
        current["source_drawing_count"] += item["source_drawing_count"]

    figures: list[dict[str, Any]] = []
    for key in sorted(unique, key=lambda value: tuple(int(part.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) for part in value.split(".") if part.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").isdigit())):
        item = unique[key]
        caption_units = targets.get(key, [])
        unit_ids = sorted({entry["unit_id"] for entry in caption_units})
        image_refs: Counter[str] = Counter()
        issues: list[str] = []
        table_signal_count = 0
        for unit_id in unit_ids:
            path = safe_project_path(project, f"target/{'edited' if config['passes']['edit'] else 'draft'}/{unit_id}.md")
            if path.is_file():
                target_text = path.read_text(encoding="utf-8")
                image_refs.update(_meaningful_image_refs(project, target_text))
                table_signal_count += sum(1 for line in target_text.splitlines() if line.count("|") >= 2)
            issues.extend(_observation_issues(project, unit_id))
        structured = sorted(set(_declared_structured_reconstruction(issues)))
        if image_refs:
            disposition = "target_asset"
            status = "pass"
            message = f"Exhibit {key} has target visual asset(s)"
        elif table_signal_count >= 2:
            disposition = "native_table_reconstruction"
            status = "warn"
            message = f"Exhibit {key} has no image asset but is represented by a native target table"
        elif structured:
            disposition = "declared_structured_reconstruction"
            status = "warn"
            message = f"Exhibit {key} has no image asset; declared reconstruction evidence: {', '.join(structured[:4])}"
        elif not caption_units:
            disposition = "missing_target_caption"
            status = "fail"
            message = f"Exhibit {key} has no mapped target caption or visual asset"
        else:
            disposition = "missing_visual_asset"
            status = "fail"
            message = f"Exhibit {key} has a target caption but no visual asset or reconstruction record"
        figures.append({
            **item,
            "source_pages": sorted(set(item["source_pages"])),
            "source_visuals": sorted(set(item["source_visuals"])),
            "target_units": unit_ids,
            "target_caption_lines": [entry["caption_line"] for entry in caption_units],
            "target_image_refs": dict(image_refs),
            "target_table_signal_count": table_signal_count,
            "declared_reconstruction_issues": structured,
            "disposition": disposition,
            "status": status,
            "message": message,
        })

    failures = [item for item in figures if item["status"] == "fail"]
    warnings = [item for item in figures if item["status"] == "warn"]
    report = {
        "schema_version": 1,
        "status": "completed",
        "passed": not failures,
        "source_format": config["source"]["format"],
        "source_path": config["source"]["path"],
        "figure_count": len(figures),
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "figures": figures,
    }
    atomic_write_json(project / "qa" / "figure-audit.json", report)
    lines = [
        "# Figure Audit", "", f"Result: {'PASS' if report['passed'] else 'FAIL'}",
        f"Figures: {report['figure_count']}",
        f"Failures: {report['failure_count']}",
        f"Warnings: {report['warning_count']}", "",
        "| Status | Exhibit | Source pages | Target units | Disposition |", "|---|---|---|---|---|",
    ]
    for item in figures:
        lines.append(
            f"| {item['status']} | {item['key']} | {', '.join(map(str, item['source_pages']))} | "
            f"{', '.join(item['target_units']) or '-'} | {item['disposition']} |"
        )
    atomic_write_text(project / "qa" / "figure-audit.md", "\n".join(lines) + "\n")
    return report

"""Stable source/target segment-group alignment and bilingual rendering."""

from __future__ import annotations

import hashlib
import re
from textwrap import dedent
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, atomic_write_text, safe_project_path, validate_schema
from .manifest import load_manifest
from .structure import parse_blocks

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_segment_alignment(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Align each stable work-unit segment group with its verified target.

    A work unit may contain several indivisible Markdown blocks. Keeping their
    source segment IDs together avoids inventing sentence-level correspondence
    that the translation worker did not record.
    """
    manifest = load_manifest(project)
    require_edit = bool(config["passes"]["edit"])
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for order, entry in enumerate(sorted(manifest["units"], key=lambda item: item["order"])):
        source_path = safe_project_path(project, entry["source_file"])
        target_path = safe_project_path(
            project, entry["edit_file"] if require_edit else entry["draft_file"]
        )
        source_text = source_path.read_text(encoding="utf-8").strip()
        target_text = target_path.read_text(encoding="utf-8").strip()
        if not source_text:
            # A unit emptied when its exhibit fragments were rebuilt in another unit has
            # nothing to pair; its translation may only say so in a comment.
            if _HTML_COMMENT_RE.sub("", target_text).strip():
                raise BTPError(f"{entry['unit_id']}: the source unit is empty but its translation is not; "
                               "move the translation to the unit that now holds its source")
            skipped.append({"unit_id": entry["unit_id"], "reason": "empty source unit; translation holds only a comment"})
            continue
        items.append({
            "id": f"align-{order + 1:04d}",
            "order": order,
            "unit_id": entry["unit_id"],
            "book_node_id": entry.get("book_node_id"),
            "source_segment_ids": entry["segment_ids"],
            "source_text": source_text,
            "target_text": target_text,
            "source_sha256": _text_hash(source_text),
            "target_sha256": _text_hash(target_text),
        })
    value = {
        "schema_version": 1,
        "source_fingerprint": manifest["source_fingerprint"],
        "target_language": config["target"]["language"],
        "items": items,
        **({"skipped_units": skipped} if skipped else {}),
    }
    validate_schema(value, "segment-alignment.schema.json")
    path = project / "target" / "alignment.json"
    atomic_write_json(path, value)
    return value


def _paired_blocks(source: str, target: str) -> list[tuple[str, str]]:
    """Pair a unit's blocks one to one when both sides have the same block shape.

    The recorded alignment is per unit; a block-for-block pairing is only
    shown when the kinds line up exactly (heading, paragraph, table, ...),
    which is what the translation protocol produces.  Otherwise the unit is
    shown whole rather than guessing which paragraph matches which.
    """
    source_blocks, target_blocks = parse_blocks(source), parse_blocks(target)
    if len(source_blocks) > 1 and [block.kind for block in source_blocks] == [block.kind for block in target_blocks]:
        pairs = list(zip(_standalone_texts(source_blocks), _standalone_texts(target_blocks)))
        # Equal kinds do not prove equal content: PDF page headers and folios
        # the translation dropped, or paragraphs it merged and split, leave the
        # same block count with every pair shifted.  Pair only when each pair
        # could be the same text.
        if all(_plausible_pair(left, right) for left, right in pairs):
            return pairs
    return [(source, target)]


_NUMBER_RE = re.compile(r"\d[\d,.]*\d")
_CJK_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")


def _visible_text(markdown: str) -> str:
    return re.sub(r"<[^>]+>", " ", markdown)


def _content_weight(text: str) -> float:
    """Words plus 0.6 per CJK character, the weighting the QA length check uses."""
    cjk = len(_CJK_RE.findall(text))
    return len(_WORD_RE.findall(_CJK_RE.sub(" ", text))) + 0.6 * cjk


def _plausible_pair(source: str, target: str) -> bool:
    """Whether two blocks could be translations of each other.

    Length must agree within a translation's normal spread, and the multi-digit
    numbers of the source must mostly survive.  Short blocks (a folio, a running
    head) pass on length only when both are short.
    """
    source_text, target_text = _visible_text(source), _visible_text(target)
    source_weight, target_weight = _content_weight(source_text), _content_weight(target_text)
    if max(source_weight, target_weight) >= 12 and not 0.4 <= (target_weight + 1) / (source_weight + 1) <= 2.5:
        return False
    numbers = {number.replace(",", "") for number in _NUMBER_RE.findall(source_text)}
    if len(numbers) >= 2:
        target_digits = target_text.replace(",", "")
        if sum(number in target_digits for number in numbers) < len(numbers) / 2:
            return False
    return True


_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def _standalone_texts(blocks: list[Any]) -> list[str]:
    """Block texts made safe to stand alone inside an alignment div.

    A list item's continuation block keeps its indentation; standing alone
    that reads as a code block to Pandoc, and its images and anchors would
    never reach the edition as elements, so it is dedented.  An indented
    block outside a list is a real code block and keeps its indentation.
    """
    texts: list[str] = []
    in_list = False
    for block in blocks:
        first = block.text.splitlines()[0]
        indent = len(first) - len(first.lstrip())
        if _LIST_ITEM_RE.match(first):
            in_list = True
        elif not indent:
            in_list = False
        texts.append(dedent(block.text) if in_list or indent < 4 else block.text)
    return texts


_ID_SPAN_RE = re.compile(
    r"<span\b[^>]*\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*>\s*</span>",
    re.IGNORECASE,
)
_PAGE_ANCHOR_RE = re.compile(r"<a\b[^>]*\bhref\s*=\s*[\"']#([^\"']+)[\"']", re.IGNORECASE)
_ANCHOR_OPEN_TAG_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_ANCHOR_ID_ATTR_RE = re.compile(r"\s+id\s*=\s*[\"'](?P<value>[^\"']+)[\"']", re.IGNORECASE)


def _drop_wrapper_tags(fragment: str, side: str, seen: set[str]) -> str:
    """Drop div/span wrapper tags; keep id anchors, namespaced by language side.

    Wrappers that open in one work unit and close in a later one cannot nest
    inside per-alignment containers, so their tags are removed.  An empty
    ``<span id="ch01fig01"></span>`` is a self-contained link target for the
    source's internal cross-references, so it stays — but the id and every
    in-page reference get a side prefix, because the source and target halves
    of one unit carry the same ids and cross-unit links must keep resolving
    (the namespace is per side, not per unit).  CHM topics repeat their
    chapter anchor on every page, so only the first occurrence per side is
    kept as an anchor; later ones would be duplicate ids.
    """
    anchors: list[str] = []

    def stash(match: re.Match[str]) -> str:
        identifier = f"{side}-{match.group(1)}"
        anchors.append(f'<span id="{identifier}"></span>' if identifier not in seen else "")
        seen.add(identifier)
        return f"\x00btp-anchor-{len(anchors) - 1}\x00"

    def namespace_anchor_id(match: re.Match[str]) -> str:
        # An id on a content-bearing anchor (a CHM image link ``<a id="IMG_1">``)
        # repeats on both language sides like any other id.  Calibre extraction
        # ids are left for ``strip_extraction_links``.
        tag = match.group(0)
        id_match = _ANCHOR_ID_ATTR_RE.search(tag)
        if not id_match or id_match.group("value").startswith("calibre_link-"):
            return tag
        identifier = f"{side}-{id_match.group('value')}"
        replacement = "" if identifier in seen else f' id="{identifier}"'
        seen.add(identifier)
        return tag[:id_match.start()] + replacement + tag[id_match.end():]

    fragment = _ID_SPAN_RE.sub(stash, fragment)
    fragment = _ANCHOR_OPEN_TAG_RE.sub(namespace_anchor_id, fragment)
    fragment = _PAGE_ANCHOR_RE.sub(lambda match: match.group(0).replace(f"#{match.group(1)}", f"#{side}-{match.group(1)}"), fragment)
    fragment = re.sub(r"</?(?:div|span)\b[^>]*>", "", fragment, flags=re.IGNORECASE)
    for index, anchor in enumerate(anchors):
        fragment = fragment.replace(f"\x00btp-anchor-{index}\x00", anchor)
    return fragment


# Word joins two tables that touch into one, so a source table and its translation (or a
# translation and the next source table) need a paragraph between them: a no-break space.
TABLE_GAP = " "


def _edge_line(block: str, first: bool) -> str:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    return (lines[0] if first else lines[-1]) if lines else ""


def render_bilingual_markdown(alignment: dict[str, Any], destination: Path | None = None) -> str:
    validate_schema(alignment, "segment-alignment.schema.json")
    blocks: list[str] = []
    seen_ids: dict[str, set[str]] = {"src": set(), "tgt": set()}
    previous_ends_in_table = False
    for item in sorted(alignment["items"], key=lambda row: row["order"]):
        source_fragment = _drop_wrapper_tags(item["source_text"], "src", seen_ids["src"])
        target_fragment = _drop_wrapper_tags(item["target_text"], "tgt", seen_ids["tgt"])
        def strip_extraction_links(value: str) -> str:
            value = re.sub(
                r"\[((?:\\[\[\]]|[^\]])+)\]\(#calibre_link-\d+(?:\s+[\"'][^\"']*[\"'])?\)",
                r"\1",
                value,
                flags=re.IGNORECASE,
            )
            return re.sub(
                r'<a\b[^>]*(?:href=["\']#calibre_link-\d+["\']|id=["\']calibre_link-\d+["\'])[^>]*>(.*?)</a>',
                r"\1",
                value,
                flags=re.IGNORECASE | re.DOTALL,
            )
        source_fragment = strip_extraction_links(source_fragment)
        target_fragment = strip_extraction_links(target_fragment)
        source_text = re.sub(
            r"\[\^([^]]+)\]", lambda match: f"[^{item['id']}-source-{match.group(1)}]", source_fragment
        )
        target_text = re.sub(
            r"\[\^([^]]+)\]", lambda match: f"[^{item['id']}-target-{match.group(1)}]", target_fragment
        )
        for number, (source_block, target_block) in enumerate(_paired_blocks(source_text, target_text), 1):
            for side, block in (("source", source_block), ("target", target_block)):
                if _edge_line(block, first=True).startswith("|") and previous_ends_in_table:
                    blocks.extend(["", TABLE_GAP, ""])
                previous_ends_in_table = _edge_line(block, first=False).startswith("|")
                blocks.extend([
                    f"::: {{.btp-{side} data-alignment-id=\"{item['id']}\" data-block=\"{number}\"}}",
                    *([""] if side == "target" else []),
                    block,
                    "",
                    ":::",
                    *([""] if side == "source" else []),
                ])
    text = "\n".join(blocks).strip() + "\n"
    if destination is not None:
        atomic_write_text(destination, text)
    return text

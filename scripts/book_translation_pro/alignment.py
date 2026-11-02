"""Stable source/target segment-group alignment and bilingual rendering."""

from __future__ import annotations

import hashlib
import re
from textwrap import dedent
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json, atomic_write_text, safe_project_path, validate_schema
from .manifest import load_manifest
from .structure import parse_blocks


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
    for order, entry in enumerate(sorted(manifest["units"], key=lambda item: item["order"])):
        source_path = safe_project_path(project, entry["source_file"])
        target_path = safe_project_path(
            project, entry["edit_file"] if require_edit else entry["draft_file"]
        )
        source_text = source_path.read_text(encoding="utf-8").strip()
        target_text = target_path.read_text(encoding="utf-8").strip()
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
        return list(zip(_standalone_texts(source_blocks), _standalone_texts(target_blocks)))
    return [(source, target)]


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

    fragment = _ID_SPAN_RE.sub(stash, fragment)
    fragment = _PAGE_ANCHOR_RE.sub(lambda match: match.group(0).replace(f"#{match.group(1)}", f"#{side}-{match.group(1)}"), fragment)
    fragment = re.sub(r"</?(?:div|span)\b[^>]*>", "", fragment, flags=re.IGNORECASE)
    for index, anchor in enumerate(anchors):
        fragment = fragment.replace(f"\x00btp-anchor-{index}\x00", anchor)
    return fragment


def render_bilingual_markdown(alignment: dict[str, Any], destination: Path | None = None) -> str:
    validate_schema(alignment, "segment-alignment.schema.json")
    blocks: list[str] = []
    seen_ids: dict[str, set[str]] = {"src": set(), "tgt": set()}
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
            blocks.extend([
                f"::: {{.btp-source data-alignment-id=\"{item['id']}\" data-block=\"{number}\"}}",
                source_block,
                "",
                ":::",
                "",
                f"::: {{.btp-target data-alignment-id=\"{item['id']}\" data-block=\"{number}\"}}",
                "",
                target_block,
                "",
                ":::",
            ])
    text = "\n".join(blocks).strip() + "\n"
    if destination is not None:
        atomic_write_text(destination, text)
    return text

"""Stable source/target segment-group alignment and bilingual rendering."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json, atomic_write_text, safe_project_path, validate_schema
from .manifest import load_manifest


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


def render_bilingual_markdown(alignment: dict[str, Any], destination: Path | None = None) -> str:
    validate_schema(alignment, "segment-alignment.schema.json")
    blocks: list[str] = []
    for item in sorted(alignment["items"], key=lambda row: row["order"]):
        # Extraction wrappers frequently open in one work unit and close in a
        # later unit. They cannot be nested safely inside per-alignment
        # containers, so retain their content and drop only div/span wrappers.
        source_fragment = re.sub(r"</?(?:div|span)\b[^>]*>", "", item["source_text"], flags=re.IGNORECASE)
        target_fragment = re.sub(r"</?(?:div|span)\b[^>]*>", "", item["target_text"], flags=re.IGNORECASE)
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
        blocks.extend([
            f"::: {{.btp-source data-alignment-id=\"{item['id']}\"}}",
            source_text,
            "",
            ":::",
            "",
            f"::: {{.btp-target data-alignment-id=\"{item['id']}\"}}",
            "",
            target_text,
            "",
            ":::",
        ])
    text = "\n".join(blocks).strip() + "\n"
    if destination is not None:
        atomic_write_text(destination, text)
    return text

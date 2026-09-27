"""Markdown structure parsing, semantic chunking, and Book Map drafting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
IMAGE_RE = re.compile(
    r"(?:!\[[^\]]*\]\(|<img\b[^>]*\bsrc\s*=\s*[\"'])([^)\s\"']+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Segment:
    id: str
    order: int
    kind: str
    text: str
    heading_level: int | None = None
    title: str | None = None
    book_node_id: str | None = None


def parse_blocks(markdown: str) -> list[Segment]:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw_blocks: list[str] = []
    buffer: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        if buffer:
            text = "\n".join(buffer).strip("\n")
            if text.strip():
                raw_blocks.append(text)
            buffer.clear()

    for line in lines:
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if not in_fence:
                flush()
                in_fence = True
                fence_marker = marker
            buffer.append(line)
            if in_fence and stripped.startswith(fence_marker) and len(buffer) > 1:
                in_fence = False
                flush()
            continue
        if not in_fence and not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()

    segments: list[Segment] = []
    for index, text in enumerate(raw_blocks, 1):
        first = text.splitlines()[0]
        match = HEADING_RE.match(first) if len(text.splitlines()) == 1 else None
        if match:
            kind = "heading"
            level = len(match.group(1))
            title = match.group(2).strip()
        elif first.lstrip().startswith(("```", "~~~")):
            kind, level, title = "code", None, None
        elif IMAGE_RE.search(text) and len(text.splitlines()) <= 3:
            kind, level, title = "image", None, None
        elif all("|" in line for line in text.splitlines()) and len(text.splitlines()) >= 2:
            kind, level, title = "table", None, None
        elif first.lstrip().startswith(("- ", "* ", "+ ")) or re.match(r"^\s*\d+[.)]\s", first):
            kind, level, title = "list", None, None
        else:
            kind, level, title = "paragraph", None, None
        segments.append(Segment(f"seg-{index:06d}", index, kind, text, level, title))
    return segments


def _split_long_segment(segment: Segment, hard_max: int) -> list[Segment]:
    if len(segment.text) <= hard_max or segment.kind in {"code", "table"}:
        return [segment]
    pieces: list[str] = []
    remaining = segment.text
    while len(remaining) > hard_max:
        cut = max(
            remaining.rfind("\n", 0, hard_max),
            remaining.rfind("。", 0, hard_max),
            remaining.rfind(". ", 0, hard_max),
            remaining.rfind(" ", 0, hard_max),
        )
        if cut < hard_max // 2:
            cut = hard_max
        else:
            cut += 1
        pieces.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        pieces.append(remaining)
    return [
        Segment(
            f"{segment.id}-p{part:02d}",
            segment.order,
            segment.kind,
            text,
            segment.heading_level,
            segment.title,
            segment.book_node_id,
        )
        for part, text in enumerate(pieces, 1)
    ]


def expand_long_segments(segments: list[Segment], hard_max: int) -> list[Segment]:
    expanded: list[Segment] = []
    for segment in segments:
        expanded.extend(_split_long_segment(segment, hard_max))
    return [
        Segment(
            item.id,
            index,
            item.kind,
            item.text,
            item.heading_level,
            item.title,
            item.book_node_id,
        )
        for index, item in enumerate(expanded, 1)
    ]


def draft_book_map(segments: list[Segment], source_fingerprint: str) -> tuple[dict[str, Any], list[Segment]]:
    nodes: list[dict[str, Any]] = []
    root_ids: list[str] = []
    stack: list[tuple[int, str]] = []
    assignments: dict[str, str] = {}

    if not segments or segments[0].kind != "heading":
        node_id = "front-0001" if any(s.kind == "heading" for s in segments) else "chapter-0001"
        kind = "front_matter" if node_id.startswith("front") else "chapter"
        nodes.append({
            "id": node_id,
            "kind": kind,
            "order": 0,
            "title": {"source": "Front Matter" if kind == "front_matter" else "Document"},
            "source_span": {"segment_ids": []},
            "children": [],
            "artifact_refs": [],
            "visual_refs": [],
            "note_refs": [],
            "status": "mapped",
        })
        root_ids.append(node_id)
        # Front matter owns only leading non-heading blocks; the first heading starts
        # a new root rather than becoming a child of the synthetic front-matter node.
        stack.append((7, node_id))

    for segment in segments:
        if segment.kind == "heading":
            level = segment.heading_level or 1
            node_id = f"section-{len(nodes) + 1:04d}"
            kind = "chapter" if level == 1 else "section"
            while stack and stack[-1][0] >= level:
                stack.pop()
            node = {
                "id": node_id,
                "kind": kind,
                "order": len(nodes),
                "title": {"source": segment.title or "Untitled"},
                "source_span": {"segment_ids": []},
                "children": [],
                "artifact_refs": [],
                "visual_refs": [],
                "note_refs": [],
                "status": "mapped",
            }
            nodes.append(node)
            if stack:
                parent = next(item for item in nodes if item["id"] == stack[-1][1])
                parent["children"].append(node_id)
            else:
                root_ids.append(node_id)
            stack.append((level, node_id))
        current = stack[-1][1]
        assignments[segment.id] = current
        next(item for item in nodes if item["id"] == current)["source_span"]["segment_ids"].append(segment.id)

    assigned = [
        Segment(s.id, s.order, s.kind, s.text, s.heading_level, s.title, assignments[s.id])
        for s in segments
    ]
    return {
        "schema_version": 1,
        "source_fingerprint": source_fingerprint,
        "root_ids": root_ids,
        "nodes": nodes,
    }, assigned


def make_work_units(
    segments: list[Segment], target_chars: int, hard_max: int
) -> list[dict[str, Any]]:
    expanded = expand_long_segments(segments, hard_max)

    groups: list[list[Segment]] = []
    current: list[Segment] = []
    current_size = 0
    current_node: str | None = None
    for segment in expanded:
        size = len(segment.text) + 2
        node_changed = current_node is not None and segment.book_node_id != current_node
        heading_boundary = segment.kind == "heading" and current_size >= max(500, target_chars // 3)
        if current and (node_changed or heading_boundary or current_size + size > target_chars):
            groups.append(current)
            current = []
            current_size = 0
        current.append(segment)
        current_size += size
        current_node = segment.book_node_id
    if current:
        groups.append(current)

    return [
        {
            "id": f"unit-{index:04d}",
            "order": index - 1,
            "book_node_id": group[0].book_node_id,
            "segment_ids": [item.id for item in group],
            "text": "\n\n".join(item.text for item in group).rstrip() + "\n",
        }
        for index, group in enumerate(groups, 1)
    ]


def profile_markdown(markdown: str, source_format: str, source_bytes: int) -> dict[str, Any]:
    lines = markdown.splitlines()
    headings = sum(1 for line in lines if HEADING_RE.match(line))
    images = len(IMAGE_RE.findall(markdown))
    tables = sum(1 for line in lines if line.count("|") >= 2) // 2
    formulas = len(re.findall(r"\$[^$]+\$|\\\[[\s\S]*?\\\]", markdown))
    warnings: list[str] = []
    if source_format == "pdf":
        warnings.append("PDF text extraction is cross-checked against the V1.0 Page Map; review every page at or above the mode risk threshold.")
    if source_format == "chm":
        warnings.append("CHM topics are decompiled and merged in HHC order; page-level visual evidence is not applicable to HTML Help sources.")
    if not markdown.strip():
        warnings.append("Extraction produced no text.")
    return {
        "schema_version": 1,
        "source_format": source_format,
        "source_bytes": source_bytes,
        "text_characters": len(markdown),
        "line_count": len(lines),
        "heading_count": headings,
        "image_reference_count": images,
        "table_signal_count": tables,
        "formula_signal_count": formulas,
        "warnings": warnings,
    }

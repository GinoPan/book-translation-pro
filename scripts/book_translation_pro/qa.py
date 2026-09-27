"""Deterministic completeness, structure, terminology, asset, and visual QA."""

from __future__ import annotations

import json
import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, atomic_write_text, hash_data, load_json, portable_path, safe_project_path, sha256_file, validate_schema
from .exceptions import approved_exception_ids
from .glossary import load_effective_glossary, select_entries
from .manifest import load_manifest, validate_manifest_sources
from .state import load_state, now_utc, save_state
from .publication import (
    clean_markdown_for_publication,
    _replace_static_toc,
    _strip_print_artifacts,
    _strip_trailing_recovery_material,
    merge_edited_units,
)
from .structure import IMAGE_RE
from .figures import run_figure_audit
from .visual import run_visual_qa


NUMBER_RE = re.compile(
    r"(?<![a-zA-Z0-9_.])[-+]?\d+(?:[.,]\d+)*(?:\s?(?:%|°[CF]|mm|cm|km|kg|mg|MPa|kPa|Pa|Hz|kHz|MHz|V|A|W)(?![a-zA-Z]))?",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
URL_TRAILING_PUNCTUATION = ".,;:!?，。；：！？\"'”’"
FORMULA_RE = re.compile(r"(?<!\\)\$[^$\n]+\$|\\\[[\s\S]*?\\\]")
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
DATE_NAME_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
    re.IGNORECASE,
)
LOCALIZED_MONTH_RE = re.compile(r"(?<!\d)\d{1,2}\s*月")
SOURCE_DATE_CONTEXT_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b"
    r"\s+\d{1,2}(?:,\s*\d{4})?",
    re.IGNORECASE,
)
TARGET_DATE_CONTEXT_RE = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
)
FORMULA_SIGNAL_RE = re.compile(r"(?:=|∑|∫|√|±|≤|≥|×|→)")


def _check(checks: list[dict[str, Any]], check_id: str, status: str, message: str, unit_id: str | None = None) -> None:
    item: dict[str, Any] = {"id": check_id, "status": status, "message": message}
    if unit_id:
        item["unit_id"] = unit_id
    checks.append(item)


def _tokens(pattern: re.Pattern[str], text: str) -> Counter[str]:
    return Counter(match.group(0) for match in pattern.finditer(text))


def _urls(text: str) -> Counter[str]:
    """Compare URL payloads while ignoring adjacent prose punctuation."""
    # PDF/HTML extraction occasionally breaks a URL before a dotted suffix;
    # rejoin that harmless line-level split before tokenizing.
    text = re.sub(r"(?P<url>https?://[^\s<>)\]]+)\s+(?=\.)", r"\g<url>", text)
    tokens: Counter[str] = Counter()
    for match in URL_RE.finditer(text):
        token = match.group(0).rstrip(URL_TRAILING_PUNCTUATION)
        # Keep balanced parentheses used by legitimate URLs, but remove a
        # prose-closing delimiter that is not part of the URL itself.
        while token.endswith(")") and token.count(")") > token.count("("):
            token = token[:-1]
        while token.endswith("]") and token.count("]") > token.count("["):
            token = token[:-1]
        tokens[token] += 1
    return tokens


def _target_path(project: Path, entry: dict[str, Any], require_edit: bool) -> Path:
    return safe_project_path(project, entry["edit_file"] if require_edit else entry["draft_file"])


def _table_signals(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.count("|") >= 2)


def _code_fences(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith(("```", "~~~")))


def _comparison_text(markdown: str) -> str:
    """Remove extraction/layout wrappers before comparing content anchors."""
    lines = _strip_print_artifacts(markdown)
    lines = _replace_static_toc(lines)
    lines = _strip_trailing_recovery_material(lines)
    text = "\n".join(lines)
    text = HTML_IMAGE_RE.sub("", text)
    text = HTML_TAG_RE.sub("", text)
    return IMAGE_RE.sub("", text)


_INLINE_PRINT_STAMP_RE = re.compile(
    r"[A-Za-z0-9_.-]+\.(?:qxd|indd)\s+\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s+(?:AM|PM)(?:\s+Page\s+\S+)?",
    re.IGNORECASE,
)
_ENGLISH_MAGNITUDE_RE = re.compile(
    r"(?P<num>[-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?P<mag>trillion|billion|million|thousand)\b",
    re.IGNORECASE,
)
_CJK_MAGNITUDE_RE = re.compile(
    r"(?P<num>[-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?P<mag>亿万|亿|万)"
)
_DECADE_RE = re.compile(r"\b(1[89]\d{2})s\b")
_ORDINAL_WORD_RE = re.compile(
    r"\b(ninetieth|eightieth|seventieth|sixtieth|fiftieth|fortieth|thirtieth|twentieth|tenth|ninth|eighth|seventh|sixth|fifth|fourth|third)\b",
    re.IGNORECASE,
)
_ORDINAL_WORD_VALUES = {
    "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "twentieth": 20, "thirtieth": 30,
    "fortieth": 40, "fiftieth": 50, "sixtieth": 60, "seventieth": 70,
    "eightieth": 80, "ninetieth": 90,
}
_SMALL_INTEGER_RE = re.compile(r"^\d{1,2}$")
_MAGNITUDE_FACTORS = {
    "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000, "万": 10_000, "亿": 100_000_000,
    "亿万": 100_000_000,
}


def _canonical_number(value: str) -> str:
    number = float(value)
    if number == int(number):
        return str(int(number))
    return repr(number)


def _canonicalize_numbers(text: str) -> str:
    """Normalize locale-specific number spellings before conservation checks.

    A faithful Chinese edition legitimately rewrites "10 million" as
    "1,000万", "15 percent" as "15%", and "the 1990s" as "20 世纪 90 年代".
    Canonicalizing those keeps the conservation check aimed at real numeric
    errors instead of standard localization.  Small spelled-out counts are
    handled as a warning downgrade in ``_variation_status`` because either
    language may spell them.
    """
    text = _INLINE_PRINT_STAMP_RE.sub(" ", text)
    text = _DECADE_RE.sub(lambda match: f"20 {match.group(1)[2:]} ", text)
    text = _ORDINAL_WORD_RE.sub(
        lambda match: str(_ORDINAL_WORD_VALUES[match.group(0).casefold()]),
        text,
    )
    text = _ENGLISH_MAGNITUDE_RE.sub(
        lambda match: _canonical_number(
            str(float(match.group("num").replace(",", "")) * _MAGNITUDE_FACTORS[match.group("mag").casefold()])
        ),
        text,
    )
    text = _CJK_MAGNITUDE_RE.sub(
        lambda match: _canonical_number(
            str(float(match.group("num").replace(",", "")) * _MAGNITUDE_FACTORS[match.group("mag")])
        ),
        text,
    )
    # Thousands separators are typography, not value: "$100,000" and the
    # CJK "10 万" canonicalize to the same token.
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    return text


def _number_tokens(markdown: str) -> Counter[str]:
    text = _comparison_text(markdown)
    # Written ``percent`` and the symbol are equivalent, including when a
    # line-break hyphen split ``per-cent``.  Do not collapse
    # ``percentage point(s)``: that is a different unit and must remain
    # visible to the conservation check.
    text = re.sub(
        r"(?<![a-zA-Z0-9_.])([-+]?\d+(?:[.,]\d+)*)[ \t]*(?:percent|per[-\t \n]*cent)\b",
        r"\1%",
        text,
        flags=re.IGNORECASE,
    )
    text = _canonicalize_numbers(text)
    tokens = Counter()
    for match in NUMBER_RE.finditer(text):
        tokens[match.group(0)] += 1
    return tokens


def _meaningful_image_refs(project: Path, markdown: str) -> Counter[str]:
    """Exclude tiny extraction fragments while retaining real visual assets."""
    refs = Counter(IMAGE_RE.findall(markdown))
    try:
        from PIL import Image
    except ImportError:
        return refs
    meaningful: Counter[str] = Counter()
    for ref, count in refs.items():
        resource = safe_project_path(project, ref.replace("\\", "/"))
        try:
            with Image.open(resource) as image:
                if min(image.size) < 32:
                    continue
        except (OSError, ValueError):
            # Missing resources remain visible to the resource-link check.
            meaningful[ref] += count
            continue
        meaningful[ref] += count
    return meaningful


def _formula_signals(markdown: str) -> int:
    """Count equation-like lines, excluding ordinary currency amounts."""
    text = _comparison_text(markdown)
    return sum(1 for line in text.splitlines() if FORMULA_SIGNAL_RE.search(line))


def _localized_date_variation(source: str, target: str) -> bool:
    return bool(SOURCE_DATE_CONTEXT_RE.search(source) and TARGET_DATE_CONTEXT_RE.search(target))


def _measurement_units(markdown: str) -> Counter[str]:
    units: Counter[str] = Counter()
    for token in _number_tokens(markdown):
        match = re.search(r"(?:%|°[CF]|mm|cm|km|kg|mg|MPa|kPa|Pa|Hz|kHz|MHz|V|A|W)$", token, re.IGNORECASE)
        if match:
            units[match.group(0).casefold()] += 1
    return units


def _compact_counter(counter: Counter[str], limit: int = 6) -> str:
    items = list(counter.items())[:limit]
    return ", ".join(f"{key}×{value}" for key, value in items) or "none"


def _variation_status(
    check_id: str,
    unit_id: str,
    source: str,
    target: str,
    observation: dict[str, Any],
) -> tuple[str, str]:
    """Classify unit-local differences without masking high-confidence edits."""
    source_issues = observation.get("source_issues", [])
    ratio = min(len(source), len(target)) / max(1, max(len(source), len(target)))
    if check_id == "content.numbers":
        source_numbers = _number_tokens(source)
        target_numbers = _number_tokens(target)
        if _localized_date_variation(source, target):
            return "warn", "localized date notation"
        if source_issues:
            evidence = ", ".join(source_issues[:4])
            return "warn", f"source/target numeric anchors differ after cleanup; evidence: {evidence}"
        if (
            _table_signals(target) > _table_signals(source)
            and not source_numbers - target_numbers
        ):
            return "warn", "source/target numeric anchors differ after cleanup; evidence: table reconstruction"
        # Keep a direct measurement change blocking even when a short target
        # translation is much more compact than the extracted source unit.
        if (
            _measurement_units(source) == _measurement_units(target)
            and _measurement_units(source)
        ):
            return "fail", f"Numbers or units changed (source-only: {_compact_counter(source_numbers - target_numbers)}; target-only: {_compact_counter(target_numbers - source_numbers)})"
        if ratio < 0.65:
            return "warn", "source/target numeric anchors differ after cleanup; evidence: unit reflow/reconstruction"
        # Small bare counts (2 products, 6 operations, "five times") are
        # spelled in either language by style; only their measurements are
        # conserved word-for-word.  Everything else stays blocking so an
        # ordinary 10 mm -> 11 mm edit remains detectable.
        differing = (source_numbers - target_numbers) | (target_numbers - source_numbers)
        if differing and all(_SMALL_INTEGER_RE.match(token) for token in differing):
            return "warn", "small-count numerals spelled differently between languages"
        return "fail", f"Numbers or units changed (source-only: {_compact_counter(source_numbers - target_numbers)}; target-only: {_compact_counter(target_numbers - source_numbers)})"
    if check_id == "content.formulas":
        if source_issues or ratio < 0.65:
            evidence = ", ".join(source_issues[:4]) or "unit reflow/reconstruction"
            return "warn", f"formula-like layout differs after cleanup; evidence: {evidence}"
        return "fail", "Formula structure changed"
    return "warn", "unit-local structure was reflowed or reconstructed; book-level inventory is authoritative"


def _heading_keys(markdown: str) -> set[str]:
    keys: set[str] = set()
    cleaned = clean_markdown_for_publication(markdown)
    for line in cleaned.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if not match:
            continue
        text = HTML_TAG_RE.sub("", match.group(1))
        text = re.sub(r"[*_`]", "", text).strip()
        number = re.match(r"(\d+(?:\.\d+)*)(?=[\s　]|$)", text)
        if number:
            keys.add(number.group(1))
            continue
        summary = re.match(r"第\s*(\d+)\s*章内容提要", text)
        if summary:
            keys.add(f"summary:{summary.group(1)}")
    return keys


def _caption_keys(markdown: str) -> set[str]:
    cleaned = clean_markdown_for_publication(markdown)
    source_keys = re.findall(r"(?i)\b(?:exhibit|figure|table)\s*([0-9]+(?:[.]\d+)*[A-Z]?)", cleaned)
    target_keys = re.findall(r"(?:图表|图|表)\s*([0-9]+(?:[.]\d+)*[A-Z]?)", cleaned)
    return set(source_keys or target_keys)


def _html_image_sources(markdown: str) -> set[str]:
    """Return local ``src`` values of raw ``<img>`` tags left by extraction."""
    sources: set[str] = set()
    for match in HTML_IMAGE_RE.finditer(markdown):
        src = re.search(r"""src\s*=\s*["']([^"']+)["']""", match.group(0), re.IGNORECASE)
        if src:
            sources.add(src.group(1).replace("\\", "/"))
    return sources


def _check_local_resources(project: Path, refs: list[str], checks: list[dict[str, Any]], unit_id: str) -> None:
    for ref in refs:
        if re.match(r"^(?:https?:|data:)", ref, re.IGNORECASE):
            continue
        try:
            resource = safe_project_path(project, ref)
        except BTPError as exc:
            _check(checks, "assets.path", "fail", str(exc), unit_id)
            continue
        if not resource.is_file():
            _check(checks, "assets.missing", "fail", f"Missing local resource: {ref}", unit_id)


def run_qa(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    manifest = load_manifest(project)
    state = load_state(project)
    glossary = load_effective_glossary(project, config)
    require_edit = bool(config["passes"]["edit"])
    checks: list[dict[str, Any]] = []

    if hash_data(config) != manifest["resolved_config_hash"]:
        _check(checks, "config.hash", "fail", "Resolved configuration differs from the prepared manifest")

    for error in validate_manifest_sources(project, manifest):
        _check(checks, "manifest.source", "fail", error)

    expected_files: set[str] = set()
    merged_sources: list[str] = []
    merged_targets: list[str] = []
    ordered_units = sorted(manifest["units"], key=lambda item: item["order"])
    unit_position = {entry["unit_id"]: index for index, entry in enumerate(ordered_units)}
    target_tokens_by_unit: dict[str, Counter[str]] = {}
    source_tokens_by_unit: dict[str, Counter[str]] = {}
    for entry in ordered_units:
        source_file = safe_project_path(project, entry["source_file"])
        target_file = _target_path(project, entry, require_edit)
        if source_file.is_file():
            source_tokens_by_unit[entry["unit_id"]] = _number_tokens(source_file.read_text(encoding="utf-8"))
        if target_file.is_file():
            target_tokens_by_unit[entry["unit_id"]] = _number_tokens(target_file.read_text(encoding="utf-8"))
    for entry in ordered_units:
        unit_id = entry["unit_id"]
        source_path = safe_project_path(project, entry["source_file"])
        target_path = _target_path(project, entry, require_edit)
        expected_files.add(portable_path(target_path, project))
        if not target_path.is_file() or not target_path.read_text(encoding="utf-8").strip():
            _check(checks, "unit.output", "fail", f"Missing or blank {'edited' if require_edit else 'draft'} output", unit_id)
            continue
        source = source_path.read_text(encoding="utf-8")
        target = target_path.read_text(encoding="utf-8")
        merged_sources.append(source)
        merged_targets.append(target)
        record = state["units"].get(unit_id, {})
        output_kind = "edit" if require_edit else "draft"
        if record.get("output_hashes", {}).get(output_kind) != sha256_file(target_path):
            _check(checks, "unit.unrecorded_change", "fail", "Target output differs from its recorded hash", unit_id)

        capsule_path = safe_project_path(project, entry["capsule_file"])
        try:
            capsule = load_json(capsule_path)
            validate_schema(capsule, "context-capsule.schema.json")
            if not capsule["summary"].strip():
                _check(checks, "capsule.blank", "fail", "Context Capsule summary is blank", unit_id)
        except BTPError as exc:
            _check(checks, "capsule.invalid", "fail", str(exc), unit_id)

        observation: dict[str, Any] = {
            "schema_version": 1,
            "new_entries": [],
            "conflicts": [],
            "source_issues": [],
            "used_glossary_ids": [],
        }
        observation_path = safe_project_path(project, entry["observation_file"])
        try:
            observation = load_json(observation_path)
            if observation.get("schema_version") != 1 or any(
                not isinstance(observation.get(field), list)
                for field in ("new_entries", "conflicts", "source_issues", "used_glossary_ids")
            ):
                raise BTPError("Observation must use schema_version 1 and the four array fields")
        except BTPError as exc:
            _check(checks, "observation.invalid", "fail", str(exc), unit_id)

        source_images = _meaningful_image_refs(project, source)
        target_images = _meaningful_image_refs(project, target)
        if source_images != target_images:
            if "source_cover_rebuilt" in observation.get("source_issues", []):
                _check(checks, "assets.image_refs", "warn", "Cover artwork was intentionally rebuilt for the target edition", unit_id)
            elif any(str(issue).startswith("source_figure_reconstructed") for issue in observation.get("source_issues", [])):
                _check(checks, "assets.image_refs", "warn", "Figure asset was reconstructed from the authoritative source page", unit_id)
            elif not set(target_images - source_images) - _html_image_sources(source):
                _check(checks, "assets.image_refs", "warn", "Extraction-embedded HTML image promoted to a Markdown asset reference", unit_id)
            else:
                _check(checks, "assets.image_refs", "fail", "Image references changed", unit_id)
        _check_local_resources(project, list(source_images), checks, unit_id)

        if _table_signals(source) != _table_signals(target):
            status, message = _variation_status("structure.tables", unit_id, source, target, observation)
            _check(checks, "structure.tables", status, message, unit_id)
        if _formula_signals(source) != _formula_signals(target):
            status, message = _variation_status("content.formulas", unit_id, source, target, observation)
            _check(checks, "content.formulas", status, message, unit_id)
        if _code_fences(source) != _code_fences(target):
            _check(checks, "structure.code_fences", "fail", "Code fence count changed", unit_id)

        source_numbers = _number_tokens(source)
        target_numbers = _number_tokens(target)
        if source_numbers != target_numbers:
            status, message = _variation_status("content.numbers", unit_id, source, target, observation)
            if status == "fail":
                # Exhibit reconstructions can move numeric anchors (a table
                # rebuilt beside its caption) into the adjacent unit.  When
                # every unmatched anchor on one side is present in the
                # neighbour on the other side, the numbers are conserved and
                # the boundary artifact is only a warning.
                moved_source = source_numbers - target_numbers
                moved_target = target_numbers - source_numbers
                unit_index = unit_position.get(unit_id)
                neighbor_tokens: Counter[str] = Counter()
                for offset in (-1, 1):
                    neighbor_id = (
                        ordered_units[unit_index + offset]["unit_id"]
                        if unit_index is not None and 0 <= unit_index + offset < len(ordered_units)
                        else None
                    )
                    neighbor_tokens.update(target_tokens_by_unit.get(neighbor_id, Counter()))
                    neighbor_tokens.update(source_tokens_by_unit.get(neighbor_id, Counter()))
                if moved_source and not moved_source - neighbor_tokens:
                    status, message = "warn", "Numeric anchors moved to an adjacent unit during exhibit reconstruction"
                elif moved_target and not moved_target - neighbor_tokens:
                    status, message = "warn", "Numeric anchors moved to an adjacent unit during exhibit reconstruction"
            _check(checks, "content.numbers", status, message, unit_id)
        if _urls(_comparison_text(source)) != _urls(_comparison_text(target)):
            _check(checks, "content.urls", "fail", "URLs changed", unit_id)

        for term in select_entries(glossary, source, entry["book_node_id"]):
            if term["target"] not in target:
                _check(checks, "terminology.approved", "fail", f"Approved target missing for {term['source']!r}: {term['target']!r}", unit_id)

        draft_path = safe_project_path(project, entry["draft_file"])
        if require_edit and draft_path.is_file():
            recorded_input = record.get("output_hashes", {}).get("edit_input_draft")
            if recorded_input != sha256_file(draft_path):
                _check(checks, "edit.stale", "fail", "Edited output was not recorded against the current draft", unit_id)

    target_root = project / "target" / ("edited" if require_edit else "draft")
    actual_files = {portable_path(path, project): path for path in target_root.glob("unit-*.md")}
    for orphan_ref in sorted(set(actual_files) - expected_files):
        _check(checks, "unit.orphan", "fail", f"Orphan output: {actual_files[orphan_ref].name}")

    book_map = load_json(project / "analysis" / "book-map.json")
    mapped_segments = [segment_id for node in book_map["nodes"] for segment_id in node["source_span"]["segment_ids"]]
    source_segments = []
    for line in (project / "source-work" / "segments.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            source_segments.append(json.loads(line)["id"])
    if Counter(mapped_segments) != Counter(source_segments):
        _check(checks, "book_map.coverage", "fail", "Book Map does not cover source segments exactly once")

    # Work units are extraction/editing boundaries, not immutable book
    # boundaries. Validate headings, captions, and URLs at book level so a
    # reconstructed table or a paragraph moved across an adjacent unit is not
    # reported as missing content.
    merged_source = "\n\n".join(merged_sources)
    merged_target = "\n\n".join(merged_targets)
    source_heading_keys = _heading_keys(merged_source)
    target_heading_keys = _heading_keys(merged_target)
    missing_headings = sorted(source_heading_keys - target_heading_keys)
    if missing_headings:
        _check(checks, "structure.heading_inventory", "fail", f"Numbered headings missing from target: {', '.join(missing_headings)}")
    else:
        _check(checks, "structure.heading_inventory", "pass", f"Target contains all {len(source_heading_keys)} source numbered heading keys")

    source_caption_keys = _caption_keys(merged_source)
    target_caption_keys = _caption_keys(merged_target)
    missing_captions = sorted(source_caption_keys - target_caption_keys)
    if missing_captions:
        _check(checks, "structure.caption_inventory", "fail", f"Figure/table caption keys missing from target: {', '.join(missing_captions)}")
    else:
        _check(checks, "structure.caption_inventory", "pass", f"Target contains all {len(source_caption_keys)} source figure/table caption keys")

    # A source PDF can contain vector-only exhibits.  They have no image
    # reference in the extracted Markdown, so the ordinary source/target image
    # comparison cannot detect their disappearance.  Audit the source PDF's
    # caption inventory directly and require either a target asset or an
    # explicit structured-reconstruction observation.
    figure_audit = run_figure_audit(project, config)
    for item in figure_audit.get("figures", []):
        _check(
            checks,
            "assets.figure_inventory",
            item["status"],
            item["message"],
            item.get("target_units", [None])[0] if item.get("target_units") else None,
        )

    source_book_urls = _urls(_comparison_text(merged_source))
    target_book_urls = _urls(_comparison_text(merged_target))
    if source_book_urls != target_book_urls:
        _check(checks, "content.url_inventory", "fail", f"Book-level URLs changed: source={_compact_counter(source_book_urls)} target={_compact_counter(target_book_urls)}")
    else:
        _check(checks, "content.url_inventory", "pass", f"Book-level URL inventory preserved ({sum(source_book_urls.values())} URLs)")

    visual_report = run_visual_qa(project, config)
    for item in visual_report["checks"]:
        suffix = ""
        if item.get("page_index") is not None:
            suffix += f" [page {item['page_index']}]"
        if item.get("region_id"):
            suffix += f" [region {item['region_id']}]"
        _check(checks, item["id"], item["status"], item["message"] + suffix)
    if config["mode"] == "publication":
        signoff = next(
            (item for item in state.get("human_reviews", []) if item["checkpoint"] == "publication_signoff"),
            None,
        )
        if not signoff or signoff.get("status") != "approved":
            _check(checks, "publication.signoff", "fail", "Publication signoff is required")
        else:
            from .publication_review import verify_signoff_evidence

            for error in verify_signoff_evidence(project, signoff):
                _check(checks, "publication.signoff_stale", "fail", error)
        candidate_path = project / "qa" / "typeset-preview.json"
        if not candidate_path.is_file():
            _check(checks, "publication.candidate", "fail", "A current typeset review candidate is required before publication QA")
        else:
            try:
                candidate = load_json(candidate_path)
                current_clean = clean_markdown_for_publication(merge_edited_units(project, config))
                current_hash = hashlib.sha256(current_clean.encode("utf-8")).hexdigest()
                if candidate.get("status") != "review_candidate":
                    _check(checks, "publication.candidate", "fail", "Typeset candidate is not marked as a review candidate")
                elif candidate.get("clean_master_sha256") != current_hash:
                    _check(checks, "publication.candidate_stale", "fail", "Typeset candidate does not match the current edited content")
                elif not candidate.get("publication_validation", {}).get("passed"):
                    _check(checks, "publication.format_validation", "fail", "Typeset candidate has not passed format validation")
                else:
                    _check(checks, "publication.format_validation", "pass", "Typeset candidate passed format validation")
                    for warning in candidate.get("publication_validation", {}).get("warnings", []):
                        _check(checks, "publication.format_warning", "warn", warning)
            except BTPError as exc:
                _check(checks, "publication.candidate", "fail", f"Typeset candidate is invalid: {exc}")

    pending_reviews = [
        item["checkpoint"]
        for item in state.get("human_reviews", [])
        if item["status"] not in {"approved", "not_required"}
    ]
    for checkpoint in pending_reviews:
        _check(checks, "checkpoint.pending", "fail", f"Human checkpoint pending: {checkpoint}")

    warnings = [item for item in checks if item["status"] == "warn"]
    if config["mode"] == "publication":
        unapproved_warning_ids = sorted({
            item["id"] for item in warnings
            if not approved_exception_ids(project, item["id"], item.get("unit_id", "publication"))
        })
        if unapproved_warning_ids:
            _check(
                checks,
                "publication.unapproved_warnings",
                "fail",
                "Publication warnings require approved exceptions: " + ", ".join(unapproved_warning_ids),
            )
    failures = [item for item in checks if item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] == "warn"]
    score = max(0, 100 - (20 * len(failures)) - (2 * len(warnings)))
    unit_metrics = [record.get("metrics", {}) for record in state["units"].values()]
    metrics = {
        "attempts": sum(int(record.get("attempts", 0)) for record in state["units"].values()),
        "duration_seconds": sum(float(item.get("duration_seconds", 0)) for item in unit_metrics),
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in unit_metrics),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in unit_metrics),
        "costs_by_currency": {},
    }
    for item in unit_metrics:
        if item.get("cost") is None:
            continue
        currency = str(item.get("currency", "USD")).upper()
        metrics["costs_by_currency"][currency] = (
            metrics["costs_by_currency"].get(currency, 0) + float(item["cost"])
        )
    report = {
        "schema_version": 1,
        "passed": not failures,
        "checked_at": now_utc(),
        "mode": config["mode"],
        "unit_count": len(manifest["units"]),
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "score": score,
        "score_is_advisory": True,
        "metrics": metrics,
        "approved_exception_ids": approved_exception_ids(project),
        "checks": checks,
        "normalization": {
            "unit_local": "print markers, HTML wrappers, image payloads, and extraction-only TOC material are excluded from anchor comparisons",
            "book_level": {
                "source_numbered_heading_keys": sorted(source_heading_keys),
                "target_numbered_heading_keys": sorted(target_heading_keys),
                "source_caption_keys": sorted(source_caption_keys),
                "target_caption_keys": sorted(target_caption_keys),
            },
        },
        "figure_audit": {
            "path": "qa/figure-audit.json",
            "figure_count": figure_audit.get("figure_count", 0),
            "failure_count": figure_audit.get("failure_count", 0),
            "warning_count": figure_audit.get("warning_count", 0),
        },
    }
    atomic_write_json(project / "qa" / "completeness.json", report)
    lines = [
        "# Completeness QA",
        "",
        f"Result: {'PASS' if report['passed'] else 'FAIL'}",
        f"Units: {report['unit_count']}",
        f"Failures: {report['failure_count']}",
        f"Warnings: {report['warning_count']}",
        "",
        "| Status | Check | Unit | Message |",
        "|---|---|---|---|",
    ]
    for item in checks:
        lines.append(f"| {item['status']} | {item['id']} | {item.get('unit_id', '')} | {item['message'].replace('|', '/')} |")
    atomic_write_text(project / "qa" / "completeness.md", "\n".join(lines) + "\n")

    stage = state["stages"]["qa"]
    stage.update({
        "status": "completed" if report["passed"] else "failed_terminal",
        "attempts": stage["attempts"] + 1,
        "dependency_hashes": {"manifest": sha256_file(project / "state" / "manifest.json")},
        "output_hashes": {"report": sha256_file(project / "qa" / "completeness.json")},
        "reason_codes": [item["id"] for item in failures],
        "updated_at": now_utc(),
    })
    visual_stage = state["stages"].setdefault("visual", {
        "status": "pending", "attempts": 0, "dependency_hashes": {}, "output_hashes": {}, "reason_codes": [], "updated_at": now_utc()
    })
    visual_failures = [item for item in visual_report["checks"] if item["status"] == "fail"]
    visual_stage.update({
        "status": "completed" if visual_report["passed"] else "failed_terminal",
        "attempts": visual_stage.get("attempts", 0) + 1,
        "output_hashes": {"report": sha256_file(project / "qa" / "visual.json")},
        "reason_codes": [item["id"] for item in visual_failures],
        "updated_at": now_utc(),
    })
    save_state(project, state)
    return report

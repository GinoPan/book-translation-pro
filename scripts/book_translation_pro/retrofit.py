"""Retrofit exhibit treatment into the source units of an already prepared PDF project.

For every exhibit the inventory can treat (a diagram with labels, a rebuilt
table), the extraction fragments around its caption are replaced by the
exhibit itself: the source crop plus a figure-label block, or the rebuilt
table.  Fragments are recognised by their words only: a line is removed when
every word on it is one of the exhibit's own (each source word used once), so
neighbouring prose, which never consists of label words alone, stops the walk.

Dry run by default.  ``apply`` writes the changed units, a removal log, and the
new source hashes into the manifest so ``plan`` re-queues exactly those units.
Exhibits whose fragments cannot be accounted for are left untouched and listed.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, atomic_write_text, safe_project_path, sha256_file, validate_schema
from .config import load_resolved_project
from .exhibits import build_inventory
from .figures import PRINT_LAYOUT_LINE_RE, SOURCE_CAPTION_RE, _caption_key, _figure_asset_name
from .manifest import load_manifest
from .pdf_text import _pipe_table
from .state import now_utc

TREATED = {"diagram", "table_grid", "table_lists"}
# Share of the exhibit's words that must be found as fragments before the
# fragments are replaced; below it the extraction is not understood well enough.
MIN_COVERAGE = 0.9
# Share of one- and two-letter tokens that marks a line as scrambled sideways text.
SCRAMBLED_SHARE = 0.5
WORD_RE = re.compile(r"[^\W_]+")
# A running head or folio: a page number at either end of a short line.
FOLIO_RE = re.compile(r"^(?:\d{1,4}|\d{1,4}\s+\D{1,60}|\D{1,60}\s+\d{1,4})$")
MARKER_RE = re.compile(r'<!-- btp:(?:figure-labels|exhibit-table) exhibit="([^"]+)"')
BLOCK_RE = re.compile(
    r'<!-- btp:(?P<kind>figure-labels|exhibit-table) exhibit="(?P<key>[^"]+)" -->\n(?P<body>.*?)\n?'
    r"<!-- /btp:(?P=kind) -->",
    re.DOTALL,
)
LABEL_LINE_RE = re.compile(r"^(\d+)\.\s+(.*\S)\s*$")


def exhibit_blocks(text: str) -> list[dict[str, Any]]:
    """Figure-label blocks and rebuilt tables in a unit, source or translated.

    A label block yields its numbered labels in order; a table block its body.
    """
    blocks = []
    for match in BLOCK_RE.finditer(text):
        block: dict[str, Any] = {"kind": match["kind"], "key": match["key"], "body": match["body"],
                                 "span": match.span()}
        if match["kind"] == "figure-labels":
            block["labels"] = [m[2] for line in match["body"].splitlines() if (m := LABEL_LINE_RE.match(line))]
            block["numbers"] = [int(m[1]) for line in match["body"].splitlines() if (m := LABEL_LINE_RE.match(line))]
        blocks.append(block)
    return blocks


def _plain(line: str) -> str:
    # Calibre splits words across spans ("Revenue</span><span>s"): tags join, not separate.
    value = re.sub(r"<[^>]+>", "", line)
    value = re.sub(r"\\(.)", r"\1", value)
    value = re.sub(r"[`*_]+", "", value)
    return " ".join(value.split())


def _words(text: str) -> Counter[str]:
    return Counter(word.casefold() for word in WORD_RE.findall(text))


def _exhibit_texts(exhibit: dict[str, Any]) -> list[str]:
    """Every text the exhibit prints (labels, or table header, meta, and cells)."""
    if exhibit["classification"] == "diagram":
        return [label["text"] for label in exhibit["labels"]]
    table = exhibit["table"]
    texts = list(table["header"] or []) + [c for row in table["meta"] for c in row]
    return texts + [part for row in table["rows"] for cell in row for part in cell.split("<br>")]


def _caption_title(exhibit: dict[str, Any]) -> str:
    return _label_key_re(exhibit).sub("", PRINT_LAYOUT_LINE_RE.sub("", exhibit["caption"]), count=1).strip(" .:")


def _letters(text: str) -> Counter[str]:
    return Counter(char for char in text if char.isalnum())


def _is_caption_of(plain: str, exhibit: dict[str, Any]) -> bool:
    match = SOURCE_CAPTION_RE.match(plain)
    if not match or _caption_key(match.group(2)) != exhibit["key"]:
        return False
    # The inventory caption names this part ("EXHIBIT 6.6 Continued" vs the first page).
    caption = PRINT_LAYOUT_LINE_RE.sub("", exhibit["caption"])
    expected, actual = list(_words(caption).elements()), _words(PRINT_LAYOUT_LINE_RE.sub("", plain))
    return not (Counter(expected) - actual)


@lru_cache(maxsize=None)
def _label_key(label: str, key: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.]){re.escape(label)}\s+{re.escape(key)}(?![\w]|\.\d)")


def _label_key_re(exhibit: dict[str, Any]) -> re.Pattern[str]:
    """The printed label and number, in the label's own case ("EXHIBIT 6.6", not a
    cross-reference "Exhibit 6.6"), anywhere on the line."""
    return _label_key(exhibit["label"], exhibit["key"])


def _is_scrambled(plain: str) -> bool:
    """Text of a sideways exhibit comes out as letter runs ("t s s t u st y yi"); prose never does."""
    tokens = [token.strip(".,;:()") for token in plain.split()]
    tokens = [token for token in tokens if token]
    return bool(tokens) and sum(len(token) <= 2 for token in tokens) >= SCRAMBLED_SHARE * len(tokens)


def _is_furniture(plain: str) -> bool:
    rest = PRINT_LAYOUT_LINE_RE.sub("", plain).strip()
    return not rest or bool(FOLIO_RE.match(rest))


def _stops_walk(line: str, plain: str, exhibits: list[dict[str, Any]]) -> bool:
    """Structure, or the caption of an exhibit in the inventory.  A label inside a
    figure that cites another exhibit ("Exhibit 6.8 ...") is not a caption."""
    stripped = line.lstrip()
    if stripped.startswith(("#", "![", "<img", "|", "<!--")):
        return True
    return any(_is_caption_of(plain, other) or _label_key_re(other).search(plain) for other in exhibits)


def _block(exhibit: dict[str, Any]) -> list[str]:
    """The Markdown that stands in for the exhibit in the source unit."""
    key = exhibit["key"]
    if exhibit["classification"] == "diagram":
        lines = [f"![](assets/original/images/{_figure_asset_name(key)})", "",
                 f'<!-- btp:figure-labels exhibit="{key}" -->']
        lines += [f"{number}. {label['text']}" for number, label in enumerate(exhibit["labels"], 1)]
        return lines + ["<!-- /btp:figure-labels -->"]
    table = exhibit["table"]

    def escaped(row: list[str]) -> list[str]:
        return [cell.replace("$", "\\$") for cell in row]

    rows = ([escaped(table["header"])] if table["header"] else []) + [escaped(row) for row in table["rows"]]
    lines = [f'<!-- btp:exhibit-table exhibit="{key}" -->', ""]
    for meta in table["meta"]:
        lines += [" ".join(escaped(meta)), ""]
    lines += _pipe_table(rows, header=bool(table["header"])).splitlines() + [""]
    if table["continues"]:
        lines += ["(continues)", ""]
    return lines + ["<!-- /btp:exhibit-table -->"]


def _find_caption(units: list[dict[str, Any]], exhibit: dict[str, Any],
                  exhibits: list[dict[str, Any]]) -> tuple[tuple[int, int], bool] | None:
    """The caption line, and whether it came out scrambled (a sideways exhibit)."""
    hits = [
        (u, i) for u, unit in enumerate(units) for i, line in enumerate(unit["lines"])
        if _is_caption_of(_plain(line), exhibit)
    ]
    # The caption words tell parts apart ("Continued"); a repeated identical
    # caption is ambiguous and left for review.
    if len(hits) == 1:
        return hits[0], False
    if hits:
        return None
    # A sideways exhibit keeps only its label and number legible, with the title
    # scrambled into the text around it.  Parts are told apart by order, and only
    # when the book prints the label exactly once per part.
    pattern = _label_key_re(exhibit)
    loose = [
        (u, i) for u, unit in enumerate(units) for i, line in enumerate(unit["lines"])
        if pattern.search(_plain(line)) and _is_scrambled(pattern.sub("", _plain(line)))
    ]
    parts = sorted((other["page"], other is exhibit) for other in exhibits
                   if other["key"] == exhibit["key"] and other["label"] == exhibit["label"])
    if len(loose) != len(parts):
        return None
    return loose[[is_this for _page, is_this in parts].index(True)], True


def _fragment(line: str, plain: str, u: int, i: int, size: int) -> dict[str, Any]:
    # Stamps are kept verbatim (emphasis stripping would eat "ch07_4772").
    raw = re.sub(r"\\(.)", r"\1", re.sub(r"<[^>]+>", "", line))
    return {"unit": u, "line": i, "text": line, "size": size,
            "keep": [match.group(0) for match in PRINT_LAYOUT_LINE_RE.finditer(raw)]}


def _walk(units: list[dict[str, Any]], start: tuple[int, int], step: int, pool: Counter[str],
          words: Counter[str], claimed: set[tuple[int, int]],
          exhibits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Fragments next to the caption in one direction, and the line the walk stopped at.

    Every letter of a fragment comes out of the exhibit's letter ``pool`` (each
    used once); a line that reads as words must also use only the exhibit's
    ``words``, so prose, which always brings words of its own, stops the walk."""
    found: list[dict[str, Any]] = []
    u, i = start
    first_unit = start[0]
    while True:
        i += step
        if i < 0 or i >= len(units[u]["lines"]):
            u += step
            if u < 0 or u >= len(units) or abs(u - first_unit) > 1:
                return found, "<unit boundary>"
            i = 0 if step > 0 else len(units[u]["lines"]) - 1
        line = units[u]["lines"][i]
        plain = _plain(line)
        if not plain:
            continue
        if (u, i) in claimed or _stops_walk(line, plain, exhibits):
            return found, plain[:120]
        if _is_furniture(plain):
            continue
        text = PRINT_LAYOUT_LINE_RE.sub("", plain)
        letters = _letters(text)
        if not letters or letters - pool:
            return found, plain[:120]
        # Whole words must be the exhibit's; a line broken into letter runs can
        # only be checked letter by letter.
        if not _is_scrambled(text) and _words(text) - words:
            return found, plain[:120]
        pool.subtract(letters)
        words.subtract(_words(text) & words)
        found.append(_fragment(line, plain, u, i, sum(letters.values())))


def _load_units(project: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    units = []
    for entry in sorted(manifest["units"], key=lambda item: item["order"]):
        path = safe_project_path(project, entry["source_file"])
        text = path.read_text(encoding="utf-8")
        units.append({"id": entry["unit_id"], "path": path, "entry": entry, "text": text, "lines": text.split("\n")})
    return units


def plan_retrofit(project: Path, units: list[dict[str, Any]], inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Decide, per exhibit, which source lines go and what replaces them (no writes).

    ``units`` are the source units in reading order, each with ``id``, ``text``, and ``lines``.
    """
    already = {key for unit in units for key in MARKER_RE.findall(unit["text"])}
    claimed: set[tuple[int, int]] = set()
    decisions: list[dict[str, Any]] = []
    exhibits = inventory["exhibits"]
    for exhibit in exhibits:
        decision: dict[str, Any] = {"key": exhibit["key"], "part": exhibit["part"], "page": exhibit["page"],
                                    "classification": exhibit["classification"]}
        decisions.append(decision)
        if exhibit["classification"] not in TREATED:
            decision.update(status="untouched", reason=exhibit["reason"])
            continue
        if exhibit["key"] in already:
            decision.update(status="already_retrofitted")
            continue
        found_caption = _find_caption(units, exhibit, exhibits)
        if found_caption is None:
            decision.update(status="unmatched", reason="caption not found once in the source units")
            continue
        caption, scrambled = found_caption
        replacement_caption: list[str] | None = None
        texts = _exhibit_texts(exhibit) + ([_caption_title(exhibit)] if scrambled else [])
        total = _letters(" ".join(texts))
        pool = Counter(total)
        words = _words(" ".join(texts))
        head = None
        if scrambled:
            # The title was scrambled into the text around the label: rebuild the caption.
            line = units[caption[0]]["lines"][caption[1]]
            plain = _plain(line)
            rest = _letters(PRINT_LAYOUT_LINE_RE.sub("", _label_key_re(exhibit).sub("", plain, count=1)))
            if rest - pool:
                decision.update(status="unmatched", caption_unit=units[caption[0]]["id"],
                                reason="scrambled caption line holds text that is not the exhibit's")
                continue
            pool.subtract(rest)
            head = _fragment(line, plain, *caption, sum(rest.values()))
            replacement_caption = [f"**{exhibit['label']} {exhibit['key']}** **{_caption_title(exhibit)}**"]
            if head["keep"]:
                replacement_caption += [""] + head["keep"]
        before_found, stop_before = _walk(units, caption, -1, pool, words, claimed, exhibits)
        after_found, stop_after = _walk(units, caption, 1, pool, words, claimed, exhibits)
        fragments = before_found + after_found
        found = sum(item["size"] for item in fragments) + (head["size"] if head else 0)
        coverage = found / max(1, sum(total.values()))
        decision.update(caption_unit=units[caption[0]]["id"], coverage=round(coverage, 3), scrambled=scrambled,
                        size=sum(total.values()), left=" ".join(sorted((+pool).elements()))[:200],
                        stopped_before=stop_before, stopped_after=stop_after)
        if coverage < MIN_COVERAGE:
            decision.update(status="unmatched",
                            reason=f"only {found} of {sum(total.values())} exhibit letters found as fragments")
            continue
        claimed.update((item["unit"], item["line"]) for item in fragments)
        before = exhibit["orientation"] == "above_caption"
        decision.update(
            status="replace", insert_before_caption=before, caption_line=caption[1],
            insert=_block(exhibit),
            remove=[{"unit_id": units[item["unit"]]["id"], "line": item["line"], "text": item["text"],
                     "keeps": item["keep"]} for item in fragments],
        )
        if replacement_caption is not None:
            decision["caption_replaced"] = {"from": units[caption[0]]["lines"][caption[1]], "to": replacement_caption}
        decision["_caption"] = caption
        decision["_fragments"] = fragments
        if exhibit["classification"] == "diagram":
            asset = project / "assets" / "original" / "images" / _figure_asset_name(exhibit["key"])
            decision["asset_exists"] = asset.is_file()
    return decisions


def rewrite_units(units: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> dict[int, str]:
    """New text for every unit a replacing decision touches."""
    removed: dict[int, dict[int, list[str]]] = {}
    inserted: dict[int, dict[int, tuple[list[str], bool]]] = {}
    for decision in decisions:
        if decision["status"] != "replace":
            continue
        for item in decision["_fragments"]:
            removed.setdefault(item["unit"], {})[item["line"]] = item["keep"]
        u, i = decision["_caption"]
        if "caption_replaced" in decision:
            # A scrambled caption line is rebuilt from the printed caption.
            removed.setdefault(u, {})[i] = decision["caption_replaced"]["to"]
        inserted.setdefault(u, {})[i] = (decision["insert"], decision["insert_before_caption"])
    out: dict[int, str] = {}
    for u in sorted(set(removed) | set(inserted)):
        lines: list[str] = []
        for i, line in enumerate(units[u]["lines"]):
            block = inserted.get(u, {}).get(i)
            if block and block[1]:
                lines += block[0] + [""]
            if i in removed.get(u, {}):
                lines += removed[u][i]  # a print stamp glued to a fragment stays as its own line
            else:
                lines.append(line)
            if block and not block[1]:
                lines += [""] + block[0]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        out[u] = text
    return out


def retrofit_exhibits(project: Path, apply: bool = False) -> dict[str, Any]:
    config, _ = load_resolved_project(project)
    if config.get("source", {}).get("format") != "pdf":
        raise BTPError("retrofit-exhibits needs a PDF source (exhibits are read from the page geometry)")
    inventory = build_inventory(safe_project_path(project, config["source"]["path"]))
    validate_schema(inventory, "exhibit-inventory.schema.json")
    atomic_write_json(project / "analysis" / "exhibit-inventory.json", inventory)
    manifest = load_manifest(project)
    units = _load_units(project, manifest)
    decisions = plan_retrofit(project, units, inventory)
    rewritten = rewrite_units(units, decisions)
    public = [{k: v for k, v in d.items() if not k.startswith("_")} for d in decisions]
    counts = Counter(d["status"] for d in decisions)
    report = {
        "schema_version": 1, "created_at": now_utc(), "applied": apply,
        "summary": {
            "exhibits": len(decisions), **{status: counts[status] for status in sorted(counts)},
            "units_changed": [units[u]["id"] for u in sorted(rewritten)],
            "lines_removed": sum(len(d.get("remove", [])) for d in decisions),
        },
        "decisions": public,
    }
    name = "exhibit-retrofit-log.json" if apply else "exhibit-retrofit-plan.json"
    if apply:
        hashes = {}
        for u, text in rewritten.items():
            atomic_write_text(units[u]["path"], text)
            hashes[units[u]["id"]] = {"before": units[u]["entry"]["source_hash"], "after": sha256_file(units[u]["path"])}
        for entry in manifest["units"]:
            if entry["unit_id"] in hashes:
                entry["source_hash"] = hashes[entry["unit_id"]]["after"]
        validate_schema(manifest, "manifest.schema.json")
        atomic_write_json(project / "state" / "manifest.json", manifest)
        report["source_hashes"] = hashes
    atomic_write_json(project / "analysis" / name, report)
    return {"applied": apply, "report": f"analysis/{name}", **report["summary"]}

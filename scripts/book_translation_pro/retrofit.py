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
from .exhibits import BULLET_RE, build_inventory, table_sections
from .figures import PRINT_LAYOUT_LINE_RE, SOURCE_CAPTION_RE, _caption_key, _figure_asset_name
from .manifest import load_manifest
from .pdf_text import _pipe_table
from .state import now_utc

TREATED = {"diagram", "table_grid", "table_lists", "text_list"}
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
    texts: list[str] = []
    for section in table_sections(exhibit["table"]):
        texts += list(section["header"] or []) + [c for row in section["meta"] for c in row] + section.get("notes", [])
        texts += [part for row in section["rows"] for cell in row for part in cell.split("<br>")]
    return texts


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


def crop_asset_name(exhibit: dict[str, Any]) -> str:
    """The original crop of one diagram page; a later page of the same exhibit has its own."""
    name = _figure_asset_name(exhibit["key"])
    return name if exhibit.get("part", 1) == 1 else name.removesuffix(".png") + f"-p{exhibit['part']}.png"


def _label_run(text: str, labels: list[tuple[str, ...]]) -> bool:
    """Whether the text is one or more whole labels in a row ("Historical Predictive")."""
    words = tuple(WORD_RE.findall(text.casefold()))
    if not words:
        return False
    reachable = {0}
    for start in range(len(words)):
        if start in reachable:
            for label in labels:
                if label and words[start:start + len(label)] == label:
                    reachable.add(start + len(label))
    return len(words) in reachable


def _block(exhibit: dict[str, Any]) -> list[str]:
    """The Markdown that stands in for the exhibit in the source unit."""
    key = exhibit["key"]
    if exhibit["classification"] == "diagram":
        lines = [f"![](assets/original/images/{crop_asset_name(exhibit)})", "",
                 f'<!-- btp:figure-labels exhibit="{key}" -->']
        lines += [f"{number}. {label['text']}" for number, label in enumerate(exhibit["labels"], 1)]
        return lines + ["<!-- /btp:figure-labels -->"]
    table = exhibit["table"]

    def escaped(row: list[str]) -> list[str]:
        return [cell.replace("$", "\\$") for cell in row]

    lines = [f'<!-- btp:exhibit-table exhibit="{key}" -->', ""]
    if table["mode"] == "list":
        # A list of items stays a list: one bullet per printed item, its own bullet dropped.
        items = [BULLET_RE.sub("", row[0]).strip() for row in table["rows"]]
        return lines + [f"- {' '.join(escaped([item]))}" for item in items] + ["", "<!-- /btp:exhibit-table -->"]
    for section in table_sections(table):
        for meta in section["meta"]:
            lines += [" ".join(escaped(meta)), ""]
        rows = ([escaped(section["header"])] if section["header"] else []) + [escaped(row) for row in section["rows"]]
        lines += _pipe_table(rows, header=bool(section["header"])).splitlines() + [""]
        for note in section.get("notes", []):
            # A footnote's "*" is the printed mark, not Markdown emphasis or a bullet.
            lines += [re.sub(r"^\*", r"\\*", note.replace("$", "\\$")), ""]
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


# A walk crosses a unit only when every line in it was the exhibit's, so this
# bounds how far an exhibit's text can have been scattered.
MAX_UNITS_WALKED = 3
# Emphasis inside a line ("S **M** econd"): the converter switched fonts mid-word.
EMPHASIS_INSIDE_RE = re.compile(r"\S\s*\*{1,2}\S|\S\*{1,2}\s*\S")


def _split_by_emphasis(line: str, missing: Counter[str], words: Counter[str]) -> bool:
    """Words the exhibit does not have, but only as pieces of its words cut by a
    font switch inside the line."""
    if not EMPHASIS_INSIDE_RE.search(line.strip().strip("*")):
        return False
    return all(any(piece in word for word in words) for piece in missing)


def _structure_fragment(units: list[dict[str, Any]], u: int, i: int, step: int,
                        labels: list[tuple[str, ...]]) -> list[int] | None:
    """A heading or a converter's table made only of the exhibit's whole labels:
    the line numbers it covers (in walking order), else None.

    A large label comes out as a heading ("### Causal"), and labels set side by
    side as a little table; their text is still the exhibit's.
    """
    lines = units[u]["lines"]
    stripped = lines[i].lstrip()
    if stripped.startswith("#"):
        return [i] if _label_run(_plain(stripped.lstrip("#")), labels) else None
    if not stripped.startswith("|"):
        return None
    covered = []
    j = i
    while 0 <= j < len(lines) and lines[j].lstrip().startswith("|"):
        covered.append(j)
        j += step
    cells = [_plain(cell) for j in covered for cell in lines[j].strip().strip("|").split("|")]
    cells = [cell for cell in cells if cell and not re.fullmatch(r":?-+:?", cell)]
    return covered if cells and all(_label_run(cell, labels) for cell in cells) else None


def _walk(units: list[dict[str, Any]], start: tuple[int, int], step: int, pool: Counter[str],
          words: Counter[str], claimed: set[tuple[int, int]],
          exhibits: list[dict[str, Any]], labels: list[tuple[str, ...]] | None = None,
          skip: frozenset[tuple[int, int]] = frozenset()) -> tuple[list[dict[str, Any]], str]:
    """Fragments next to the caption in one direction, and the line the walk stopped at.

    Every letter of a fragment comes out of the exhibit's letter ``pool`` (each
    used once); a line that reads as words must also use only the exhibit's
    ``words``, so prose, which always brings words of its own, stops the walk.
    A heading or table counts only when made of whole ``labels``; lines in
    ``skip`` (the caption's second line) are passed over untouched."""
    found: list[dict[str, Any]] = []
    u, i = start
    first_unit = start[0]
    while True:
        i += step
        if i < 0 or i >= len(units[u]["lines"]):
            # Past the neighbouring unit only through a unit that was the exhibit's own
            # text (it held fragments and nothing else); one of running heads alone is not.
            contributed = any(item["unit"] == u for item in found)
            u += step
            if (u < 0 or u >= len(units) or abs(u - first_unit) > MAX_UNITS_WALKED
                    or (abs(u - first_unit) > 1 and not contributed)):
                return found, "<unit boundary>"
            i = 0 if step > 0 else len(units[u]["lines"]) - 1
        line = units[u]["lines"][i]
        plain = _plain(line)
        if not plain or (u, i) in skip:
            continue
        if (u, i) in claimed:
            return found, plain[:120]
        covered = _structure_fragment(units, u, i, step, labels or [])
        if covered is not None:
            letters = _letters(" ".join(_plain(units[u]["lines"][j]).replace("|", " ") for j in covered))
            if letters - pool or any((u, j) in claimed for j in covered):
                return found, plain[:120]
            pool.subtract(letters)
            for j in covered:
                size = sum(_letters(_plain(units[u]["lines"][j])).values())
                found.append(_fragment(units[u]["lines"][j], _plain(units[u]["lines"][j]), u, j, size))
            i = covered[-1]
            continue
        if _stops_walk(line, plain, exhibits):
            return found, plain[:120]
        if _is_furniture(plain):
            continue
        text = PRINT_LAYOUT_LINE_RE.sub("", plain)
        letters = _letters(text)
        missing = _words(text) - words
        # Whole words must be the exhibit's; a line broken into letter runs can
        # only be checked letter by letter.
        if (not letters or letters - pool
                or (not _is_scrambled(text) and missing and not _split_by_emphasis(line, missing, words))):
            last = _last_words(line, pool, words) if step > 0 else None
            if last is not None:
                head, rest = last
                pool.subtract(_letters(head))
                found.append({"unit": u, "line": i, "text": line, "size": sum(_letters(head).values()), "keep": [rest]})
            return found, plain[:120]
        pool.subtract(letters)
        words.subtract(_words(text) & words)
        found.append(_fragment(line, plain, u, i, sum(letters.values())))


def _last_words(line: str, pool: Counter[str], words: Counter[str]) -> tuple[str, str] | None:
    """The exhibit's last words at the start of a line that runs on into the
    paragraph the exhibit interrupted ("… eliminate waste rized in Part B …"):
    (those words, the rest).  Only when they use up exactly the exhibit's
    remaining letters and the rest goes on in lower case; a line with markup
    is never split."""
    if re.search(r"[*_`<\[\\]", line):
        return None
    tokens = line.split()
    remaining = +pool
    for k in range(len(tokens) - 1, 0, -1):
        head, rest = " ".join(tokens[:k]), " ".join(tokens[k:])
        if not rest[:1].islower():
            continue
        if _letters(head) == remaining and not _words(head) - words:
            return head, rest
    return None


IMAGE_LINE_RE = re.compile(r"^\s*(?:!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>)\s*$", re.IGNORECASE)


def _image_beside(units: list[dict[str, Any]], caption: tuple[int, int], before: bool,
                  skip: frozenset[tuple[int, int]] = frozenset()) -> tuple[int, int] | None:
    """The image line next to a caption on the figure's side, with only blank lines
    (or the caption's own second line) between, across a unit boundary too; or None."""
    u, i = caption
    step = -1 if before else 1
    while True:
        i += step
        while not 0 <= i < len(units[u]["lines"]):
            u += step
            if not 0 <= u < len(units):
                return None
            i = len(units[u]["lines"]) - 1 if before else 0
        line = units[u]["lines"][i]
        if line.strip() and (u, i) not in skip:
            return (u, i) if IMAGE_LINE_RE.match(line) else None


def _next_line(units: list[dict[str, Any]], at: tuple[int, int]) -> tuple[int, int] | None:
    """The next non-blank line after ``at`` in the same unit."""
    u, i = at
    for j in range(i + 1, len(units[u]["lines"])):
        if units[u]["lines"][j].strip():
            return u, j
    return None


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
        merged = exhibit.get("checks", {}).get("merged_cells", [])
        if merged:
            # Splicing a table whose columns ran together would put a wrong table in the book.
            decision.update(status="needs_hand_rebuild",
                            reason=f"rebuilt table has merged cells: {'; '.join(merged[:3])}")
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
        labels = [tuple(WORD_RE.findall(text.casefold())) for text in _exhibit_texts(exhibit)]
        # The caption's second line stays with the caption, never taken as a fragment.
        tail = _next_line(units, caption) if exhibit.get("caption_tail") else None
        if tail is not None and _plain(units[tail[0]]["lines"][tail[1]]) != _plain(exhibit["caption_tail"]):
            tail = None
        skip = frozenset([tail] if tail else [])
        before_found, stop_before = _walk(units, caption, -1, pool, words, claimed, exhibits, labels, skip)
        after_found, stop_after = _walk(units, caption, 1, pool, words, claimed, exhibits, labels, skip)
        fragments = before_found + after_found
        found = sum(item["size"] for item in fragments) + (head["size"] if head else 0)
        coverage = found / max(1, sum(total.values()))
        decision.update(caption_unit=units[caption[0]]["id"], coverage=round(coverage, 3), scrambled=scrambled,
                        size=sum(total.values()), left=" ".join(sorted((+pool).elements()))[:200],
                        stopped_before=stop_before, stopped_after=stop_after)
        image = None
        if coverage < MIN_COVERAGE and not fragments and not scrambled:
            # Extraction already cropped the exhibit into an image and dropped its text
            # (a newly prepared book): that image becomes the label block or the table.
            # Extraction may set the image on either side of the caption, whatever the
            # page does; the printed side is tried first.
            printed_above = exhibit.get("orientation") == "above_caption"
            image = (_image_beside(units, caption, printed_above, skip)
                     or _image_beside(units, caption, not printed_above, skip))
            if image is not None and image not in claimed:
                fragments = [{"unit": image[0], "line": image[1], "text": units[image[0]]["lines"][image[1]],
                              "keep": [], "size": 0}]
                decision["extraction_image"] = fragments[0]["text"]
                # A table run on to further pages was pictured page by page: the pictures
                # right after this one are its other pages.
                at = image
                for _page in exhibit.get("continued", []):
                    at = _image_beside(units, at, False)
                    after = _next_line(units, at) if at else None
                    if (at is None or at in claimed or (after is not None and SOURCE_CAPTION_RE.match(
                            _plain(units[after[0]]["lines"][after[1]])))):
                        break  # a picture right above a caption is that exhibit's
                    fragments.append({"unit": at[0], "line": at[1], "text": units[at[0]]["lines"][at[1]],
                                      "keep": [], "size": 0})
                decision["extraction_images"] = len(fragments)
        if coverage < MIN_COVERAGE and "extraction_image" not in decision:
            decision.update(status="unmatched",
                            reason=f"only {found} of {sum(total.values())} exhibit letters found as fragments")
            continue
        claimed.update((item["unit"], item["line"]) for item in fragments)
        # The exhibit goes where it is printed, or where extraction put its image.
        before = image < caption if "extraction_image" in decision else exhibit["orientation"] == "above_caption"
        decision.update(
            status="replace", insert_before_caption=before, caption_line=caption[1],
            insert=_block(exhibit),
            remove=[{"unit_id": units[item["unit"]]["id"], "line": item["line"], "text": item["text"],
                     "keeps": item["keep"]} for item in fragments],
        )
        if replacement_caption is not None:
            decision["caption_replaced"] = {"from": units[caption[0]]["lines"][caption[1]], "to": replacement_caption}
        decision["_caption"] = caption
        # A block set under the caption goes under its second line too.
        decision["_insert_at"] = caption if before or tail is None else tail
        decision["_fragments"] = fragments
        if exhibit["classification"] == "diagram":
            asset = project / "assets" / "original" / "images" / crop_asset_name(exhibit)
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
        u, i = decision.get("_insert_at", decision["_caption"])
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


# Running text with a caption glued to its end: "... bringing these **EXHIBIT 7.8 Title**".
GLUED_CAPTION_RE = re.compile(r"^(?P<head>[^*\n].*?\S)\s+(?P<caption>(?:\*\*[^*\n]+\*\*\s*)+)$")


def split_glued_captions(units: list[dict[str, Any]], exhibits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put a caption that extraction ran onto the end of a paragraph back on its own line.

    A figure set at the top of a page, inside a paragraph, comes out glued to the
    paragraph's last words, where no caption can be found.  Only a whole bold run
    that is an exhibit's printed caption (label in the label's own case) is moved;
    no character is dropped.  Edits ``units`` in place; returns a log.
    """
    log = []
    for u, unit in enumerate(units):
        lines = unit["lines"]
        for i in range(len(lines) - 1, -1, -1):
            match = GLUED_CAPTION_RE.match(lines[i])
            if not match:
                continue
            plain = _plain(match["caption"])
            exhibit = next((e for e in exhibits if _is_caption_of(plain, e) and _label_key_re(e).match(plain)), None)
            if exhibit is None:
                continue
            lines[i:i + 1] = [match["head"], "", match["caption"].rstrip()]
            log.append({"exhibit": exhibit["key"], "unit": u, "caption": plain})
        if any(item["unit"] == u for item in log):
            unit["text"] = "\n".join(lines)
    return log


# A caption line that goes on into running text: "**EXHIBIT 4.2 Title** change to monitor ...".
CAPTION_WITH_TEXT_RE = re.compile(r"^(?P<caption>(?:\*\*[^*\n]+\*\*\s*)+)(?P<tail>\S.*)$")
# A paragraph ending in one of these is complete; anything else was cut off.
SENTENCE_END = (".", "!", "?", ":", ";", "\"", "”", "’")
MIN_PARAGRAPH_WORDS = 6


def _paragraph_before(lines: list[list[str]], u: int, i: int) -> tuple[int, int] | None:
    """The paragraph a figure interrupted: the nearest prose line above the caption,
    past images, exhibit blocks, tables and short leftover labels; None when that
    prose ends a sentence, or a heading or another caption comes first."""
    in_block = False
    for _ in range(MAX_UNITS_WALKED + 1):
        while i > 0:
            i -= 1
            text = lines[u][i].strip()
            if not text:
                continue
            if text.startswith("<!-- /btp:"):
                in_block = True
            elif in_block:
                in_block = not text.startswith("<!-- btp:")
            elif text.startswith(("![", "<img", "|", "<!--")):
                continue
            elif text.startswith("#") or SOURCE_CAPTION_RE.match(_plain(text)):
                return None
            elif len(text.split()) >= MIN_PARAGRAPH_WORDS:
                return None if text.endswith(SENTENCE_END) else (u, i)
        u -= 1
        if u < 0:
            return None
        i = len(lines[u])
    return None


def _paragraph_after(lines: list[list[str]], u: int, i: int) -> tuple[int, int] | None:
    """The first running text after a caption, past the caption's second line, an
    exhibit block or image set under it, within this unit and the next."""
    in_block = False
    for unit in (u, u + 1):
        if unit >= len(lines):
            return None
        for j in range(i + 1 if unit == u else 0, len(lines[unit])):
            text = lines[unit][j].strip()
            if not text:
                continue
            if text.startswith("<!-- btp:"):
                in_block = True
            if in_block:
                in_block = not text.startswith("<!-- /btp:")
                continue
            if text.startswith(("![", "<img", "|", "<!--")) or re.fullmatch(r"(?:\*\*[^*]+\*\*\s*)+", text):
                continue
            return (unit, j) if not text.startswith("#") else None
    return None


def _joined(head: str, tail: str) -> str:
    head, tail = head.rstrip(), tail.strip()
    return head[:-1] + tail if head.endswith("-") and head[-2:-1].isalpha() else f"{head} {tail}"


def rejoin_caption_text(lines: list[list[str]], exhibits: list[dict[str, Any]]) -> tuple[set[int], list[dict[str, Any]]]:
    """Give a paragraph that a figure cut in two its second half back.

    A figure set inside a paragraph splits it: the extraction ends the first half
    above the figure and puts the rest on the caption's line, after the caption,
    or both, so the translator would get half sentences (one of them inside a
    caption).  Each piece goes back to the end of the paragraph, joining a word
    broken by a hyphen.  Only text starting in lower case is moved, and only to a
    paragraph that does not end a sentence.  Edits ``lines`` (per unit) in place;
    returns the units changed and a log.
    """
    changed: set[int] = set()
    log: list[dict[str, Any]] = []
    for u in range(len(lines)):
        for i in range(len(lines[u])):
            line = lines[u][i]
            match = CAPTION_WITH_TEXT_RE.match(line)
            caption_part = match["caption"] if match else line
            if not caption_part.lstrip().startswith("**"):
                continue
            exhibit = next((e for e in exhibits if _is_caption_of(_plain(caption_part), e)), None)
            if exhibit is None:
                continue
            on_line = match["tail"].strip() if match and match["tail"][:1].islower() else None
            after = _paragraph_after(lines, u, i)
            if after is not None and not lines[after[0]][after[1]].lstrip()[:1].islower():
                after = None
            if on_line is None and after is None:
                continue
            entry = {"exhibit": exhibit["key"], "text": (on_line or lines[after[0]][after[1]].strip())[:80]}
            target = _paragraph_before(lines, u, i)
            if target is None:
                log.append(entry | {"rejoined": False, "reason": "no unfinished paragraph above the figure"})
                continue
            tu, ti = target
            if on_line is not None:
                lines[tu][ti] = _joined(lines[tu][ti], on_line)
                lines[u][i] = caption_part.rstrip()
                changed |= {u, tu}
            # The text after the caption continues it only while the sentence is still open.
            if after is not None and not lines[tu][ti].rstrip().endswith(SENTENCE_END):
                au, aj = after
                lines[tu][ti] = _joined(lines[tu][ti], lines[au][aj])
                lines[au][aj] = ""
                changed |= {au, tu}
            log.append(entry | {"rejoined": True, "from_unit": u, "to_unit": tu})
    return changed, log


# A picture whose grey thumbnail differs from a replaced exhibit's page crop by at
# most this much (0-255 per pixel), in the same proportions, shows that exhibit again.
DUPLICATE_MAX_DIFF = 6.0
DUPLICATE_RATIO_TOLERANCE = 0.05
DUPLICATE_THUMB = 48
IMAGE_SRC_RE = re.compile(r'^\s*(?:!\[[^\]]*\]\((?P<md>[^)\s]+)\)|<img\b[^>]*\bsrc="(?P<html>[^"]+)"[^>]*>)\s*$', re.IGNORECASE)


def _thumbnail(image: Any) -> tuple[bytes, float]:
    from PIL import ImageOps

    grey = ImageOps.grayscale(image.convert("RGB")).resize((DUPLICATE_THUMB, DUPLICATE_THUMB))
    return grey.tobytes(), image.width / max(1, image.height)


def drop_duplicate_pictures(project: Path, source_pdf: Path, lines: list[list[str]], inventory: dict[str, Any],
                            decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove a picture that shows again an exhibit already replaced in the text.

    Extraction may render a figure into a picture and set it far from its caption
    (beside the next sideways table), while the exhibit's block stands by its
    caption; the stray picture would print the exhibit a second time, in English.
    Only a picture on a line of its own is removed, and only when it matches the
    exhibit's own crop of the page almost pixel for pixel.  Edits ``lines`` in
    place; returns a log entry per removed picture.
    """
    import io

    import fitz
    from PIL import Image

    from .exhibits import upright_page

    pictures = []
    for u, unit_lines in enumerate(lines):
        for i, line in enumerate(unit_lines):
            match = IMAGE_SRC_RE.match(line)
            if not match:
                continue
            src = (match["md"] or match["html"]).replace("\\", "/")
            try:
                path = safe_project_path(project, src)
            except BTPError:
                continue
            if path.name.startswith("exhibit-") or not path.is_file():
                continue
            with Image.open(path) as image:
                if min(image.size) >= 32:  # smaller slivers are removed on their own
                    pictures.append((u, i, src, _thumbnail(image)))
    replaced = [e for e in inventory["exhibits"] if (e.get("crop") or e.get("region"))
                and any(d["status"] == "replace" and (d["key"], d["part"], d["page"]) == (e["key"], e["part"], e["page"])
                        for d in decisions)]
    if not pictures or not replaced:
        return []
    crops = []
    with fitz.open(source_pdf) as document:
        for exhibit in replaced:
            page, _rotation, _holder = upright_page(document, exhibit["page"])
            pixmap = page.get_pixmap(dpi=60, clip=fitz.Rect(exhibit.get("crop") or exhibit["region"]))
            with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
                crops.append((exhibit, _thumbnail(image)))
    removed = []
    for u, i, src, (pixels, ratio) in pictures:
        diff, exhibit = min(((sum(abs(a - b) for a, b in zip(pixels, crop[0])) / len(pixels), exhibit)
                             for exhibit, crop in crops if abs(ratio - crop[1]) <= DUPLICATE_RATIO_TOLERANCE * crop[1]),
                            key=lambda item: item[0], default=(None, None))
        if diff is not None and diff <= DUPLICATE_MAX_DIFF:
            lines[u][i] = ""
            removed.append({"unit": u, "picture": src, "exhibit": exhibit["key"], "difference": round(diff, 1)})
    return removed


def _inventory_and_plan(project: Path, config: dict[str, Any], units: list[dict[str, Any]], apply: bool
                        ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, str], dict[str, Any]]:
    """Inventory the source PDF (written to analysis/), decide every exhibit, and
    return (inventory, decisions, new unit texts, report)."""
    if config.get("source", {}).get("format") != "pdf":
        raise BTPError("retrofit-exhibits needs a PDF source (exhibits are read from the page geometry)")
    inventory = build_inventory(safe_project_path(project, config["source"]["path"]))
    validate_schema(inventory, "exhibit-inventory.schema.json")
    atomic_write_json(project / "analysis" / "exhibit-inventory.json", inventory)
    split = split_glued_captions(units, inventory["exhibits"])
    decisions = plan_retrofit(project, units, inventory)
    rewritten = rewrite_units(units, decisions)
    for item in split:
        rewritten.setdefault(item["unit"], units[item["unit"]]["text"])
    lines = [rewritten[u].split("\n") if u in rewritten else list(unit["lines"]) for u, unit in enumerate(units)]
    rejoined_units, rejoined = rejoin_caption_text(lines, inventory["exhibits"])
    duplicates = drop_duplicate_pictures(project, safe_project_path(project, config["source"]["path"]),
                                         lines, inventory, decisions)
    for u in rejoined_units | {item["unit"] for item in duplicates}:
        rewritten[u] = re.sub(r"\n{3,}", "\n\n", "\n".join(lines[u]))
    public = [{k: v for k, v in d.items() if not k.startswith("_")} for d in decisions]
    counts = Counter(d["status"] for d in decisions)
    report = {
        "schema_version": 1, "created_at": now_utc(), "applied": apply,
        "summary": {
            "exhibits": len(decisions), **{status: counts[status] for status in sorted(counts)},
            "units_changed": [units[u]["id"] for u in sorted(rewritten)],
            "lines_removed": sum(len(d.get("remove", [])) for d in decisions),
            "paragraphs_rejoined": sum(1 for item in rejoined if item["rejoined"]),
            "captions_split": len(split),
            "duplicate_pictures_removed": len(duplicates),
        },
        "decisions": public,
        "captions_split": [item | {"unit": units[item["unit"]]["id"]} for item in split],
        "duplicate_pictures_removed": [item | {"unit": units[item["unit"]]["id"]} for item in duplicates],
        "rejoined": [item | ({"from_unit": units[item["from_unit"]]["id"], "to_unit": units[item["to_unit"]]["id"]}
                             if item["rejoined"] else {}) for item in rejoined],
    }
    return inventory, decisions, rewritten, report


def _write_diagram_crops(project: Path, config: dict[str, Any], inventory: dict[str, Any],
                         decisions: list[dict[str, Any]]) -> list[str]:
    """The original image each inserted diagram block points at, cropped from the
    page by the inventory's crop box when it does not exist yet."""
    import fitz

    from .exhibits import upright_page

    exhibits = {(e["key"], e["part"], e["page"]): e for e in inventory["exhibits"]}
    written: list[str] = []
    with fitz.open(safe_project_path(project, config["source"]["path"])) as document:
        for decision in decisions:
            if decision["status"] != "replace" or decision["classification"] != "diagram":
                continue
            exhibit = exhibits[(decision["key"], decision["part"], decision["page"])]
            asset = project / "assets" / "original" / "images" / crop_asset_name(exhibit)
            if asset.is_file():
                continue
            page, _rotation, _holder = upright_page(document, exhibit["page"])
            asset.parent.mkdir(parents=True, exist_ok=True)
            page.get_pixmap(dpi=180, clip=fitz.Rect(exhibit["crop"]), alpha=False).save(asset)
            decision["asset_exists"] = True
            written.append(f"assets/original/images/{asset.name}")
    return written


def retrofit_prepared_units(project: Path, config: dict[str, Any], unit_ids: list[str]) -> dict[str, Any]:
    """prepare's exhibit step: treat every exhibit in freshly written source units.

    Runs before capsules, the Page Map and the manifest exist, so it rewrites the
    unit files only; the manifest is then built from the treated text.  Writes the
    original crop of each inserted diagram and the decision log
    (analysis/exhibit-retrofit-log.json).
    """
    units = []
    for unit_id in unit_ids:
        path = project / "source-work" / "work-units" / f"{unit_id}.md"
        text = path.read_text(encoding="utf-8")
        units.append({"id": unit_id, "path": path, "text": text, "lines": text.split("\n")})
    inventory, decisions, rewritten, report = _inventory_and_plan(project, config, units, apply=True)
    for u, text in rewritten.items():
        atomic_write_text(units[u]["path"], text)
    report["summary"]["crops_written"] = _write_diagram_crops(project, config, inventory, decisions)
    report["decisions"] = [{k: v for k, v in d.items() if not k.startswith("_")} for d in decisions]
    report["applied_by"] = "prepare"
    atomic_write_json(project / "analysis" / "exhibit-retrofit-log.json", report)
    return report["summary"]


def retrofit_exhibits(project: Path, apply: bool = False) -> dict[str, Any]:
    config, _ = load_resolved_project(project)
    manifest = load_manifest(project)
    units = _load_units(project, manifest)
    inventory, decisions, rewritten, report = _inventory_and_plan(project, config, units, apply)
    name = "exhibit-retrofit-log.json" if apply else "exhibit-retrofit-plan.json"
    if apply:
        report["summary"]["crops_written"] = _write_diagram_crops(project, config, inventory, decisions)
        report["decisions"] = [{k: v for k, v in d.items() if not k.startswith("_")} for d in decisions]
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

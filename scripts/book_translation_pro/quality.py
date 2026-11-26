"""Translation quality rules that hold for any book: language profiles, rule checks, and
the signals that decide which units an independent reviewer reads first.

Measured on a finished book before being made rules (see references/translation-quality.md):

- Repeated sentences and Chinese typography are exact, so they are checks.
- A name printed in English in some units and translated in others is a strong
  signal but not proof (a citation keeps the author's name), so it is one warning.
- Sentence-count and length ratios swing widely with tables, lists, and indexes,
  so they never block; they rank units for the review instead.
- Abbreviations kept in Latin letters were rejected as an omission anchor: capitals
  in headings look like abbreviations, and terms such as MBA are rightly translated.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable

from .glossary import entry_matches, target_matches

CJK = "一-鿿㐀-䶿"
# Units are compared as (unit_id, source, target, observation).
Unit = tuple[str, str, str, dict[str, Any]]

# --- language profiles --------------------------------------------------------------------
# A profile holds what depends on the target language.  Checks a profile lacks are
# reported as not run, never passed silently.
PROFILES: dict[str, dict[str, Any]] = {
    "zh": {
        "name": "Chinese",
        "sentence_end": "。！？",
        "typography": {
            # Half-width , ; : ! ? set between Chinese characters.
            "half_width_punctuation": re.compile(rf"[{CJK}][,;:!?](?=\s*[{CJK}])"),
            # The same full-width mark twice, or two different ones together (，。).
            "doubled_punctuation": re.compile(r"([，。；：、！？])\1|[，；：、][，。；：、]|。[，；：、]"),
            # A space between two Chinese characters (an extraction or typing slip).
            "space_between_characters": re.compile(rf"[{CJK}] +(?=[{CJK}])"),
            # Zero-width characters: invisible on the page, but they split words for search and wrapping.
            "invisible_characters": re.compile("[​-‍⁠﻿]"),
        },
        "paired": (("“", "”"), ("（", "）"), ("《", "》")),
        # Expected sentences in the target per source sentence; below this the unit is
        # read first by the reviewer (a ranking signal, not a check).
        "sentence_ratio_signal": 0.6,
    },
}


def profile_for(language: str) -> dict[str, Any] | None:
    """The profile of a target language tag (``zh-Hans`` → ``zh``), or None."""
    return PROFILES.get(str(language).lower().split("-")[0].split("_")[0])


# --- text pieces ---------------------------------------------------------------------------
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+|(?<=[。！？])")
_EMPHASIS_RE = re.compile(r"[*_`\\]+")
_SPACE_RE = re.compile(r"\s+")
REPEAT_MIN_WEIGHT = 8          # about 13 Chinese characters or 8 English words
# A source sentence translates into a heavier target one (seven English words make about
# fourteen Chinese characters), so a repeat in the source excuses one from half the weight.
SOURCE_REPEAT_MIN_WEIGHT = 4
# Two source sentences that differ by a typo translate the same.  Rows that differ in
# their numbers are different rows, so the numbers must match.
NEAR_DUPLICATE = 0.97
_DIGITS_RE = re.compile(r"\d+")
JOIN_WINDOW = 3                # sentences on each side of a join between units


def _prose(markdown: str) -> list[str]:
    from .qa import _prose_blocks

    return _prose_blocks(markdown)


def _weight(text: str) -> float:
    from .qa import _prose_weight

    return _prose_weight(text)


_TRAILING_RE = re.compile(r"[.。!！?？;；:：,，]+$")


def sentences(markdown: str) -> list[str]:
    """Normalised sentences of the prose blocks, in order (emphasis and the closing
    punctuation dropped, spacing collapsed, so two copies of a sentence compare equal)."""
    out = []
    for block in _prose(markdown):
        for piece in _SENTENCE_RE.split(block):
            piece = _TRAILING_RE.sub("", _SPACE_RE.sub(" ", _EMPHASIS_RE.sub("", piece)).strip()).strip()
            if piece:
                out.append(piece)
    return out


def _long(pieces: Iterable[str]) -> list[str]:
    return [piece for piece in pieces if _weight(piece) >= REPEAT_MIN_WEIGHT]


def _target_repeats(pieces: list[str]) -> Counter[str]:
    return Counter({piece: n - 1 for piece, n in Counter(pieces).items() if n > 1})


def _near_duplicates(pieces: Iterable[str]) -> int:
    """Pairs of different sentences that are the same but for a slip ("waits" / "awaits")."""
    distinct = sorted(set(pieces))
    pairs = 0
    for index, first in enumerate(distinct):
        for second in distinct[index + 1:]:
            if _DIGITS_RE.findall(first) != _DIGITS_RE.findall(second):
                continue
            matcher = SequenceMatcher(None, first, second, autojunk=False)
            if (matcher.real_quick_ratio() >= NEAR_DUPLICATE and matcher.quick_ratio() >= NEAR_DUPLICATE
                    and matcher.ratio() >= NEAR_DUPLICATE):
                pairs += 1
    return pairs


def _source_repeats(source: str) -> int:
    """Repeats in the source, counted on its joined text: a copy that extraction wrapped
    across two lines or blocks, or printed without its full stop, still counts.  A short
    sentence (a list item or a table cell) counts when it is repeated whole, and two
    sentences that differ by a slip count as a repeat."""
    pieces = sentences(source)
    joined = " ".join(pieces)
    long = set(_long(pieces))
    exact = sum(joined.count(piece) - 1 for piece in long if joined.count(piece) > 1)
    short = Counter(piece for piece in pieces
                    if piece not in long and _weight(piece) >= SOURCE_REPEAT_MIN_WEIGHT)
    exact += sum(n - 1 for n in short.values())
    return exact + _near_duplicates(piece for piece in pieces if _weight(piece) >= SOURCE_REPEAT_MIN_WEIGHT)


# --- checks ---------------------------------------------------------------------------------

def repeated_text(units: list[Unit]) -> list[dict[str, Any]]:
    """Sentences the target repeats more often than the source repeats any: within a unit,
    or on both sides of a join between two units (a passage translated twice)."""
    findings = []
    previous: tuple[str, list[str]] | None = None
    for unit_id, source, target, _observation in units:
        target_long = _long(sentences(target))
        repeats = _target_repeats(target_long)
        if sum(repeats.values()) > _source_repeats(source):
            findings.append({"unit_id": unit_id, "where": "within the unit", "count": sum(repeats.values()),
                             "example": next(iter(repeats))[:60]})
        if previous is not None:
            target_join = set(previous[1][-JOIN_WINDOW:]) & set(target_long[:JOIN_WINDOW])
            # Excused only when the source itself repeats across the two units.
            across = _source_repeats(previous[0] + "\n\n" + source) - _source_repeats(previous[0]) - _source_repeats(source)
            if target_join and across <= 0:
                findings.append({"unit_id": unit_id, "where": "across the join with the previous unit",
                                 "count": len(target_join), "example": next(iter(target_join))[:60]})
        previous = (source, target_long)
    return findings


_LABEL_BLOCK_RE = re.compile(r"<!--\s*btp:figure-labels.*?<!--\s*/btp:figure-labels\s*-->", re.S)
_AUTOLINK_RE = re.compile(r"<(?:https?|ftp|mailto):[^>\s]+>")


def typography_issues(target: str, profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Typography slips by kind, with a count and the first example.  Diagram labels
    (spaced to fit a drawing) are left out, and a bare link counts as a word, so the
    punctuation on either side of a removed URL is not read as doubled."""
    from .qa import _table_cells

    issues: dict[str, dict[str, Any]] = {}
    target = _AUTOLINK_RE.sub(" URL ", _LABEL_BLOCK_RE.sub("", target))
    text = "\n".join(_prose(target) + _table_cells(target))
    for kind, pattern in profile["typography"].items():
        found = [match.group(0) for match in pattern.finditer(text)]
        if found:
            issues[kind] = {"count": len(found), "example": found[0]}
    for opening, closing in profile["paired"]:
        if text.count(opening) != text.count(closing):
            issues[f"unpaired {opening}{closing}"] = {
                "count": abs(text.count(opening) - text.count(closing)),
                "example": f"{text.count(opening)} {opening} / {text.count(closing)} {closing}"}
    return issues


_QUOTE_RE = re.compile(r"^[>\s*_]+|[\s*_]+$")
_SOURCE_ELLIPSIS = ("...", "…", ". . .")


def _edge_blocks(markdown: str) -> tuple[str, str]:
    blocks = [_QUOTE_RE.sub("", block) for block in _prose(markdown)]
    blocks = [block for block in blocks if block]
    return (blocks[0], blocks[-1]) if blocks else ("", "")


def split_fragments(units: list[Unit]) -> list[dict[str, Any]]:
    """Units whose translation ends or starts with an ellipsis the source does not have:
    a sentence split by a page break left as two fragments (……) instead of one
    sentence in one unit."""
    out = []
    for unit_id, source, target, _observation in units:
        source_first, source_last = _edge_blocks(source)
        target_first, target_last = _edge_blocks(target)
        if target_last.endswith(("……", "…")) and not source_last.rstrip(" ”\"’)").endswith(_SOURCE_ELLIPSIS):
            out.append({"unit_id": unit_id, "where": "end", "example": target_last[-40:]})
        if target_first.startswith(("……", "…")) and not source_first.startswith(_SOURCE_ELLIPSIS):
            out.append({"unit_id": unit_id, "where": "start", "example": target_first[:40]})
    return out


# A source block that ends a sentence: its stop, then closing quotes, brackets, emphasis,
# or a note marker.  A block that ends otherwise runs on into the next one.
_BLOCK_END_RE = re.compile(r"[.!?:;…。！？：；][\s\"'”’)）*_\]^\d]*$")


def unexplained_shortfalls(units: list[Unit]) -> list[dict[str, Any]]:
    """Units whose translation has fewer paragraphs than the source, behind a note that
    excuses it (QA then only warns), by more than a reflow can explain.

    A reflow joins a block that runs on into the next (one that does not end a sentence,
    or a first block starting in lower case that continues the previous unit), or moves
    blocks into a neighbouring translation, which then has more blocks than its source.
    A paragraph that ends a sentence and is gone is not a reflow.  Measured on Lean
    Accounting, this picks 11 of 133 excused shortfalls: the unit that dropped a biography
    paragraph, and 10 whose notes say PDF extraction split lines or exhibits."""
    from .qa import STRUCTURE_EVIDENCE_PREFIXES, _has_evidence, _prose_blocks

    counts = [(len(_prose_blocks(source)), len(_prose_blocks(target))) for _u, source, target, _o in units]
    out = []
    for index, (unit_id, source, target, observation) in enumerate(units):
        source_blocks, target_blocks = counts[index]
        if not target_blocks or source_blocks <= target_blocks or not _has_evidence(observation, STRUCTURE_EVIDENCE_PREFIXES):
            continue
        blocks = [block.strip() for block in _prose_blocks(source)]
        run_on = sum(1 for block in blocks if not _BLOCK_END_RE.search(block))
        continued = int(bool(re.match(r"[a-z]", blocks[0])))
        moved = sum(max(0, t - s) for s, t in (counts[i] for i in (index - 1, index + 1) if 0 <= i < len(units)))
        missing = source_blocks - target_blocks - run_on - continued - moved
        if missing > 0:
            out.append({"unit_id": unit_id, "source": source_blocks, "target": target_blocks, "missing": missing})
    return out


_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
_SENTENCE_STOP = ("。", "！", "？", ".", "!", "?")
HEADING_PARAGRAPH_WEIGHT = 40   # about 67 Chinese characters; chapter titles stay far below


def heading_paragraphs(markdown: str) -> list[str]:
    """Markdown headings that are really paragraphs: long text ending in a full stop
    (extraction marks a paragraph as a heading, and a translation keeps the markup)."""
    found = []
    for match in _HEADING_LINE_RE.finditer(markdown):
        text = re.sub(r"<[^>]+>|[*_`]", "", match.group(2)).strip()
        if text.rstrip("”’\"）)").endswith(_SENTENCE_STOP) and _weight(text) >= HEADING_PARAGRAPH_WEIGHT:
            found.append(text[:40])
    return found


# Note markers as extraction writes them (^8^, <sup>8</sup>, [^8]) and as a translation may
# (digits right after full-width punctuation, superscript digits).  <sup> is rewritten as
# ^8^ first: the comparison text drops every tag.
_SUP_NOTE_RE = re.compile(r"<sup\b[^>]*>\s*(?:<a\b[^>]*>\s*)?\\?\[?\s*(\d{1,3})\s*\\?\]?\s*(?:</a>\s*)?</sup>",
                          re.IGNORECASE)
_SOURCE_NOTE_RE = re.compile(r"\^\d{1,3}\^|\[\^[^\]]+\](?!:)")
# A number after punctuation that a measure word or unit follows is a quantity
# ("包括：3个步骤", "其中，10家企业"), not a note marker.
_COUNTED = "个家项次步条章节点页图表强倍元万亿千百种位名台件类份组轮届期层级段行列部本篇块张人所季天周岁年月日号时分秒米吨磅%"
_TARGET_NOTE_RE = re.compile(r"\^\d{1,3}\^|\[\^[^\]]+\](?!:)|[⁰¹²³⁴⁵⁶⁷⁸⁹]+"
                             rf"|(?<=[。，；：！？”）])\d{{1,3}}(?![\d.]|\s*[{_COUNTED}])")


def note_markers(source: str, target: str) -> tuple[int, int]:
    """Note markers counted in the source and in the target, on the comparison text
    (which writes superscript spans as ^8^)."""
    from .qa import _comparison_text

    def count(pattern: re.Pattern[str], text: str) -> int:
        text = _comparison_text(_SUP_NOTE_RE.sub(r"^\1^", text))
        # A marker that starts a line opens the note's own text, not a reference to it.
        return sum(1 for m in pattern.finditer(text) if m.start() and text[m.start() - 1] != "\n")

    return count(_SOURCE_NOTE_RE, source), count(_TARGET_NOTE_RE, target)


def note_marker_gaps(units: list[Unit]) -> list[dict[str, Any]]:
    """Units with fewer note markers in the translation than in the source, counted
    over the unit and its neighbours, so a marker that moved with its sentence across
    a join is not a gap."""
    counts = [note_markers(source, target) for _unit_id, source, target, _observation in units]
    out = []
    for index, (unit_id, *_rest) in enumerate(units):
        source_notes, target_notes = counts[index]
        window = counts[max(0, index - 1):index + 2]
        if source_notes > target_notes and sum(c[0] for c in window) > sum(c[1] for c in window):
            out.append({"unit_id": unit_id, "source": source_notes, "target": target_notes})
    return out


_NAME_RE = re.compile(r"(?<=[a-z,;] )([A-Z][a-z]{2,})\b")
_WORD_RE = re.compile(r"(?<![\w'’-])([A-Za-z][a-z]+)(?![\w-])")
NAME_MIN_UNITS = 3
NAME_MIN_SIDE = 2


def name_candidates(sources: list[str]) -> Counter[str]:
    """Proper names in the source: capitalised words used mid-sentence that the book
    never writes in lower case, with how often they occur mid-sentence."""
    book = "\n".join(sources)
    lower = {word for word in _WORD_RE.findall(book) if word[0].islower()}
    found = Counter(_NAME_RE.findall(book))
    return Counter({name: n for name, n in found.items() if name.lower() not in lower and n >= 2})


def _gloss_free(target: str, name: str) -> str:
    """The target without parenthesised glosses of the name (戴明（Deming）keeps it as a gloss)."""
    return re.sub(rf"[（(][^）)]*{re.escape(name)}[^）)]*[）)]", "", target)


def name_consistency(units: list[Unit], covered: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Names printed in English in some units and not in others.  Names that a glossary or
    ledger entry covers are left to that entry's own check."""
    covered = list(covered)
    # Units kept in the source language by design, and placeholders with no reader text
    # (an omitted index mentions every name but translates none), are left out.
    texts = [(unit_id, "\n".join(_prose(source)), "\n".join(_prose(target)))
             for unit_id, source, target, observation in units
             if not any(str(issue).startswith("untranslated_by_design") for issue in observation.get("source_issues", []))
             and re.sub(r"<!--.*?-->", "", target, flags=re.S).strip()]
    out = []
    for name in name_candidates([source for _, source, _ in texts]):
        if any(entry_matches(entry, name) for entry in covered):
            continue
        pattern = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")
        # Bylines and headings print names in capitals (DR. C. J. MCNAIR).
        in_source = re.compile(rf"(?<![\w-])(?:{re.escape(name)}|{re.escape(name.upper())})(?![\w-])")
        mentioning = [(unit_id, target) for unit_id, source, target in texts if in_source.search(source)]
        if len(mentioning) < NAME_MIN_UNITS:
            continue
        english = [unit_id for unit_id, target in mentioning if pattern.search(_gloss_free(target, name))]
        other = [unit_id for unit_id, _ in mentioning if unit_id not in english]
        if len(english) >= NAME_MIN_SIDE and len(other) >= NAME_MIN_SIDE:
            out.append({"name": name, "units": len(mentioning), "english": english, "translated": other})
    # The most-mentioned names first: they are the ones a reader notices.
    return sorted(out, key=lambda item: (-item["units"], item["name"]))


def ledger_checks(units: list[Unit], ledger: dict[str, Any], glossary: dict[str, Any]) -> list[dict[str, Any]]:
    """Units whose source mentions a recorded rendering but whose target does not use it.
    An approved glossary entry for the same source is enforced by the glossary check."""
    approved = [entry for entry in glossary.get("entries", []) if entry["status"] == "approved"]
    out = []
    for entry in ledger.get("entries", []):
        if len(entry["targets"]) != 1 or any(entry_matches(item, entry["source"]) for item in approved):
            continue
        rule = {"source": entry["source"], "aliases": [], "target": entry["targets"][0]}
        missing = [unit_id for unit_id, source, target, _ in units
                   if entry_matches(rule, "\n".join(_prose(source))) and not target_matches(rule, target)]
        if missing:
            out.append({"source": entry["source"], "target": rule["target"], "units": missing})
    return out


# --- review priority signals --------------------------------------------------------------

def unit_signals(source: str, target: str, profile: dict[str, Any] | None) -> list[str]:
    """Reasons to read this unit first in a review.  They rank; they never block."""
    from .qa import TRUNCATION_WARN_RATIO

    reasons = []
    source_weight = sum(map(_weight, _prose(source)))
    if source_weight >= 40:
        ratio = sum(map(_weight, _prose(target))) / source_weight
        if ratio < TRUNCATION_WARN_RATIO + 0.1:
            reasons.append(f"target is {ratio:.0%} of the source length")
    if profile:
        source_count = len(_long(sentences(source)))
        target_count = len([s for s in sentences(target) if _weight(s) >= 2])
        if source_count >= 8 and target_count / source_count < profile["sentence_ratio_signal"]:
            reasons.append(f"{target_count} target sentences for {source_count} source sentences")
    return reasons


# --- glossary candidates at prepare -----------------------------------------------------------
_STOP = set("""a an and are as at be been but by can could did do does each for from had has have he her his
how if in into is it its may more most must no not of on one or our out over she should so some such than that
the their them then there these they this those through to two up us was we were what when where which while
who will with would you your also other only many much very any all both same new own about after before
between under first second third""".split())
_PHRASE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
CANDIDATE_MIN_COUNT = 3
CANDIDATE_MIN_UNITS = 2


def term_candidates(sources: dict[str, str], limit: int) -> dict[str, list[dict[str, Any]]]:
    """Recurring names and two- and three-word phrases across the whole book, for the
    glossary author to consider beside the sampled units."""
    phrases: Counter[str] = Counter()
    where: dict[str, set[str]] = {}
    for unit_id, text in sources.items():
        for block in _prose(text):
            for piece in _SENTENCE_RE.split(block):
                words = [w.lower() for w in _PHRASE_WORD_RE.findall(piece)]
                for size in (2, 3):
                    for start in range(len(words) - size + 1):
                        gram = words[start:start + size]
                        if gram[0] in _STOP or gram[-1] in _STOP or any(len(w) < 3 for w in gram):
                            continue
                        phrase = " ".join(gram)
                        phrases[phrase] += 1
                        where.setdefault(phrase, set()).add(unit_id)
    ranked = [(phrase, n) for phrase, n in phrases.most_common()
              if n >= CANDIDATE_MIN_COUNT and len(where[phrase]) >= CANDIDATE_MIN_UNITS]
    # A two-word phrase that is only ever part of one three-word phrase adds nothing.
    kept = [(p, n) for p, n in ranked if not (p.count(" ") == 1 and any(
        q.count(" ") == 2 and p in q and m == n for q, m in ranked))]
    names = name_candidates(list(sources.values()))
    return {
        "phrases": [{"source": p, "count": n, "units": len(where[p])} for p, n in kept[:limit]],
        "names": [{"source": name, "count": n} for name, n in names.most_common(limit)],
    }


# --- QA entry point ----------------------------------------------------------------------------

def quality_checks(check: Any, units: list[Unit], config: dict[str, Any], glossary: dict[str, Any],
                   ledger: dict[str, Any]) -> dict[str, Any]:
    """Run the rule checks over the translated units, reporting through ``check(id, status,
    message, unit_id=None)``.  Returns the details for the signals report."""
    profile = profile_for(config["target"]["language"])
    repeats = repeated_text(units)
    for finding in repeats:
        check("content.repeated_text", "fail",
              f"{finding['count']} sentence(s) repeated {finding['where']} where the source has no repeat; "
              f"first: {finding['example']!r}", finding["unit_id"])
    fragments = split_fragments(units)
    for finding in fragments:
        check("content.split_sentence", "warn",
              f"The translation {'ends' if finding['where'] == 'end' else 'starts'} with an ellipsis the source does not "
              f"have ({finding['example']!r}): a sentence split by a page break; translate it whole in one unit",
              finding["unit_id"])
    notes = note_marker_gaps(units)
    for gap in notes:
        check("content.note_markers", "warn",
              f"{gap['source']} note marker(s) in the source, {gap['target']} in the translation (neighbouring units "
              "included); a note reference may have been dropped", gap["unit_id"])
    for unit_id, _source, target, _observation in units:
        headings = heading_paragraphs(target)
        if headings:
            check("structure.heading_paragraph", "warn",
                  f"{len(headings)} heading(s) read as paragraphs (long, ending in a full stop), first {headings[0]!r}; "
                  "remove the heading markup", unit_id)
    typography: dict[str, Any] = {}
    if profile:
        for unit_id, _source, target, _observation in units:
            issues = typography_issues(target, profile)
            if issues:
                typography[unit_id] = issues
                summary = "; ".join(f"{kind} ×{item['count']} ({item['example']!r})" for kind, item in issues.items())
                check("style.typography", "warn", f"{profile['name']} typography: {summary}", unit_id)
    else:
        check("style.typography", "warn",
              f"No typography profile for target language {config['target']['language']!r}; typography was not checked")
    covered = [entry for entry in glossary.get("entries", []) if entry["status"] != "rejected"]
    covered += [{"source": entry["source"], "aliases": []} for entry in ledger.get("entries", [])]
    names = name_consistency(units, covered)
    if names:
        listed = "; ".join(f"{item['name']} (English in {len(item['english'])} of {item['units']} units, "
                           f"e.g. {', '.join(item['english'][:3])})" for item in names[:12])
        check("terminology.name_consistency", "warn",
              f"{len(names)} name(s) appear in English in some units and not in others: {listed}"
              + (f" (+{len(names) - 12} more in qa/translation-signals.json)" if len(names) > 12 else "")
              + "; record one rendering per name in the glossary or ledger")
    for conflict in [entry for entry in ledger.get("entries", []) if len(entry["targets"]) > 1]:
        check("terminology.ledger_conflict", "warn",
              f"{conflict['source']!r} was recorded with {len(conflict['targets'])} renderings "
              f"({' / '.join(conflict['targets'])}) in {', '.join(conflict['units'][:4])}; choose one")
    for item in ledger_checks(units, ledger, glossary):
        for unit_id in item["units"]:
            check("terminology.ledger", "warn",
                  f"{item['source']!r} is recorded as {item['target']!r} in the ledger but this unit does not use it",
                  unit_id)
    if ledger.get("skipped"):
        check("terminology.ledger_entries", "warn",
              f"{ledger['skipped']} observation new_entries were not usable (each needs source, target, and category)")
    return {"profile": profile["name"] if profile else None, "typography": typography, "names": names,
            "repeated_text": repeats, "split_sentences": fragments, "note_marker_gaps": notes}

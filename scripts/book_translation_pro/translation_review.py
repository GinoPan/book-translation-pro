"""Translation review: an independent reader judges the translation against the source.

``build_translation_review`` selects the units to read (every unit in Publication; in
Study, an even sample of each chapter plus every unit a rule signal points at), writes a
review file whose verdicts start ``pending``, and an overview page.  A reviewer who did
not translate the units reads each source beside its translation and records findings
by category and severity (references/translation-quality.md).  ``record_translation_review``
accepts the file only when every unit is judged and nothing changed since it was built.

The score is weighted findings per 1,000 source words.  A chapter whose sample scores
above the mode's bar must be reviewed in full; an open major or critical finding, or open
findings above the bar, fail QA.  The user reads the overview and approves the
``translation_review`` checkpoint, which binds to the recorded review.
"""

from __future__ import annotations

import html
import math
import re
from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, atomic_write_text, hash_data, load_json, safe_project_path, sha256_file, validate_schema
from .manifest import load_manifest
from .state import now_utc

REVIEW_DIR = "qa/translation-review"
TEMPLATE = f"{REVIEW_DIR}/review.json"
OVERVIEW = f"{REVIEW_DIR}/index.html"
RECORD = "qa/translation-review.json"
CHECKPOINT = "translation_review"
# MQM 2.0 scoring weights.
SEVERITY_WEIGHTS = {"minor": 1, "major": 5, "critical": 25}
BLOCKING_SEVERITIES = {"major", "critical"}


def default_status() -> tuple[str, str]:
    return "pending", ("Run translation-review, have a reviewer who did not translate the units judge them, "
                       "record it, then approve.")


# --- what there is to review -------------------------------------------------------------

STRETCH_MIN_UNITS = 5        # a chapter this long is sampled on its own; shorter ones together
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _chapters(project: Path) -> dict[str, str]:
    """Book Map node → the plain title of its top-level node (the chapter)."""
    book_map = load_json(project / "analysis" / "book-map.json")
    parent = {child: node["id"] for node in book_map["nodes"] for child in node["children"]}

    def title(node: dict[str, Any]) -> str:
        text = re.sub(r"<[^>]+>|[*_#]", "", str(node.get("title", {}).get("source") or node["id"]))
        text = " ".join(text.split())
        return text if len(text) <= 60 else text[:57] + "..."

    titles = {node["id"]: title(node) for node in book_map["nodes"]}

    def root(node_id: str) -> str:
        while node_id in parent:
            node_id = parent[node_id]
        return node_id

    return {node["id"]: titles[root(node["id"])] for node in book_map["nodes"]}


def _stretches(units: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    """Consecutive stretches of the book to sample from: a chapter of at least
    STRETCH_MIN_UNITS units on its own; runs of shorter chapters (front matter, chapter
    summaries, or a Book Map that found few real chapters) together."""
    runs: list[tuple[str, list[str]]] = []
    for u in units:
        if runs and runs[-1][0] == u["chapter"]:
            runs[-1][1].append(u["unit_id"])
        else:
            runs.append((u["chapter"], [u["unit_id"]]))
    stretches: list[tuple[str, list[str]]] = []
    small: list[tuple[str, list[str]]] = []

    def flush() -> None:
        if small:
            name = small[0][0] if len(small) == 1 else f"{small[0][0]} … {small[-1][0]}"
            stretches.append((name, [unit_id for _, ids in small for unit_id in ids]))
            small.clear()

    for name, ids in runs:
        if len(ids) >= STRETCH_MIN_UNITS:
            flush()
            stretches.append((name, ids))
        else:
            small.append((name, ids))
    flush()
    return stretches


def _evenly(items: list[str], count: int) -> list[str]:
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]


def translated_units(project: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Units with a translation to read, in order (markup-only units carried over unchanged are left out)."""
    from .figures import _target_root
    from .qa import _table_cells
    from .quality import _prose, _weight
    from .state import load_state

    state = load_state(project)
    chapters = _chapters(project)
    root = _target_root(project, config)
    out = []
    for entry in sorted(load_manifest(project)["units"], key=lambda item: item["order"]):
        target_path = root / f"{entry['unit_id']}.md"
        if state["units"].get(entry["unit_id"], {}).get("status") != "completed" or not target_path.is_file():
            continue
        source_path = safe_project_path(project, entry["source_file"])
        source = source_path.read_text(encoding="utf-8")
        observation_path = safe_project_path(project, entry["observation_file"])
        target = target_path.read_text(encoding="utf-8")
        out.append({
            "unit_id": entry["unit_id"], "chapter": chapters.get(entry["book_node_id"], entry["book_node_id"]),
            "source": source, "target": target,
            # A unit whose translation holds no reader text (an omitted index, an exhibit
            # rebuilt in another unit) has nothing to read; its note is checked by QA.
            "placeholder": not _COMMENT_RE.sub("", target).strip(),
            "observation": load_json(observation_path) if observation_path.is_file() else {},
            # Table cells are source words too, as in the length check.
            "source_words": round(sum(map(_weight, _prose(source) + _table_cells(source)))),
            "evidence": hash_data({"source": sha256_file(source_path), "target": sha256_file(target_path)}),
        })
    # Scores and samples are kept per stretch of the book, named after its chapters.
    stretch_of = {unit_id: name for name, ids in _stretches(out) for unit_id in ids}
    for u in out:
        u["chapter"] = stretch_of[u["unit_id"]]
    return out


def _signals(units: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, list[str]]:
    """Why the rule checks point at a unit: reasons to read it first.  Names rendered two
    ways are a book-level question for the reviewer's consistency pass, not unit picks."""
    from .quality import note_marker_gaps, profile_for, repeated_text, split_fragments, typography_issues, unit_signals

    profile = profile_for(config["target"]["language"])
    reasons: dict[str, list[str]] = {u["unit_id"]: unit_signals(u["source"], u["target"], profile) for u in units}
    quads = [(u["unit_id"], u["source"], u["target"], u["observation"]) for u in units]
    for finding in repeated_text(quads):
        reasons[finding["unit_id"]].append(f"repeated text {finding['where']}")
    for gap in note_marker_gaps(quads):
        reasons[gap["unit_id"]].append(f"{gap['source']} note markers in the source, {gap['target']} in the translation")
    for fragment in split_fragments(quads):
        reasons[fragment["unit_id"]].append(f"sentence fragment at the {fragment['where']}")
    if profile:
        for u in units:
            if typography_issues(u["target"], profile):
                reasons[u["unit_id"]].append("typography slips")
    return reasons


def select_units(units: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, list[str]]:
    """unit_id → why it is reviewed, in reading order."""
    quality = config["quality"]
    readable = [u for u in units if not u["placeholder"]]
    if quality["review_scope"] == "all":
        return {u["unit_id"]: ["full review"] for u in readable}
    signals = _signals(readable, config)
    by_stretch: dict[str, list[dict[str, Any]]] = {}
    for u in units:
        by_stretch.setdefault(u["chapter"], []).append(u)
    sampled: set[str] = set()
    for members in by_stretch.values():
        # Spaced over every unit, so the picks do not move when a unit becomes a
        # placeholder; a pick that is one moves to the nearest readable unit.
        ids = [u["unit_id"] for u in members]
        options = [u["unit_id"] for u in members if not u["placeholder"]]
        for pick in _evenly(ids, math.ceil(quality["review_sample_share"] * len(ids))):
            nearest = sorted((o for o in options if o not in sampled), key=lambda o: abs(ids.index(o) - ids.index(pick)))
            if nearest:
                sampled.add(pick if pick in options and pick not in sampled else nearest[0])
    units = readable
    chosen = {}
    for u in units:
        why = (["sample of its part of the book"] if u["unit_id"] in sampled else []) + signals[u["unit_id"]]
        if why:
            chosen[u["unit_id"]] = why
    return chosen


def _previous(project: Path) -> tuple[dict[str, dict[str, Any]], str]:
    """Judged items to carry into a rebuilt review, and the reviewer: the recorded review,
    then the verdicts and findings entered in the review file but not yet recorded, so
    rebuilding never loses a review in progress."""
    items: dict[str, dict[str, Any]] = {}
    reviewer = ""
    for path, judged_only in ((project / RECORD, False), (project / TEMPLATE, True)):
        if not path.is_file():
            continue
        review = load_json(path)
        reviewer = review["reviewer"] or reviewer
        for item in review["items"]:
            if not judged_only or item["verdict"] != "pending" or item["findings"]:
                items[item["unit_id"]] = item
    return items, reviewer


def build_translation_review(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Write the review file (new or changed units pending) and the overview page."""
    if config["quality"]["review_scope"] == "none":
        raise BTPError("This mode has no translation review (quality.review_scope is none)")
    units = translated_units(project, config)
    if not units:
        raise BTPError("No translated units to review yet")
    chosen = select_units(units, config)
    previous, reviewer = _previous(project)
    if (project / RECORD).is_file():
        # A stretch whose recorded sample scored above the bar is now read in full.
        recorded = load_json(project / RECORD)
        weak = {name for name, chapter in score(recorded, config["quality"]["max_penalty_per_1000_words"])["chapters"].items()
                if chapter["needs_full_review"]}
        for u in units:
            if u["chapter"] in weak and not u["placeholder"]:
                chosen.setdefault(u["unit_id"], []).append("its part of the book scored above the bar")
    items = []
    for u in units:
        old = previous.get(u["unit_id"])
        if u["unit_id"] not in chosen and not old:
            continue
        item = {"unit_id": u["unit_id"], "chapter": u["chapter"], "source_words": u["source_words"],
                "reasons": chosen.get(u["unit_id"], ["reviewed before"]), "evidence": u["evidence"],
                "verdict": "pending", "findings": []}
        if old:
            # Earlier findings stay for the reviewer to mark fixed or still open.
            item["findings"] = old["findings"]
            if old["evidence"] == u["evidence"]:
                item["verdict"] = old["verdict"]
        items.append(item)
    chapters: dict[str, dict[str, int]] = {}
    for u in units:
        if u["placeholder"]:
            continue
        chapter = chapters.setdefault(u["chapter"], {"units": 0, "source_words": 0})
        chapter["units"] += 1
        chapter["source_words"] += u["source_words"]
    review ={"schema_version": 1, "reviewer": reviewer,
              "reviewed_at": now_utc(), "scope": config["quality"]["review_scope"], "chapters": chapters,
              "items": items}
    validate_schema(review, "translation-review.schema.json")
    atomic_write_json(project / TEMPLATE, review)
    write_overview(project, config, review)
    return {"template": TEMPLATE, "overview": OVERVIEW, "units": len(items),
            "pending": sum(1 for item in items if item["verdict"] == "pending"),
            "units_in_book": len(units)}


# --- recording, scoring, and verification ---------------------------------------------------

def stale_items(project: Path, config: dict[str, Any], review: dict[str, Any]) -> list[str]:
    """Units whose translation or source changed since the review, or that are gone."""
    current = {u["unit_id"]: u["evidence"] for u in translated_units(project, config)}
    return [item["unit_id"] for item in review["items"] if current.get(item["unit_id"]) != item["evidence"]]


def score(review: dict[str, Any], bar: float) -> dict[str, Any]:
    """Weighted findings per 1,000 source words, for each chapter and the reviewed whole.
    ``found`` counts every finding not dismissed (how good the translation was);
    ``open`` counts those not yet fixed (how good it is now)."""
    def per_thousand(points: float, words: int) -> float:
        return round(points * 1000 / words, 2) if words else 0.0

    chapters: dict[str, dict[str, Any]] = {}
    for name, totals in review["chapters"].items():
        chapters[name] = {"units": totals["units"], "units_reviewed": 0, "words_reviewed": 0, "found": 0, "open": 0}
    blocking = []
    for item in review["items"]:
        chapter = chapters.setdefault(item["chapter"], {"units": 0, "units_reviewed": 0, "words_reviewed": 0,
                                                        "found": 0, "open": 0})
        chapter["units_reviewed"] += 1
        chapter["words_reviewed"] += item["source_words"]
        for finding in item["findings"]:
            weight = SEVERITY_WEIGHTS[finding["severity"]]
            if finding["status"] != "dismissed":
                chapter["found"] += weight
            if finding["status"] == "open":
                chapter["open"] += weight
                if finding["severity"] in BLOCKING_SEVERITIES:
                    blocking.append(f"{item['unit_id']} {finding['severity']} {finding['category']}")
    for chapter in chapters.values():
        chapter["found_per_1000"] = per_thousand(chapter["found"], chapter["words_reviewed"])
        chapter["open_per_1000"] = per_thousand(chapter["open"], chapter["words_reviewed"])
        chapter["fully_reviewed"] = chapter["units_reviewed"] >= chapter["units"]
        chapter["needs_full_review"] = not chapter["fully_reviewed"] and chapter["found_per_1000"] > bar
    words = sum(c["words_reviewed"] for c in chapters.values())
    return {
        "bar_per_1000": bar, "words_reviewed": words,
        "found_per_1000": per_thousand(sum(c["found"] for c in chapters.values()), words),
        "open_per_1000": per_thousand(sum(c["open"] for c in chapters.values()), words),
        "blocking_open": blocking, "chapters": chapters,
    }


def record_translation_review(project: Path, config: dict[str, Any], source: Path) -> dict[str, Any]:
    review = load_json(source)
    validate_schema(review, "translation-review.schema.json")
    if not review["reviewer"].strip():
        raise BTPError("The translation review needs a reviewer (one who did not translate the units)")
    pending = [item["unit_id"] for item in review["items"] if item["verdict"] == "pending"]
    if pending:
        raise BTPError(f"Translation review still has pending verdicts: {', '.join(pending[:10])}"
                       + (f" (+{len(pending) - 10} more)" if len(pending) > 10 else ""))
    for item in review["items"]:
        live = [f for f in item["findings"] if f["status"] == "open"]
        if item["verdict"] == "ok" and live:
            raise BTPError(f"{item['unit_id']} is marked ok but has open findings; mark it findings, or fix them")
        if item["verdict"] == "findings" and not item["findings"]:
            raise BTPError(f"{item['unit_id']} is marked findings but lists none")
    template_path = project / TEMPLATE
    if template_path.is_file():
        missing = sorted({i["unit_id"] for i in load_json(template_path)["items"]} - {i["unit_id"] for i in review["items"]})
        if missing:
            raise BTPError("The review leaves out selected units: " + ", ".join(missing[:10]))
    stale = stale_items(project, config, review)
    if stale:
        raise BTPError("The review does not match the current translation (re-run translation-review): " + ", ".join(stale[:10]))
    atomic_write_json(project / RECORD, review)
    # The review file becomes the recorded review, so the work a rebuild carries over
    # from it is never older than the record (a reviewer may judge a copy elsewhere).
    atomic_write_json(project / TEMPLATE, review)
    write_overview(project, config, review)
    result = score(review, config["quality"]["max_penalty_per_1000_words"])
    return {"recorded": "translation-review", "path": RECORD, "units": len(review["items"]),
            "found_per_1000": result["found_per_1000"], "open_per_1000": result["open_per_1000"],
            "blocking_open": result["blocking_open"],
            "chapters_needing_full_review": [n for n, c in result["chapters"].items() if c["needs_full_review"]],
            "overview": OVERVIEW}


def problems(project: Path, config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(check id, status, message) for QA: the record is current and the score meets the bar."""
    if config["quality"]["review_scope"] == "none":
        return [("translation_review.record", "fail",
                 "The translation_review checkpoint is set but quality.review_scope is none, so no review can be "
                 "built; set quality.review_scope to sample or all")]
    path = project / RECORD
    if not path.is_file():
        return [("translation_review.record", "fail",
                 "No recorded translation review: run translation-review, have an independent reviewer judge "
                 "the selected units, and record it (record --kind translation-review)")]
    review = load_json(path)
    out = []
    stale = stale_items(project, config, review)
    if stale:
        out.append(("translation_review.record", "fail",
                    f"The recorded translation review is stale for {len(stale)} unit(s): {', '.join(stale[:10])}; "
                    "run translation-review again and record it"))
    selected = select_units(translated_units(project, config), config)
    unreviewed = sorted(set(selected) - {item["unit_id"] for item in review["items"]})
    if unreviewed:
        out.append(("translation_review.coverage", "fail",
                    f"{len(unreviewed)} selected unit(s) were not reviewed: {', '.join(unreviewed[:10])}"))
    bar = config["quality"]["max_penalty_per_1000_words"]
    result = score(review, bar)
    if result["blocking_open"]:
        out.append(("translation_review.open", "fail",
                    f"{len(result['blocking_open'])} open major or critical finding(s): {'; '.join(result['blocking_open'][:6])}"))
    elif result["open_per_1000"]:
        out.append(("translation_review.open", "warn", f"Open minor findings: {result['open_per_1000']} per 1,000 words"))
    for name, chapter in result["chapters"].items():
        if chapter["needs_full_review"]:
            out.append(("translation_review.sample", "fail",
                        f"Part of the book {name!r}: its sample scored {chapter['found_per_1000']} per 1,000 words (bar {bar}); "
                        f"review all {chapter['units']} of its units"))
    if result["open_per_1000"] > bar:
        out.append(("translation_review.score", "fail",
                    f"Open findings weigh {result['open_per_1000']} per 1,000 words (bar {bar})"))
    if not out:
        out.append(("translation_review.score", "pass",
                    f"{len(review['items'])} units reviewed; {result['found_per_1000']} found and "
                    f"{result['open_per_1000']} open per 1,000 words (bar {bar})"))
    return out


def review_evidence(project: Path, config: dict[str, Any]) -> dict[str, str]:
    """What an approval of the translation_review checkpoint binds to; refuses a review that would fail QA."""
    blocking = [message for _id, status, message in problems(project, config) if status == "fail"]
    if blocking:
        raise BTPError("The translation review cannot be approved yet: " + " | ".join(blocking[:3]))
    return {"translation_review": sha256_file(project / RECORD)}


# --- from defects to rules ----------------------------------------------------------------
# The automatic checks that already look for each category of finding.
COVERED_BY = {
    "accuracy.omission": ["content.block_coverage", "content.length_ratio"],
    "accuracy.addition": ["content.repeated_text", "content.length_ratio"],
    "accuracy.untranslated": ["content.untranslated"],
    "terminology.inconsistent": ["terminology.approved", "terminology.ledger", "terminology.name_consistency"],
    "terminology.wrong_term": ["terminology.approved"],
    "locale.conventions": ["style.typography", "content.numbers"],
    "structure.markup": ["structure.tables", "structure.heading_inventory"],
}
PROPOSAL_MIN_COUNT = 3


def rule_proposals(projects: list[Path], min_count: int = PROPOSAL_MIN_COUNT, output: Path | None = None) -> dict[str, Any]:
    """Group the findings of recorded reviews, across books, by cause (a cause such as
    term-not-in-glossary shows up under more than one category).  A cause found at least
    ``min_count`` times is proposed as a rule: a new check, or a line in the translation
    protocol or style guide.  A person decides; nothing is changed here."""
    groups: dict[str, dict[str, Any]] = {}
    books = []
    for project in projects:
        path = project / RECORD
        if not path.is_file():
            raise BTPError(f"No recorded translation review in {project}")
        review = load_json(path)
        books.append(project.name)
        for item in review["items"]:
            for finding in item["findings"]:
                if finding["status"] == "dismissed":
                    continue
                group = groups.setdefault(finding["cause"], {
                    "cause": finding["cause"], "categories": [], "count": 0,
                    "weight": 0, "books": [], "examples": []})
                if finding["category"] not in group["categories"]:
                    group["categories"].append(finding["category"])
                group["count"] += 1
                group["weight"] += SEVERITY_WEIGHTS[finding["severity"]]
                if project.name not in group["books"]:
                    group["books"].append(project.name)
                if len(group["examples"]) < 3:
                    group["examples"].append({"book": project.name, "unit_id": item["unit_id"],
                                              "source": finding["source_excerpt"][:200],
                                              "target": finding["target_excerpt"][:200], "note": finding["note"]})
    proposals = []
    for group in sorted(groups.values(), key=lambda g: (-g["weight"], -g["count"])):
        covered = [check for category in group["categories"] for check in COVERED_BY.get(category, [])]
        covered = list(dict.fromkeys(covered))
        group["existing_checks"] = covered
        group["proposal"] = group["count"] >= min_count
        group["suggestion"] = (
            f"the checks {', '.join(covered)} look for this category but missed these; add the examples to their "
            "tests and widen the rule, or add a protocol line if no rule can see it"
            if covered else
            "no automatic check looks for this; add a line to the translation protocol or style guide, and a check "
            "if the cause can be recognised in the text")
        proposals.append(group)
    report = {"schema_version": 1, "books": books, "min_count": min_count, "groups": proposals}
    out_dir = output or projects[0] / "qa"
    atomic_write_json(out_dir / "rule-proposals.json", report)
    lines = ["# Rule proposals", "", f"From the recorded translation reviews of: {', '.join(books)}.",
             f"A cause found {min_count} or more times is proposed as a rule. A person decides; the tool changes nothing.", ""]
    for group in proposals:
        mark = "PROPOSED" if group["proposal"] else "watch"
        lines += [f"## {mark}: {group['cause']} ({', '.join(group['categories'])})", "",
                  f"{group['count']} finding(s), weight {group['weight']}, in {', '.join(group['books'])}. "
                  f"Suggestion: {group['suggestion']}.", ""]
        for example in group["examples"]:
            lines.append(f"- {example['book']} {example['unit_id']}: {example['source']!r} → {example['target']!r} ({example['note']})")
        lines.append("")
    atomic_write_text(out_dir / "rule-proposals.md", "\n".join(lines))
    return {"books": books, "groups": len(proposals), "proposed": [g["cause"] for g in proposals if g["proposal"]],
            "report": str(out_dir / "rule-proposals.md")}


# --- overview ---------------------------------------------------------------------------

def _excerpt(text: str) -> str:
    return html.escape(text.strip())[:400]


def write_overview(project: Path, config: dict[str, Any], review: dict[str, Any]) -> None:
    """One page: the score by chapter, then the units with findings, then the rest folded away."""
    result = score(review, config["quality"]["max_penalty_per_1000_words"])
    rows = "".join(
        f"<tr class='{'bad' if c['needs_full_review'] else ''}'><td>{html.escape(name)}</td>"
        f"<td>{c['units_reviewed']} / {c['units']}</td><td>{c['found_per_1000']}</td><td>{c['open_per_1000']}</td></tr>"
        for name, c in result["chapters"].items())

    def card(item: dict[str, Any]) -> str:
        findings = "".join(
            f"<li class='{f['status']} {f['severity']}'><b>{html.escape(f['severity'])} · {html.escape(f['category'])}"
            f" · {html.escape(f['status'])}</b> <code>{html.escape(f['cause'])}</code><br>"
            f"<span class=src>{_excerpt(f['source_excerpt'])}</span><br>"
            f"<span class=tgt>{_excerpt(f['target_excerpt'])}</span><br>{html.escape(f['note'])}</li>"
            for f in item["findings"])
        reasons = html.escape("; ".join(item["reasons"]))
        return (f"<article class='{item['verdict']}'><h3>{html.escape(item['unit_id'])} "
                f"<small>{html.escape(item['chapter'])} · {item['source_words']} words · {html.escape(item['verdict'])}</small></h3>"
                f"<p class=why>{reasons}</p><ul>{findings}</ul></article>")

    first = [i for i in review["items"] if i["findings"] or i["verdict"] != "ok"]
    rest = [i for i in review["items"] if i not in first]
    page = f"""<!doctype html>
<html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Translation review</title>
<style>
:root {{ --ink:#1d1d1f; --muted:#6b6b70; --line:#d9d9de; --bg:#fafafa; --bad:#b42318; --warn:#b54708; --ok:#067647; }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui, sans-serif; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:18px; margin:28px 0 8px; }}
.summary, small, .why {{ color:var(--muted); }} small {{ font-weight:normal; }}
table {{ border-collapse:collapse; width:100%; max-width:720px; background:#fff; }}
td, th {{ border:1px solid var(--line); padding:4px 8px; text-align:left; }} tr.bad td {{ color:var(--bad); }}
article {{ background:#fff; border:1px solid var(--line); border-left:4px solid var(--line); border-radius:6px; padding:10px 12px; margin:10px 0; }}
article.findings {{ border-left-color:var(--warn); }} article.ok {{ border-left-color:var(--ok); }}
h3 {{ font-size:16px; margin:0 0 4px; }} ul {{ padding-left:18px; }} li {{ margin:6px 0; }}
li.open.major b, li.open.critical b {{ color:var(--bad); }} li.fixed, li.dismissed {{ color:var(--muted); }}
.src {{ font-family:Georgia, serif; }} .tgt {{ color:#1f3a8a; }}
details > summary {{ cursor:pointer; font-weight:600; margin:20px 0 8px; }}
</style></head><body>
<h1>Translation review</h1>
<p class=summary>Reviewer: {html.escape(review['reviewer'] or 'not yet recorded')} · {len(review['items'])} units read ·
found {result['found_per_1000']} and open {result['open_per_1000']} weighted findings per 1,000 source words
(bar {result['bar_per_1000']}; minor 1, major 5, critical 25).</p>
<table><tr><th>Part of the book</th><th>Units read</th><th>Found / 1,000</th><th>Open / 1,000</th></tr>{rows}</table>
<h2>Look at these ({len(first)})</h2>
{''.join(card(i) for i in first) or '<p class=summary>No findings.</p>'}
<details><summary>Read and fine ({len(rest)})</summary>
{''.join(card(i) for i in rest)}
</details>
</body></html>
"""
    atomic_write_text(project / OVERVIEW, page)

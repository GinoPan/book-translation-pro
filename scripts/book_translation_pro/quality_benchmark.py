"""Measure the translation checks on a book: plant known errors and count what is caught.

Works on any translated project, so the checks are judged on more than one book.  For
each translated unit it runs the unit-level checks on the finished text (anything they
report there is an alarm on a translation a person accepted) and on copies with one
planted error each.  An error whose check already fired on the clean unit is not
counted, so the recall is what the check adds.  Errors that only a reader can see
(a dropped sentence, a wrong meaning) are planted too: their low recall is the
measured reason the translation review exists.  Nothing in the project is changed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .artifacts import atomic_write_json, atomic_write_text

PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_NOT_PROSE = re.compile(r"^\s*(?:[|#!<>]|[-*+]\s|\d{1,3}[.)]\s|```)")


def _chunks(markdown: str) -> list[str]:
    return PARAGRAPH_SPLIT.split(markdown)


def _join(chunks: list[str]) -> str:
    return "\n\n".join(chunks)


def _longest_paragraph(chunks: list[str]) -> int | None:
    from .quality import _weight

    candidates = [(i, _weight(c)) for i, c in enumerate(chunks) if c.strip() and not _NOT_PROSE.match(c)]
    candidates = [item for item in candidates if item[1] >= 20]
    return max(candidates, key=lambda item: item[1])[0] if candidates else None


def _sentences_of(chunk: str) -> list[str]:
    return [piece for piece in re.split(r"(?<=[。！？])|(?<=[.!?])\s+", chunk) if piece.strip()]


# --- planted errors: (target, source, previous target) -> changed target, or None -------------

def drop_paragraph(target: str, source: str, previous: str | None) -> str | None:
    chunks = _chunks(target)
    index = _longest_paragraph(chunks)
    return None if index is None else _join(chunks[:index] + chunks[index + 1:])


def truncate(target: str, source: str, previous: str | None) -> str | None:
    chunks = [c for c in _chunks(target) if c.strip()]
    return _join(chunks[:max(1, int(len(chunks) * 0.4))]) if len(chunks) >= 4 else None


def drop_sentence(target: str, source: str, previous: str | None) -> str | None:
    chunks = _chunks(target)
    index = _longest_paragraph(chunks)
    if index is None:
        return None
    pieces = _sentences_of(chunks[index])
    if len(pieces) < 3:
        return None
    del pieces[len(pieces) // 2]
    chunks[index] = "".join(pieces)
    return _join(chunks)


def repeat_sentence(target: str, source: str, previous: str | None) -> str | None:
    from .quality import REPEAT_MIN_WEIGHT, _weight

    chunks = _chunks(target)
    index = _longest_paragraph(chunks)
    if index is None:
        return None
    longest = max(_sentences_of(chunks[index]), key=_weight)
    if _weight(longest) < REPEAT_MIN_WEIGHT:
        return None
    chunks[index] = chunks[index] + longest
    return _join(chunks)


def repeat_join(target: str, source: str, previous: str | None) -> str | None:
    from .quality import _long, sentences

    tail = _long(sentences(previous))[-1:] if previous else []
    return tail[0] + "。\n\n" + target if tail else None



def untranslated_sentence(target: str, source: str, previous: str | None) -> str | None:
    target_chunks, source_chunks = _chunks(target), _chunks(source)
    t, s = _longest_paragraph(target_chunks), _longest_paragraph(source_chunks)
    if t is None or s is None:
        return None
    target_chunks[t] = source_chunks[s]
    return _join(target_chunks)


def changed_number(target: str, source: str, previous: str | None) -> str | None:
    """Change the last digit of the first number the number check reads (not one in an
    image path or a marker)."""
    from .qa import _number_tokens

    before = _number_tokens(target)
    for match in re.finditer(r"(?<![\d.])(\d{2,})(?![\d.])", target):
        value = match.group(1)
        changed = target[:match.start(1)] + value[:-1] + str((int(value[-1]) + 1) % 10) + target[match.end(1):]
        if _number_tokens(changed) != before:
            return changed
    return None


def half_width_punctuation(target: str, source: str, previous: str | None) -> str | None:
    """A half-width comma between Chinese characters in text the reader sees (not in a
    comment or marker the build removes)."""
    from .quality import _prose

    for match in re.finditer(r"(?<=[一-鿿])，(?=[一-鿿])", target):
        changed = target[:match.start()] + "," + target[match.end():]
        if "\n".join(_prose(changed)) != "\n".join(_prose(target)):
            return changed
    return None


# kind -> (plant, the checks expected to catch it; empty when only a reader can)
PLANTED: dict[str, tuple[Callable[[str, str, str | None], str | None], set[str]]] = {
    "drop_paragraph": (drop_paragraph, {"content.block_coverage"}),
    "truncate": (truncate, {"content.length_ratio", "content.block_coverage"}),
    "drop_sentence": (drop_sentence, set()),
    "repeat_sentence": (repeat_sentence, {"content.repeated_text"}),
    "repeat_join": (repeat_join, {"content.repeated_text"}),
    "untranslated_sentence": (untranslated_sentence, {"content.untranslated"}),
    "changed_number": (changed_number, {"content.numbers"}),
    "half_width_punctuation": (half_width_punctuation, {"style.typography"}),
}
ALL_CHECKS = ("content.block_coverage", "content.length_ratio", "content.untranslated", "content.numbers",
              "content.repeated_text", "style.typography")


RANK = {"warn": 1, "fail": 2}


def fired(unit_id: str, source: str, target: str, observation: dict[str, Any], profile: dict[str, Any] | None,
          previous: tuple[str, str] | None = None) -> dict[str, str]:
    """The unit-level checks that report the unit, with the worst status QA would give.
    Numbers are judged as QA judges them (declared corrections and reflow evidence);
    QA's further excuse for numbers moved into a neighbouring unit is not applied."""
    from .qa import (_corrected_back, _coverage_checks, _declared_corrections, _exhibit_unit_checks,
                     _number_tokens, _variation_status)
    from .quality import repeated_text, typography_issues

    checks: list[dict[str, Any]] = []
    _coverage_checks(checks, unit_id, source, target, observation)
    numbers_target = _exhibit_unit_checks([], unit_id, source, target, observation)
    if _number_tokens(source) != _corrected_back(_number_tokens(numbers_target), _declared_corrections(observation)):
        status, _message = _variation_status("content.numbers", unit_id, source, numbers_target, observation)
        checks.append({"id": "content.numbers", "status": status})
    pair = [(f"{unit_id}-previous", previous[0], previous[1], {})] if previous else []
    if any(f["unit_id"] == unit_id for f in repeated_text(pair + [(unit_id, source, target, observation)])):
        checks.append({"id": "content.repeated_text", "status": "fail"})
    if profile and typography_issues(target, profile):
        checks.append({"id": "style.typography", "status": "warn"})
    worst: dict[str, str] = {}
    for item in checks:
        if item["status"] in RANK and RANK[item["status"]] > RANK.get(worst.get(item["id"], ""), 0):
            worst[item["id"]] = item["status"]
    return worst


def _worse(before: dict[str, str], after: dict[str, str], ids: set[str]) -> bool:
    return any(RANK.get(after.get(i, ""), 0) > RANK.get(before.get(i, ""), 0) for i in ids)


def benchmark(project: Path, config: dict[str, Any], output: Path | None = None) -> dict[str, Any]:
    from .quality import profile_for
    from .translation_review import translated_units

    profile = profile_for(config["target"]["language"])
    units = translated_units(project, config)
    alarms: dict[str, dict[str, list[str]]] = {}
    from .qa import NUMERIC_EVIDENCE_PREFIXES, _has_evidence

    planted = {kind: {"planted": 0, "caught": 0, "planted_without_excuse": 0, "caught_without_excuse": 0,
                      "not_measurable": 0, "expected_checks": sorted(expected), "missed": []}
               for kind, (_plant, expected) in PLANTED.items()}
    previous_pair: tuple[str, str] | None = None
    for u in units:
        uid, source, target, observation = u["unit_id"], u["source"], u["target"], u["observation"]
        clean = fired(uid, source, target, observation, profile, previous_pair)
        for check_id, status in clean.items():
            alarms.setdefault(check_id, {"fail": [], "warn": []})[status].append(uid)
        for kind, (plant, expected) in PLANTED.items():
            changed = plant(target, source, previous_pair[1] if previous_pair else None)
            if changed is None or changed == target:
                continue
            row = planted[kind]
            # A review-only error counts as caught when any check newly reports the unit.
            watched = expected or set(ALL_CHECKS)
            if expected and all(clean.get(i) == "fail" for i in expected):
                row["not_measurable"] += 1  # already failing: the error could not change the verdict
                continue
            row["planted"] += 1
            excused = _has_evidence(observation, NUMERIC_EVIDENCE_PREFIXES)
            if not excused:
                row["planted_without_excuse"] += 1
            now = fired(uid, source, changed, observation, profile, previous_pair)
            if _worse(clean, now, watched):
                row["caught"] += 1
                if not excused:
                    row["caught_without_excuse"] += 1
            elif len(row["missed"]) < 5:
                row["missed"].append(uid)
        previous_pair = (source, target)
    for row in planted.values():
        row["recall"] = round(row["caught"] / row["planted"], 3) if row["planted"] else None
        row["recall_without_excuse"] = (round(row["caught_without_excuse"] / row["planted_without_excuse"], 3)
                                        if row["planted_without_excuse"] else None)
    report = {
        "schema_version": 1, "units": len(units), "target_language": config["target"]["language"],
        "profile": profile["name"] if profile else None,
        "alarms_on_finished_text": {
            check_id: {"fail": len(by["fail"]), "warn": len(by["warn"]),
                       "share": round((len(by["fail"]) + len(by["warn"])) / len(units), 3) if units else 0,
                       "first_failing": by["fail"][:8]}
            for check_id, by in sorted(alarms.items())},
        "planted_errors": planted,
    }
    out_dir = output or project / "qa"
    atomic_write_json(out_dir / "quality-benchmark.json", report)
    atomic_write_text(out_dir / "quality-benchmark.md", render(report))
    return {"units": len(units), "report": str(out_dir / "quality-benchmark.json"),
            "recall": {kind: row["recall"] for kind, row in planted.items()},
            "recall_without_excuse": {kind: row["recall_without_excuse"] for kind, row in planted.items()},
            "alarms_fail_warn": {check_id: (item["fail"], item["warn"])
                                 for check_id, item in report["alarms_on_finished_text"].items()}}


def render(report: dict[str, Any]) -> str:
    lines = ["# Translation check benchmark", "",
             f"{report['units']} translated units; target {report['target_language']} "
             f"(profile: {report['profile'] or 'none'}).", "",
             "## Planted errors", "",
             "Recall without excuse: in units whose observation records no reflow or reconstruction note "
             "(such a note turns the paragraph, length, and number checks into warnings).", "",
             "| Error | Planted | Caught | Recall | Recall without excuse | Expected check |", "|---|---:|---:|---:|---:|---|"]

    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.0%}"

    for kind, row in report["planted_errors"].items():
        expected = ", ".join(row["expected_checks"]) or "review only"
        lines.append(f"| {kind} | {row['planted']} | {row['caught']} | {pct(row['recall'])} | "
                     f"{pct(row['recall_without_excuse'])} | {expected} |")
    lines += ["", "## Alarms on the finished translation", "",
              "A finished translation a person accepted: a failure here is a false alarm or a real defect left in.", "",
              "| Check | Fail | Warn | Share of units |", "|---|---:|---:|---:|"]
    for check_id, item in report["alarms_on_finished_text"].items():
        lines.append(f"| {check_id} | {item['fail']} | {item['warn']} | {item['share']:.0%} |")
    return "\n".join(lines) + "\n"

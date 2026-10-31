"""Regression tests for the defects found in the 2026-10 project review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book_translation_pro.artifacts import BTPError, atomic_write_json, atomic_write_text, load_json, sha256_file
from book_translation_pro.cli import show_unit
from book_translation_pro.config import load_resolved_project, load_yaml, write_yaml
from book_translation_pro.glossary import save_glossary, select_entries
from book_translation_pro.ingest import simplify_html_tables, strip_wrapper_divs
from book_translation_pro.pdf_text import extract_markdown, heading_lines, promote_headings
from book_translation_pro.manifest import load_manifest
from book_translation_pro.project import initialize_project, prepare_project, visualize_project
from book_translation_pro.qa import run_qa
from book_translation_pro.state import dispatch_unit, load_state, plan_run, record_review, record_text_output

from tests.unit.test_state_and_qa import make_project


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
BOOK = "# Chapter One\n\nThe first chapter has one paragraph.\n\n# Chapter Two\n\nThe second chapter has another paragraph.\n"


def fake_conversion(markdown: str):
    """Stand in for Calibre/Pandoc so prepare can run without external tools."""

    def convert(project: Path, config: dict):
        path = project / "source-work" / "extracted" / "input.md"
        atomic_write_text(path, markdown)
        return path, {"format": "docx"}

    return convert


def new_project(root: Path, markdown: str = BOOK, mode: str = "fast") -> Path:
    source = root / "book.docx"
    source.write_bytes(b"docx fixture")
    project = root / "project"
    initialize_project(source, project, "zh-CN", "en", mode, ["markdown"])
    with patch("book_translation_pro.project.convert_to_markdown", fake_conversion(markdown)):
        prepare_project(project)
    return project


def translate_all(project: Path) -> None:
    for entry in load_manifest(project)["units"]:
        unit_id = entry["unit_id"]
        capsule_path = project / entry["capsule_file"]
        capsule = load_json(capsule_path)
        capsule["summary"] = f"Summary of {unit_id}."
        atomic_write_json(capsule_path, capsule)
    for entry in load_manifest(project)["units"]:
        dispatch_unit(project, entry["unit_id"])
        atomic_write_text(project / entry["draft_file"], f"译文 {entry['unit_id']}\n")
        record_text_output(project, entry["unit_id"], "draft")


class RebuildPreservesWork(unittest.TestCase):
    def test_output_format_change_refreshes_config_without_losing_translations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = new_project(Path(temp))
            translate_all(project)
            raw = load_yaml(project / "book-project.yaml")
            raw["outputs"] = ["markdown", "docx"]
            write_yaml(project / "book-project.yaml", raw)
            blocked = plan_run(project, False, 2)["blocked"]
            self.assertIn("book_project_yaml_changed:run_prepare", blocked)

            result = prepare_project(project)
            self.assertTrue(result["config_refreshed"])
            queue = plan_run(project, False, 2)
            self.assertEqual(queue["translation_unit_ids"], [])
            self.assertEqual(len(queue["unchanged_unit_ids"]), 2)
            self.assertEqual(len(list((project / "target" / "draft").glob("unit-*.md"))), 2)

    def test_rebuild_keeps_unchanged_units_and_requeues_changed_ones(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = new_project(Path(temp))
            translate_all(project)
            changed = BOOK.replace("another paragraph", "a revised paragraph")
            with patch("book_translation_pro.project.convert_to_markdown", fake_conversion(changed)):
                result = prepare_project(project, rebuild=True)
            self.assertEqual(result["preserved"]["unit_records"], 2)
            self.assertEqual(result["preserved"]["capsules_kept"], 1)
            self.assertTrue((project / "target" / "draft" / "unit-0001.md").is_file())
            queue = plan_run(project, False, 2)
            self.assertEqual(queue["unchanged_unit_ids"], ["unit-0001"])
            self.assertEqual(queue["translation_unit_ids"], ["unit-0002"])
            self.assertIn("dependency_changed:source", queue["details"]["unit-0002"])
            self.assertEqual(queue["capsule_unit_ids"], ["unit-0002"])

    def test_rebuild_quarantines_files_of_units_that_no_longer_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = new_project(Path(temp))
            translate_all(project)
            single = "# Chapter One\n\nThe first chapter has one paragraph.\n"
            with patch("book_translation_pro.project.convert_to_markdown", fake_conversion(single)):
                result = prepare_project(project, rebuild=True)
            orphaned = result["preserved"]["orphaned_files"]
            self.assertTrue(any(path.endswith("target/draft/unit-0002.md") for path in orphaned))
            self.assertFalse((project / "target" / "draft" / "unit-0002.md").exists())


class OverridesPersist(unittest.TestCase):
    def test_prepare_override_is_written_to_yaml_and_survives_visualize(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = new_project(Path(temp), mode="study")
            with patch("book_translation_pro.project.convert_to_markdown", fake_conversion(BOOK)):
                prepare_project(project, rebuild=True, overrides={"mode": "fast"})
            self.assertEqual(load_yaml(project / "book-project.yaml")["mode"], "fast")
            visualize_project(project)
            self.assertEqual(load_resolved_project(project)[0]["mode"], "fast")

    def test_visualize_refuses_unprepared_yaml_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = new_project(Path(temp), mode="fast")
            raw = load_yaml(project / "book-project.yaml")
            raw["mode"] = "study"
            write_yaml(project / "book-project.yaml", raw)
            with self.assertRaisesRegex(BTPError, "run `prepare` first"):
                visualize_project(project)


class DispatchSnapshot(unittest.TestCase):
    def test_draft_requires_an_issued_translation_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_project(root)
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", "译文\n")
            with self.assertRaisesRegex(BTPError, "No translation package"):
                record_text_output(root, "unit-0001", "draft")

    def test_draft_written_against_an_older_glossary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_project(root, with_term=True)
            dispatch_unit(root, "unit-0001")
            glossary = load_json(root / "terminology" / "glossary.json")
            glossary["entries"][0]["target"] = "预加载"
            save_glossary(root, glossary)
            draft = root / "target" / "draft" / "unit-0001.md"
            atomic_write_text(draft, "轴承预紧\n")
            with self.assertRaisesRegex(BTPError, "glossary_selection"):
                record_text_output(root, "unit-0001", "draft")
            self.assertEqual(load_state(root)["units"]["unit-0001"]["output_hashes"], {})


QA_SOURCE = (
    "# Chapter 1 The Machine\n\n"
    "In 1998 the plant produced machines at a steady pace and kept careful records of every station.\n\n"
    "Operators reviewed each station before the shift began, and they explained why that habit mattered.\n\n"
    "The manager wrote that the routine prevented most failures during the busy winter months.\n"
)
QA_TARGET = (
    "# 第 1 章 机器\n\n"
    "1998 年，工厂以稳定的节奏生产机器，并仔细记录了每个工位的情况。\n\n"
    "操作员在班次开始前检查每个工位，并解释了这一习惯为何重要。\n\n"
    "经理写道，这一做法在繁忙的冬季月份里避免了大多数故障。\n"
)


def qa_unit(root: Path, target: str, source: str = QA_SOURCE, source_issues: list[str] | None = None) -> dict:
    """Record one translated unit against ``source`` and return the QA report."""
    config, _ = make_project(root)
    unit_source = root / "source-work" / "work-units" / "unit-0001.md"
    atomic_write_text(unit_source, source)
    manifest = load_json(root / "state" / "manifest.json")
    manifest["units"][0]["source_hash"] = sha256_file(unit_source)
    atomic_write_json(root / "state" / "manifest.json", manifest)
    atomic_write_json(root / "state" / "observations" / "unit-0001.json", {
        "schema_version": 1, "new_entries": [], "conflicts": [],
        "source_issues": source_issues or [], "used_glossary_ids": [],
    })
    record_review(root, "terminology", True, "tester", "approved")
    for kind, folder in (("draft", "draft"), ("edit", "edited")):
        atomic_write_text(root / "target" / folder / "unit-0001.md", target)
        if kind == "draft":
            dispatch_unit(root, "unit-0001")
        record_text_output(root, "unit-0001", kind)
    return run_qa(root, config)


def statuses(report: dict, check_id: str) -> list[str]:
    return [item["status"] for item in report["checks"] if item["id"] == check_id]


class QualityGates(unittest.TestCase):
    def test_faithful_chinese_translation_has_no_content_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = qa_unit(Path(temp), QA_TARGET)
            for check_id in ("content.block_coverage", "content.length_ratio", "content.untranslated",
                             "content.numbers", "unit.stale", "unit.status"):
                self.assertEqual(statuses(report, check_id), [], check_id)

    def test_untranslated_unit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = qa_unit(Path(temp), QA_SOURCE)
            self.assertEqual(statuses(report, "content.untranslated"), ["fail"])
            self.assertFalse(report["passed"])

    def test_declared_untranslated_material_is_only_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = qa_unit(Path(temp), QA_SOURCE, source_issues=["untranslated_by_design: bibliography"])
            self.assertEqual(statuses(report, "content.untranslated"), ["warn"])

    def test_omitted_paragraph_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dropped = QA_TARGET.replace("操作员在班次开始前检查每个工位，并解释了这一习惯为何重要。\n\n", "")
            report = qa_unit(Path(temp), dropped)
            self.assertEqual(statuses(report, "content.block_coverage"), ["fail"])

    def test_truncated_paragraph_fails_length_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            truncated = "# 第 1 章 机器\n\n1998 年，工厂生产机器。\n\n操作员检查。\n\n经理写道。\n"
            report = qa_unit(Path(temp), truncated)
            self.assertEqual(statuses(report, "content.length_ratio"), ["fail"])

    def test_changed_year_blocks_in_chinese_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = qa_unit(Path(temp), QA_TARGET.replace("1998", "1989"))
            self.assertEqual(statuses(report, "content.numbers"), ["fail"])

    def test_style_guide_change_makes_recorded_translation_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(statuses(qa_unit(root, QA_TARGET), "unit.stale"), [])
            style = root / "config" / "style-guide.md"
            atomic_write_text(style, style.read_text(encoding="utf-8") + "RULE-ID: register\nFormal.\n")
            report = run_qa(root, load_resolved_project(root)[0])
            self.assertEqual(statuses(report, "unit.stale"), ["fail"])
            self.assertFalse(report["passed"])


class ExtractionResidue(unittest.TestCase):
    def test_wrapper_divs_are_removed_but_link_targets_survive(self) -> None:
        markdown = (
            '<div id="calibre_link-0" class="calibre">\n\n# Title\n\nSee [the note](#calibre_link-7).\n\n'
            '<div id="calibre_link-7" class="calibre1">\n\nNote text.\n\n</div>\n\n</div>\n\n'
            "```html\n<div>\nkept in code\n</div>\n```\n"
        )
        cleaned, removed = strip_wrapper_divs(markdown)
        self.assertEqual(removed, 4)
        self.assertNotIn("calibre_link-0", cleaned)
        self.assertIn('<a id="calibre_link-7"></a>', cleaned)
        self.assertIn("```html\n<div>\nkept in code\n</div>\n```", cleaned)

    def test_html_tables_keep_structure_spans_and_ids_only(self) -> None:
        markdown = (
            '<table class="table_">\n<colgroup>\n<col style="width: 50%" />\n<col style="width: 50%" />\n</colgroup>\n'
            '<tbody class="calibre1">\n<tr class="calibre2" id="row-1">\n<td class="td_"><p>Year</p></td>\n'
            '<td colspan="2" class="td_7"><span class="calibre26">1</span></td>\n</tr>\n</tbody>\n</table>\n\n'
            '```html\n<table class="kept"><tr><td class="x">code</td></tr></table>\n```\n'
        )
        simplified, tables = simplify_html_tables(markdown)
        self.assertEqual(tables, 1)
        self.assertTrue(simplified.startswith(
            '<table>\n<tbody>\n<tr id="row-1">\n<td><p>Year</p></td>\n'
            '<td colspan="2"><span class="calibre26">1</span></td>\n</tr>\n</tbody>\n</table>\n'
        ))
        self.assertIn('<table class="kept"><tr><td class="x">code</td></tr></table>', simplified)

    def test_markup_only_unit_is_completed_without_a_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = new_project(Path(temp), markdown='<a id="top"></a>\n\n![](cover.png)\n\n' + BOOK)
            state = load_state(project)
            self.assertEqual(state["units"]["unit-0001"]["status"], "skipped_approved")
            queue = plan_run(project, False, 2)
            self.assertIn("unit-0001", queue["unchanged_unit_ids"])
            self.assertNotIn("unit-0001", queue["translation_unit_ids"] + queue["capsule_unit_ids"])
            self.assertEqual(queue["translation_unit_ids"], ["unit-0002", "unit-0003"])


def make_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    titles = ["Origins of the Factory", "The Night Shift", "Quality Without Inspectors"]
    body = "The operators reviewed the station and recorded every result before the shift began. " * 6
    for number, title in enumerate(titles, 1):
        page = document.new_page()
        page.insert_text((60, 30), "A History of Work", fontsize=9)
        page.insert_text((60, 90), f"Chapter {number}", fontsize=14)
        page.insert_text((60, 120), title, fontsize=22)
        page.insert_textbox(fitz.Rect(60, 150, 540, 330), body, fontsize=11)
        page.insert_text((60, 360), f"{number}.1 Daily Lessons", fontsize=15)
        page.insert_textbox(fitz.Rect(60, 380, 540, 560), body, fontsize=11)
        page.insert_text((290, 810), str(number), fontsize=9)
    document.save(path)
    document.close()


class PdfStructure(unittest.TestCase):
    def test_fallback_extractor_recovers_headings_and_drops_running_heads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_pdf(root / "book.pdf")
            markdown, stats = extract_markdown(root / "book.pdf", root, root / "images")
            headings = [line for line in markdown.splitlines() if line.startswith("#")]
            self.assertEqual(headings, [
                "# Chapter 1 Origins of the Factory", "## 1.1 Daily Lessons",
                "# Chapter 2 The Night Shift", "## 2.1 Daily Lessons",
                "# Chapter 3 Quality Without Inspectors", "## 3.1 Daily Lessons",
            ])
            self.assertNotIn("A History of Work", markdown)
            self.assertEqual(stats["margin_lines_removed"], 6)
            self.assertEqual(markdown.count("The operators reviewed the station"), 36)

    def test_calibre_paragraphs_are_promoted_from_pdf_font_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_pdf(root / "book.pdf")
            calibre_like = (
                '<span class="calibre3">Origins of the Factory</span>\n\n'
                "The operators reviewed the station.\n\n**1.1 Daily Lessons**\n\n```\nThe Night Shift\n```\n"
            )
            promoted, count = promote_headings(calibre_like, heading_lines(root / "book.pdf"))
            self.assertEqual(count, 2)
            self.assertIn("# Origins of the Factory\n", promoted)
            self.assertIn("## 1.1 Daily Lessons\n", promoted)
            self.assertIn("```\nThe Night Shift\n```", promoted)

    def test_prepare_falls_back_to_pymupdf_when_calibre_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_pdf(root / "book.pdf")
            initialize_project(root / "book.pdf", root / "project", "zh-CN", "en", "fast", ["markdown"])
            with patch("book_translation_pro.ingest._convert_with_tools", side_effect=BTPError("calibre crashed")):
                result = prepare_project(root / "project")
            profile = load_json(root / "project" / "analysis" / "book-profile.json")
            self.assertEqual(profile["conversion"]["extractor"], "pymupdf")
            self.assertTrue(any("PyMuPDF fallback" in warning for warning in result["warnings"]))
            book_map = load_json(root / "project" / "analysis" / "book-map.json")
            self.assertEqual(sum(node["kind"] == "chapter" for node in book_map["nodes"]), 3)


class ContextAndLanguage(unittest.TestCase):
    def test_show_unit_carries_earlier_summaries_from_the_same_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            book = (
                "# One\n\nIntro text.\n\n## One A\n\nFirst section.\n\n## One B\n\nSecond section.\n\n"
                "# Two\n\nNext chapter.\n"
            )
            project = new_project(Path(temp), markdown=book)
            translate_all(project)
            third = show_unit(project, "unit-0003", "translate")
            self.assertEqual(third["incoming_capsule"]["unit_id"], "unit-0002")
            self.assertEqual(third["chapter_context"], [{"unit_id": "unit-0001", "summary": "Summary of unit-0001."}])
            self.assertEqual(show_unit(project, "unit-0004", "translate")["chapter_context"], [])

    def test_init_warns_for_targets_outside_the_tested_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "book.docx").write_bytes(b"docx fixture")
            german = initialize_project(root / "book.docx", root / "de", "de", "en", "fast", ["markdown"])
            chinese = initialize_project(root / "book.docx", root / "zh", "zh-CN", "en", "fast", ["markdown"])
            self.assertIn("language_warning", german)
            self.assertNotIn("language_warning", chinese)


class GlossaryInflection(unittest.TestCase):
    def test_plural_and_possessive_forms_select_the_entry(self) -> None:
        glossary = {"entries": [
            {"id": "nn", "source": "neural network", "target": "神经网络", "status": "approved"},
            {"id": "st", "source": "strategy", "target": "战略", "status": "approved"},
            {"id": "ai", "source": "AI", "target": "人工智能", "status": "approved"},
        ]}
        for text, expected in (
            ("A neural network learns.", ["nn"]),
            ("Neural networks learn.", ["nn"]),
            ("The neural network's weights", ["nn"]),
            ("Two strategies differ.", ["st"]),
            ("AIs and the AI's output", ["ai"]),
            ("A networking neural layer", []),
            ("Strategic thinking", []),
        ):
            self.assertEqual([entry["id"] for entry in select_entries(glossary, text)], expected, text)


class ConcurrentRecording(unittest.TestCase):
    def test_parallel_workers_do_not_lose_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_project(root, unit_count=6)
            record_review(root, "terminology", True, "tester", "approved")
            unit_ids = [f"unit-{number:04d}" for number in range(1, 7)]
            for unit_id in unit_ids:
                dispatch_unit(root, unit_id)
                atomic_write_text(root / "target" / "draft" / f"{unit_id}.md", f"译文 {unit_id}\n")
            code = (
                "import sys; from pathlib import Path; "
                "from book_translation_pro.state import record_text_output; "
                "record_text_output(Path(sys.argv[1]), sys.argv[2], 'draft')"
            )
            env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
            workers = [
                subprocess.Popen([sys.executable, "-c", code, str(root), unit_id], env=env)
                for unit_id in unit_ids
            ]
            self.assertEqual([worker.wait() for worker in workers], [0] * len(unit_ids))
            state = load_state(root)
            recorded = sorted(uid for uid, record in state["units"].items() if record["output_hashes"].get("draft"))
            self.assertEqual(recorded, unit_ids)
            for unit_id in unit_ids:
                self.assertEqual(
                    state["units"][unit_id]["output_hashes"]["draft"],
                    sha256_file(root / "target" / "draft" / f"{unit_id}.md"),
                )


if __name__ == "__main__":
    unittest.main()

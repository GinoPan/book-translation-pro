from __future__ import annotations

import os
import base64
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from book_translation_pro.artifacts import atomic_write_json, atomic_write_text, safe_project_path
from book_translation_pro.build import build_outputs
from book_translation_pro.config import load_resolved_project
from book_translation_pro.manifest import load_manifest
from book_translation_pro.project import initialize_project, prepare_project
from book_translation_pro.qa import run_qa
from book_translation_pro.runtime import doctor_report, find_binary
from book_translation_pro.state import plan_run, record_review, record_text_output
from book_translation_pro.visual import record_visual_review


RUN_E2E = os.environ.get("BTP_RUN_E2E") == "1"


def fixture_markdown() -> str:
    sections = [
        "# Book Translation Pro Fixture",
        "A self-generated test book for pipeline validation.",
        "![A generated diagram](fixture.png)",
    ]
    for number in range(1, 26):
        sections.extend([
            f'<h1 style="page-break-before: always;">Chapter {number}</h1>',
            f"This chapter preserves value {number} mm and reference https://example.com/chapter/{number}.",
            "| Item | Value |\n|---|---:|\n| sample | 10 mm |",
            f"- Step {number}.1 checks structure.\n- Step {number}.2 checks list continuity.",
            "`bearing_preload = 10` remains code, while the surrounding prose remains translatable.",
        ])
    return "\n\n".join(sections) + "\n"


def make_sources(root: Path) -> dict[str, Path]:
    pandoc = find_binary("pandoc")
    calibre = find_binary("ebook-convert")
    assert pandoc and calibre
    markdown = root / "fixture.md"
    markdown.write_text(fixture_markdown(), encoding="utf-8")
    (root / "fixture.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAFElEQVR4nGP8z4APMOGVHZUelSAGAN1IAxHkX7dSAAAAAElFTkSuQmCC"
    ))
    docx = root / "fixture.docx"
    epub = root / "fixture.epub"
    html = root / "fixture.html"
    pdf = root / "fixture.pdf"
    subprocess.run([str(pandoc), str(markdown), "--standalone", "-o", str(docx)], check=True, cwd=root)
    subprocess.run([str(pandoc), str(markdown), "--standalone", "--toc", "-o", str(epub)], check=True, cwd=root)
    subprocess.run([str(pandoc), str(markdown), "--standalone", "-o", str(html)], check=True, cwd=root)
    subprocess.run([str(calibre), str(html), str(pdf)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import fitz

        with fitz.open(pdf) as document:
            if not 20 <= len(document) <= 30:
                raise AssertionError(f"expected a 20-30 page fixture, got {len(document)} pages")
    except ImportError:
        pass
    return {"pdf": pdf, "docx": docx, "epub": epub}


def make_visual_source(root: Path) -> Path:
    import fitz

    path = root / "visual-fixture.pdf"
    scan_path = root / "scan.png"
    scan = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(scan)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 30) if font_path.is_file() else ImageFont.load_default()
    for index, line in enumerate((
        "SCANNED APPENDIX",
        "Bearing preload must remain consistent across chapters.",
        "Measured torque is 42 Nm and tolerance is 5 percent.",
        "CALIBRATION RECORD 2026",
    )):
        draw.text((90, 120 + index * 100), line, font=font, fill="black")
    draw.rectangle((80, 650, 1120, 1400), outline="black", width=5)
    scan.save(scan_path)

    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 90), "Book Translation Pro V1.0 Visual Fixture", fontsize=22)
    page = document.new_page()
    page.insert_text((50, 55), "Chapter 1 - Two Columns", fontsize=18)
    left = fitz.Rect(50, 90, 280, 420)
    right = fitz.Rect(320, 90, 550, 420)
    page.insert_textbox(left, "Read the complete left column first.\n\nBearing preload controls spindle stiffness.\n\nStructure-aware translation preserves this order.", fontsize=10)
    page.insert_textbox(right, "The right column continues the discussion.\n\nVisual evidence resolves reading-order ambiguity.\n\nThe Page Map links pages to work units.", fontsize=10)
    page = document.new_page()
    page.insert_text((50, 55), "Chapter 2 - Table and Formula", fontsize=18)
    page.insert_text((60, 110), "Term        Value       Unit\nPreload     12          kN\nTorque      42          Nm", fontsize=11)
    for y in (90, 125, 160, 195):
        page.draw_line((50, y), (520, y))
    for x in (50, 240, 390, 520):
        page.draw_line((x, 90), (x, 195))
    page.insert_text((60, 260), "Formula: F = k x and efficiency >= 95%", fontsize=14)
    page.insert_text((60, 300), "Table 2.1 Calibration values.", fontsize=10)
    page = document.new_page()
    page.insert_image(page.rect, filename=str(scan_path))
    document.save(path)
    document.close()
    scan_path.unlink()
    return path


def complete_fake_semantic_work(project: Path) -> None:
    config, _ = load_resolved_project(project)
    manifest = load_manifest(project)
    record_review(project, "terminology", True, "integration-test", "empty glossary accepted")
    review_dir = project / "qa" / "visual-reviews"
    for review_path in sorted(review_dir.glob("page-*.json")):
        review = __import__("json").loads(review_path.read_text(encoding="utf-8"))
        review.update({
            "status": "approved", "reviewer": "integration-test",
            "reviewed_at": "2026-09-05T00:00:00Z", "notes": "Fixture page accepted.",
        })
        atomic_write_json(review_path, review)
        record_visual_review(project, review_path)
    for entry in sorted(manifest["units"], key=lambda item: item["order"]):
        unit_id = entry["unit_id"]
        capsule_path = safe_project_path(project, entry["capsule_file"])
        capsule = __import__("json").loads(capsule_path.read_text(encoding="utf-8"))
        capsule["summary"] = f"Continuity summary for {unit_id}."
        atomic_write_json(capsule_path, capsule)
        atomic_write_json(safe_project_path(project, entry["observation_file"]), {
            "schema_version": 1, "new_entries": [], "conflicts": [],
            "source_issues": [], "used_glossary_ids": [],
        })
        source = safe_project_path(project, entry["source_file"])
        draft = safe_project_path(project, entry["draft_file"])
        atomic_write_text(draft, source.read_text(encoding="utf-8"))
        record_text_output(project, unit_id, "draft")
        edited = safe_project_path(project, entry["edit_file"])
        atomic_write_text(edited, draft.read_text(encoding="utf-8"))
        record_text_output(project, unit_id, "edit")
    queue = plan_run(project, config["passes"]["edit"], config["execution"]["max_attempts_per_unit"])
    assert not queue["blocked"], queue
    assert not queue["translation_unit_ids"], queue
    assert not queue["edit_unit_ids"], queue


@unittest.skipUnless(RUN_E2E, "set BTP_RUN_E2E=1 to run real Calibre/Pandoc integration")
class RealFormatIntegrationTests(unittest.TestCase):
    def test_prepare_all_inputs_and_build_outputs(self) -> None:
        self.assertTrue(doctor_report()["v0_1_ready"])
        temp = tempfile.mkdtemp(prefix="BTP 中文 path ")
        root = Path(temp)
        try:
            sources = make_sources(root)
            projects = {}
            for fmt, source in sources.items():
                if fmt == "epub":
                    project = root.joinpath(*(["长路径段-" + ("x" * 28)] * 6), f"项目 {fmt} with spaces")
                    self.assertGreater(len(str(project)), 260)
                else:
                    project = root / f"项目 {fmt} with spaces"
                initialized = initialize_project(source, project, "zh-Hans", "en", "study", ["docx", "epub", "pdf"])
                project = Path(initialized["project"])
                prepared = prepare_project(project)
                self.assertGreater(prepared["unit_count"], 0)
                complete_fake_semantic_work(project)
                self.assertTrue(run_qa(project, load_resolved_project(project)[0])["passed"])
                projects[fmt] = project
            report = build_outputs(projects["epub"], load_resolved_project(projects["epub"])[0])
            self.assertTrue({"markdown", "docx", "epub", "pdf"}.issubset(report["generated"]))
            self.assertTrue(all(safe_project_path(projects["epub"], item["path"]).is_file() for item in report["generated"].values()))
        finally:
            cleanup = Path("\\\\?\\" + str(root)) if os.name == "nt" else root
            shutil.rmtree(cleanup)

    def test_v02_page_map_layout_and_ocr(self) -> None:
        capabilities = doctor_report()
        if not capabilities["ocr_ready"]:
            self.skipTest("Tesseract is required for the scanned-page integration test")
        root = Path(tempfile.mkdtemp(prefix="BTP visual 中文 "))
        try:
            source = make_visual_source(root)
            project = root / "视觉 project with spaces"
            initialize_project(source, project, "zh-Hans", "en", "study", ["markdown"])
            prepared = prepare_project(project)
            self.assertEqual(prepared["visual"]["page_count"], 4)
            page_map = __import__("json").loads((project / "analysis" / "page-map.json").read_text(encoding="utf-8"))
            self.assertEqual(page_map["pages"][1]["layout"], "multi_column")
            self.assertTrue({"table", "formula"}.issubset({region["role"] for region in page_map["pages"][2]["regions"]}))
            self.assertEqual(page_map["pages"][3]["ocr_status"], "completed")
            ocr_text = safe_project_path(project, page_map["pages"][3]["ocr_text_ref"]).read_text(encoding="utf-8")
            self.assertIn("42 Nm", ocr_text)
            self.assertIn("2026", ocr_text)
        finally:
            cleanup = Path("\\\\?\\" + str(root)) if os.name == "nt" else root
            shutil.rmtree(cleanup)

    def test_chm_extraction_reuses_html_pipeline(self) -> None:
        if not doctor_report()["chm_ready"]:
            self.skipTest("Pandoc, core Python dependencies, and 7-Zip or hh.exe are required for CHM extraction")
        root = Path(tempfile.mkdtemp(prefix="BTP chm 中文 "))
        try:
            # A ZIP-shaped fixture is intentional: 7-Zip exposes compiled CHM
            # contents through the same extraction interface, while this test
            # remains deterministic and does not ship a binary help book.
            source = root / "manual.chm"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("manual.hhc", """
<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML//EN">
<HTML><BODY><UL>
<LI><OBJECT type="text/sitemap"><param name="Name" value="Topic two"><param name="Local" value="topic.htm"></OBJECT>
<LI><OBJECT type="text/sitemap"><param name="Name" value="Topic one"><param name="Local" value="index.htm"></OBJECT>
</UL></BODY></HTML>
""")
                archive.writestr("index.htm", "<html><body><h1>CHM Manual</h1><p>Bearing preload is 12 kN.</p></body></html>")
                archive.writestr("topic.htm", "<html><body><h2>Topic</h2><p>Torque is 42 Nm.</p></body></html>")
            project = root / "CHM project with spaces"
            initialize_project(source, project, "zh-Hans", "en", "fast", ["markdown"])
            prepared = prepare_project(project)
            self.assertGreater(prepared["unit_count"], 0)
            conversion = __import__("json").loads((project / "analysis" / "book-profile.json").read_text(encoding="utf-8"))["conversion"]
            self.assertIn(conversion["extractor"], {"7z", "hh"})
            self.assertEqual(conversion["format"], "chm")
            self.assertEqual(conversion["topic_count"], 2)
            markdown = (project / "source-work" / "extracted" / "input.md").read_text(encoding="utf-8")
            self.assertIn("Bearing preload", markdown)
            self.assertIn("Torque", markdown)
        finally:
            cleanup = Path("\\\\?\\" + str(root)) if os.name == "nt" else root
            shutil.rmtree(cleanup)


if __name__ == "__main__":
    unittest.main()

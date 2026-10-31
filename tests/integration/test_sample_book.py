"""A figure- and table-heavy PDF sample book through prepare, figure audit, and typeset.

Real books exposed what small fixtures missed: a vector chart cropped to one
bar, chart labels left as stray text, running heads of short chapters kept
as headings, chapters stuck at level 2 under the book title, the rights page
opening the body, and narrow tables split in two.  This book has all of them.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

from book_translation_pro.artifacts import load_json
from book_translation_pro.build import typeset_preview
from book_translation_pro.config import load_resolved_project
from book_translation_pro.figures import run_figure_audit
from book_translation_pro.project import initialize_project, prepare_project
from book_translation_pro.runtime import doctor_report

from .test_real_formats import RUN_E2E, complete_fake_semantic_work

WIDTH, HEIGHT = 432, 648
TEXT = (
    "The factory opened in the spring and hired workers from the nearby towns. Each station "
    "kept a ledger of output, and the foremen compared the figures every Friday evening. "
)


def _photo(seed: int) -> bytes:
    image = Image.new("RGB", (480, 300), (200, 210, 220))
    draw = ImageDraw.Draw(image)
    for index in range(12):
        x = (index * 41 + seed * 17) % 440
        draw.rectangle([x, 60 + (index % 4) * 40, x + 30, 280], fill=(60 + index * 10, 80, 120 - seed * 10))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _furniture(page, number: int, chapter: str, opener: bool = False) -> None:
    if not opener:
        head = "THE MACHINE AGE" if number % 2 == 0 else chapter.upper()
        page.insert_text((WIDTH / 2 - len(head) * 2.6, 30), head, fontsize=8)
    page.insert_text((WIDTH / 2 - 4, HEIGHT - 24), str(number), fontsize=8)


def _paragraphs(page, top: float, count: int) -> float:
    y = top
    for _index in range(count):
        rect = fitz.Rect(48, y, WIDTH - 48, y + 70)
        spare = page.insert_textbox(rect, TEXT, fontsize=10, align=fitz.TEXT_ALIGN_JUSTIFY)
        if spare < 0:
            raise RuntimeError(f"sample paragraph does not fit at y={y}")
        y = rect.y1 - spare + 6
    return y + 4


def _ruled_table(page, top: float, rows: list[list[str]], widths: list[float], bold_header: bool) -> float:
    for r, row in enumerate(rows):
        x = 48.0
        for c, cell in enumerate(row):
            rect = fitz.Rect(x, top + r * 18, x + widths[c], top + (r + 1) * 18)
            page.draw_rect(rect, color=(0, 0, 0), width=0.6)
            page.insert_text((rect.x0 + 3, rect.y1 - 5), cell, fontsize=8, fontname="hebo" if bold_header and r == 0 else "helv")
            x += widths[c]
    return top + len(rows) * 18 + 10


def make_sample_book(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=WIDTH, height=HEIGHT)  # title page
    page.insert_text((90, 220), "The Machine Age", fontsize=26, fontname="hebo")
    page.insert_text((150, 260), "A. N. Author", fontsize=12)
    page = document.new_page(width=WIDTH, height=HEIGHT)  # chapter 1 opener with a footnote
    page.insert_text((48, 90), "Chapter 1 The Factory", fontsize=18, fontname="hebo")
    y = _paragraphs(page, 120, 3)
    page.insert_text((48, y + 4), "The ledger recorded every repair.", fontsize=10)
    page.insert_text((214, y), "1", fontsize=6)
    page.insert_text((48, HEIGHT - 60), "1 Source: factory ledger, 1910, p. 42.", fontsize=7)
    _furniture(page, 1, "The Factory", opener=True)
    page = document.new_page(width=WIDTH, height=HEIGHT)  # photo, caption below
    y = _paragraphs(page, 50, 1)
    page.insert_image(fitz.Rect(72, y, 360, y + 180), stream=_photo(1))
    page.insert_text((72, y + 196), "Figure 1.1 The factory floor in 1910.", fontsize=9)
    _paragraphs(page, y + 214, 3)
    _furniture(page, 2, "The Factory")
    page = document.new_page(width=WIDTH, height=HEIGHT)  # vector bar chart, caption above
    y = _paragraphs(page, 50, 1)
    page.insert_text((72, y + 6), "Exhibit 1.2 Output by decade", fontsize=9, fontname="hebo")
    base = y + 200
    page.draw_line((90, y + 30), (90, base), color=(0, 0, 0))
    page.draw_line((90, base), (360, base), color=(0, 0, 0))
    for index, value in enumerate((60, 110, 150, 90)):
        x = 110 + index * 62
        page.draw_rect(fitz.Rect(x, base - value, x + 36, base), color=(0, 0, 0), fill=(0.4, 0.5, 0.7))
        page.insert_text((x + 6, base + 12), str(1910 + index * 10), fontsize=8)
        page.insert_text((x + 8, base - value - 4), str(value), fontsize=7)
    page.insert_text((72, base + 30), "Source: Company records.", fontsize=7)
    _paragraphs(page, base + 44, 3)
    _furniture(page, 3, "The Factory")
    page = document.new_page(width=WIDTH, height=HEIGHT)  # headed and headerless tables
    y = _paragraphs(page, 50, 1)
    page.insert_text((48, y + 6), "Table 1.1 Station duties", fontsize=9, fontname="hebo")
    y = _ruled_table(page, y + 14, [["Station", "Duty", "Shift"], ["Press line", "Checks the seals", "Day"],
                                    ["Paint shop", "Records humidity", "Night"]], [90, 160, 80], bold_header=True)
    y = _paragraphs(page, y, 1)
    y = _ruled_table(page, y, [["North gate", "Opens at six"], ["South gate", "Opens at seven"]], [150, 180], bold_header=False)
    _paragraphs(page, y, 2)
    _furniture(page, 4, "The Factory")
    page = document.new_page(width=WIDTH, height=HEIGHT)  # chapter 2 opener
    page.insert_text((48, 90), "Chapter 2 The Workers", fontsize=18, fontname="hebo")
    _paragraphs(page, 120, 5)
    _furniture(page, 5, "The Workers", opener=True)
    page = document.new_page(width=WIDTH, height=HEIGHT)  # photo, caption above
    y = _paragraphs(page, 50, 1)
    page.insert_text((72, y + 6), "Figure 2.1 The night shift.", fontsize=9)
    page.insert_image(fitz.Rect(72, y + 14, 360, y + 194), stream=_photo(2))
    _paragraphs(page, y + 206, 3)
    _furniture(page, 6, "The Workers")
    page = document.new_page(width=WIDTH, height=HEIGHT)  # narrow six-column table
    y = _paragraphs(page, 50, 1)
    page.insert_text((48, y + 6), "Table 2.1 Wages by year", fontsize=9, fontname="hebo")
    _ruled_table(page, y + 14, [["Year", "Press", "Paint", "Store", "Gate", "Total"],
                                ["1910", "12", "10", "8", "6", "36"], ["1920", "15", "13", "9", "7", "44"]],
                 [56] * 6, bold_header=True)
    _paragraphs(page, y + 90, 3)
    _furniture(page, 7, "The Workers")
    page = document.new_page(width=WIDTH, height=HEIGHT)
    _paragraphs(page, 50, 5)
    _furniture(page, 8, "The Workers")
    document.save(path)
    document.close()


@unittest.skipUnless(RUN_E2E, "set BTP_RUN_E2E=1 to run real Calibre/Pandoc integration")
class SampleBookTests(unittest.TestCase):
    def test_figures_tables_and_chapters_survive_to_the_typeset_book(self) -> None:
        self.assertTrue(doctor_report()["v0_1_ready"])
        root = Path(tempfile.mkdtemp(prefix="BTP sample book "))
        try:
            make_sample_book(root / "book.pdf")
            project = root / "project"
            initialize_project(root / "book.pdf", project, "en", "en", "study", ["docx", "epub"],
                               rights_status="public-domain", rights_basis="Synthetic sample",
                               intended_use="personal-study")
            prepare_project(project)
            draft = (project / "source-work" / "extracted" / "input.md").read_text(encoding="utf-8")
            layout = load_json(project / "source-work" / "extracted" / "pdf-layout.json")

            # The whole chart is one image, its tick labels go with it, and the
            # source note stays translatable text.
            (chart,) = layout["figures"]
            self.assertGreaterEqual(chart["crop_bbox"][2] - chart["crop_bbox"][0], 270)
            self.assertNotIn("1910 1920", " ".join(draft.split()).replace("</span> <span", ""))
            self.assertEqual(sorted(item["text"] for item in layout["removed"] if item["kind"] == "figure_label"),
                             sorted(["1910", "1920", "1930", "1940", "60", "110", "150", "90"]))
            self.assertIn("Source: Company records.", draft)
            # Short-chapter running heads are furniture, not headings.
            self.assertNotIn("THE FACTORY", draft)
            self.assertNotIn("THE WORKERS", draft)

            complete_fake_semantic_work(project)
            config, _ = load_resolved_project(project)
            audit = run_figure_audit(project, config)
            self.assertEqual(
                {(item["kind"], item["key"]): item["status"] for item in audit["figures"]},
                {("figure", "1.1"): "pass", ("exhibit", "1.2"): "pass", ("figure", "2.1"): "pass",
                 ("table", "1.1"): "warn", ("table", "2.1"): "warn"},
            )

            candidate = typeset_preview(project, config)
            self.assertTrue(candidate["publication_validation"]["passed"], candidate["publication_validation"])
            master = (project / candidate["generated"]["markdown"]["path"]).read_text(encoding="utf-8")
            headings = [line for line in master.splitlines() if line.startswith("#")]
            self.assertEqual(headings, ["# The Machine Age", "## A. N. Author", "# Copyright and Authorization",
                                        "# Chapter 1 The Factory", "# Chapter 2 The Workers"])
            self.assertNotIn("续表", master)
            self.assertNotIn("(continued)", master)

            from docx import Document

            document = Document(project / candidate["generated"]["docx"]["path"])
            self.assertEqual([section.header.paragraphs[0].text for section in document.sections],
                             ["", "Chapter 1 The Factory", "Chapter 2 The Workers"])
            xhtml = (project / "target" / "xhtml" / "book.xhtml").read_text(encoding="utf-8")
            self.assertEqual(xhtml.count("<figure>"), 3)
            self.assertIn('alt="Figure 1.1 The factory floor in 1910."', xhtml)
        finally:
            cleanup = Path("\\\\?\\" + str(root)) if os.name == "nt" else root
            shutil.rmtree(cleanup)


if __name__ == "__main__":
    unittest.main()

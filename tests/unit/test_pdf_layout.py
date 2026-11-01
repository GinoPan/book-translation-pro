"""PDF page furniture, footnotes, figures, and tables in the extracted working draft."""

from __future__ import annotations

import re
import tempfile
from collections import Counter
import unittest
from pathlib import Path
from unittest.mock import patch

from book_translation_pro.artifacts import BTPError, load_json
from book_translation_pro.ingest import strip_calibre_spans
from book_translation_pro.pdf_text import (
    _caption_candidate,
    _continues,
    _has_header,
    _hyphen_decisions,
    _inner,
    _join_blocks,
    _line_text,
    _mark_superscripts,
    _normalize,
    _pipe_table,
    _rejoin_hyphenation,
    _rejoin_wrapped_lines,
    _split_trailing_captions,
    clean_pdf,
    promote_headings,
    restore_layout_markup,
)
from book_translation_pro.project import initialize_project, prepare_project

PAGE_WIDTH, PAGE_HEIGHT = 420, 600
SENTENCE = "The operators inspected every station before the shift began, and the habit mattered. "


def make_layout_pdf(path: Path) -> None:
    """Two chapters laid out like a printed book.

    Chapter opening pages carry only a centred folio; other pages carry an
    alternating running head (book title verso, chapter title recto) and a
    folio.  Page 2 holds a superscript footnote, page 3 a vector figure with
    labels inside it, page 4 a ruled table, and one paragraph is hyphenated
    across the page 2/3 break.
    """
    import fitz

    document = fitz.open()
    chapters = {1: "Engines of Change", 6: "The Night Shift"}
    chapter_title = ""
    for number in range(1, 9):
        page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        folio = str(number + 10)
        y = 60
        if number in chapters:
            chapter_title = chapters[number]
            page.insert_text((190, 585), folio, fontsize=8)
            page.insert_text((40, 90), f"Chapter {1 if number == 1 else 2}", fontsize=18)
            page.insert_text((40, 116), chapter_title, fontsize=18)
            y = 150
        elif number % 2:
            page.insert_text((40, 30), chapter_title, fontsize=8)
            page.insert_text((370, 30), folio, fontsize=8)
        else:
            page.insert_text((40, 30), folio, fontsize=8)
            page.insert_text((300, 30), "A HISTORY OF WORK", fontsize=8)
        if number == 3:
            page.insert_textbox(fitz.Rect(40, y, 380, y + 40), "urs when a belt slipped, so the checks came first.", fontsize=10)
            y += 40
            page.draw_rect(fitz.Rect(60, y, 360, y + 140), color=(0, 0, 0), fill=(0.85, 0.85, 0.85))
            page.draw_circle((150, y + 70), 30, color=(0, 0, 0))
            page.insert_text((135, y + 74), "motor", fontsize=9)
            page.insert_text((270, y + 74), "gearbox", fontsize=9)
            y += 150
            page.insert_text((40, y + 10), "Figure 1.1 The assembly line.", fontsize=9)
            y += 30
        if number == 4:
            page.insert_text((40, y + 10), "Table 1.1 Output by year", fontsize=9)
            y += 20
            rows = (("Year", "Units", "Defects"), ("1998", "1,200", "14"), ("1999", "1,450", "9"))
            for row_index, row in enumerate(rows):
                top = y + row_index * 20
                for column, cell in enumerate(row):
                    left = 60 + column * 100
                    page.draw_rect(fitz.Rect(left, top, left + 100, top + 20), color=(0, 0, 0))
                    page.insert_text((left + 6, top + 14), cell, fontsize=10)
            y += 80
        if number == 5:
            page.insert_text((40, y + 10), "Daily Lessons", fontsize=10, fontname="hebo")
            y += 24
        body = f"Page {number} opens here. " + SENTENCE * 3
        page.insert_textbox(fitz.Rect(40, y, 380, y + 120), body, fontsize=10)
        y += 130
        if number == 2:
            page.insert_textbox(fitz.Rect(40, y, 380, y + 30), "The ledger recorded the result.", fontsize=10)
            page.insert_text((191, y + 7), "1", fontsize=6)
            y += 40
            page.insert_textbox(
                fitz.Rect(40, y, 380, y + 60),
                "A loose belt was the most common fault, and the whole line could stop for ho-",
                fontsize=10,
            )
            page.draw_line((40, 520), (140, 520), color=(0, 0, 0))
            page.insert_text((40, 532), "1 Source: factory ledger, 1998, p. 42.", fontsize=7)
    document.save(path)
    document.close()


def prepare_layout_book(root: Path, *, calibre: bool = False) -> tuple[Path, str]:
    make_layout_pdf(root / "book.pdf")
    initialize_project(root / "book.pdf", root / "project", "zh-CN", "en", "fast", ["markdown"])
    if calibre:
        prepare_project(root / "project")
    else:
        with patch("book_translation_pro.ingest._convert_with_tools", side_effect=BTPError("no calibre in unit tests")):
            prepare_project(root / "project")
    return root / "project", (root / "project" / "source-work" / "extracted" / "input.md").read_text(encoding="utf-8")


class CleanPdf(unittest.TestCase):
    def test_layout_record_lists_furniture_tables_footnotes_and_figures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_layout_pdf(root / "book.pdf")
            record = clean_pdf(root / "book.pdf", root / "clean.pdf")
            # Six pages with a running head and folio, two chapter openings with a folio only.
            self.assertEqual(record["stats"]["furniture_lines_removed"], 14)
            self.assertEqual(record["tables"]["0001"]["markdown"].splitlines()[0], "| Year | Units | Defects |")
            self.assertEqual(record["footnotes"]["0001"]["text"], "Source: factory ledger, 1998, p. 42.")
            self.assertEqual([figure["caption"] for figure in record["figures"]], ["Figure 1.1 The assembly line."])
            self.assertEqual(record["page_breaks"], [{"page": 3, "tail": "t, and the whole line could stop for ho-", "head": "urs when a belt slipped, so the checks c"}])
            import fitz

            with fitz.open(root / "clean.pdf") as cleaned:
                text = "\n".join(page.get_text() for page in cleaned)
                self.assertNotIn("A HISTORY OF WORK", text)
                self.assertNotIn("gearbox", text)
                self.assertNotIn("1,450", text)
                self.assertEqual(len(cleaned[2].get_images()), 1)

    def test_boxed_sidebar_and_unreferenced_note_are_left_in_the_text(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            page.insert_textbox(fitz.Rect(40, 60, 380, 160), SENTENCE * 3, fontsize=10)
            page.draw_rect(fitz.Rect(50, 180, 370, 330), color=(0, 0, 0))
            page.insert_textbox(fitz.Rect(60, 190, 360, 320), "Sidebar. " + SENTENCE * 3, fontsize=10)
            page.insert_text((40, 345), "Figure 2 A boxed aside.", fontsize=9)
            page.insert_text((40, 530), "1 A note whose marker is missing.", fontsize=7)
            document.save(root / "book.pdf")
            document.close()
            record = clean_pdf(root / "book.pdf", root / "clean.pdf")
            self.assertEqual(record["figures"], [])
            self.assertEqual(record["footnotes"], {})
            self.assertEqual(record["stats"]["footnotes_unlinked"], 1)
            with fitz.open(root / "clean.pdf") as cleaned:
                text = cleaned[0].get_text()
            self.assertIn("Sidebar.", text)
            self.assertIn("A note whose marker is missing.", text)


class RestoreLayout(unittest.TestCase):
    RECORD = {
        "tables": {"0001": {"page": 4, "markdown": "| A | B |\n|---|---|\n| 1 | 2 |"}},
        "footnotes": {"0001": {"page": 2, "marker": "1", "text": "Source note."}},
        "page_breaks": [{"page": 3, "tail": "could stop for ho-", "head": "urs when a belt"}],
    }

    def test_calibre_markup_is_restored(self) -> None:
        markdown = (
            'The ledger recorded the result. <span class="calibre7">BTPFN0001Z</span>\n\n'
            "A loose belt could stop for ho-\n\n"
            '<div class="calibre6">\n\n</div>\n\n'
            '<img src="images/000000.png" class="calibre8" />\n\n'
            "urs when a belt slipped.\n\n"
            '<span class="calibre9">Table 1</span> <span class="calibre11">BTPTB0001Z</span> After.\n'
        )
        restored, stats = restore_layout_markup(markdown, self.RECORD)
        blocks = restored.rstrip("\n").split("\n\n")
        self.assertEqual(blocks[0], "The ledger recorded the result.[^fn1]")
        self.assertEqual(blocks[1], "[^fn1]: Source note.")
        self.assertEqual(blocks[2], "A loose belt could stop for hours when a belt slipped.")
        self.assertIn('<img src="images/000000.png" class="calibre8" />', blocks[3:5])
        self.assertEqual(blocks[-3:], ['<span class="calibre9">Table 1</span>', "| A | B |\n|---|---|\n| 1 | 2 |", "After."])
        self.assertEqual(stats, {"tables_restored": 1, "footnotes_restored": 1, "page_breaks_joined": 1, "line_wraps_joined": 0, "missing_tokens": []})

    def test_lost_placeholders_are_reported_and_kept(self) -> None:
        restored, stats = restore_layout_markup("Nothing survived.\n", self.RECORD)
        self.assertEqual(stats["missing_tokens"], ["footnote 1 (page 2)", "table 1 (page 4)"])
        self.assertIn("1 Source note.", restored)
        self.assertIn("| 1 | 2 |", restored)

    def test_page_break_join_keeps_link_targets(self) -> None:
        markdown = 'See [the note](#calibre_link-7).\n\nA loose belt could stop for ho-\n\n<a id="calibre_link-7"></a>\n\nurs when a belt slipped.\n'
        restored, stats = restore_layout_markup(markdown, {**self.RECORD, "footnotes": {}, "tables": {}})
        self.assertEqual(stats["page_breaks_joined"], 1)
        self.assertIn('A loose belt could stop for hours when a belt slipped.\n\n<a id="calibre_link-7"></a>', restored)

    def test_cjk_page_break_joins_without_a_space(self) -> None:
        record = {"tables": {}, "footnotes": {}, "page_breaks": [{"page": 2, "tail": "工厂的节奏", "head": "从未改变"}]}
        restored, stats = restore_layout_markup("每天早上工厂的节奏\n\n从未改变。\n", record)
        self.assertEqual(restored, "每天早上工厂的节奏从未改变。\n")
        self.assertEqual(stats["page_breaks_joined"], 1)

    def test_calibre_label_heading_merges_with_promoted_title(self) -> None:
        markdown = '## <span class="calibre2">Chapter 1</span>\n\n<span class="calibre4">Engines of Change</span>\n\n\xa0\n\nBody.'
        promoted, count = promote_headings(markdown, {"chapter 1": 1, "engines of change": 1})
        self.assertEqual(promoted.split("\n\n")[0], "# Chapter 1 Engines of Change")
        self.assertEqual(count, 1)


class PreparedLayout(unittest.TestCase):
    def test_fallback_draft_has_clean_flow_tables_footnotes_and_figures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, markdown = prepare_layout_book(Path(temp))
            self.assertNotIn("A HISTORY OF WORK", markdown)
            self.assertEqual(markdown.count("Engines of Change"), 1)
            self.assertEqual(markdown.count("The Night Shift"), 1)
            self.assertIsNone(re.search(r"^\d+$", markdown, re.MULTILINE), "folio left in the draft")
            self.assertIn("The ledger recorded the result.[^fn1]\n\n[^fn1]: Source: factory ledger, 1998, p. 42.\n", markdown)
            self.assertIn("could stop for hours when a belt slipped", markdown)
            self.assertNotIn("gearbox", markdown)
            self.assertRegex(markdown, r"!\[\]\(assets/original/pdf/page-0003-\d+\.png\)\n\nFigure 1.1 The assembly line.")
            self.assertIn("| Year | Units | Defects |\n|---|---|---|\n| 1998 | 1,200 | 14 |\n| 1999 | 1,450 | 9 |", markdown)
            headings = [line for line in markdown.splitlines() if line.startswith("#")]
            self.assertEqual(headings, ["# Chapter 1 Engines of Change", "## Daily Lessons", "# Chapter 2 The Night Shift"])
            conversion = load_json(project / "analysis" / "book-profile.json")["conversion"]
            self.assertEqual(conversion["pdf_layout"]["page_breaks_joined"], 1)
            self.assertEqual(conversion["layout_warnings"], [])
            self.assertEqual(conversion["pdf_layout"]["missing_characters"], 0)

    def test_page_map_marks_running_heads_and_folios_instead_of_footnotes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _markdown = prepare_layout_book(Path(temp))
            pages = load_json(project / "analysis" / "page-map.json")["pages"]
            roles = [sorted(region["role"] for region in page["regions"] if region["role"] in {"header", "page_number", "footnote"}) for page in pages]
            # Chapter openings carry a bottom folio (formerly flagged as a footnote); other
            # pages one top block holding running head and folio; page 2 also a real footnote.
            self.assertEqual(roles, [
                ["page_number"], ["footnote", "header"], ["header"], ["header"],
                ["header"], ["page_number"], ["header"], ["header"],
            ])
            furniture = [region for page in pages for region in page["regions"] if region["role"] in {"header", "page_number"}]
            self.assertTrue(all(region["risk"] == 0 and region["disposition"] == "omit_approved" for region in furniture))
            # The labels inside the page 3 figure went into its image: nothing to review.
            labels = [region for region in pages[2]["regions"] if region["role"] == "figure"]
            self.assertEqual([(region["risk"], region["disposition"], region["risk_reasons"]) for region in labels],
                             [(0, "preserve_asset", ["figure_rendered_as_image"])])
            self.assertEqual(pages[2]["layout"], "single_column")

    def test_page_map_does_not_claim_cleaning_on_a_restored_page(self) -> None:
        import fitz

        def greedy(rect, *_shape):
            return fitz.Rect(rect.x0, rect.y0, rect.x1 + 300, rect.y1 + 40)

        with tempfile.TemporaryDirectory() as temp:
            with patch("book_translation_pro.pdf_text._inner", side_effect=greedy):
                project, _markdown = prepare_layout_book(Path(temp))
            warnings = load_json(project / "source-work" / "extracted" / "pdf-layout.json")["warnings"]
            restored_pages = {
                int(re.match(r"Layout cleaning was undone on page (\d+):", warning).group(1))
                for warning in warnings if warning.startswith("Layout cleaning was undone")
            }
            self.assertIn(2, restored_pages)
            for page in load_json(project / "analysis" / "page-map.json")["pages"]:
                if page["page_index"] in restored_pages:
                    roles = {region["role"] for region in page["regions"]}
                    self.assertFalse(roles & {"header", "page_number"}, page["page_index"])

    def test_lost_source_text_is_reported_after_conversion(self) -> None:
        def lose_a_sentence(markdown, record):
            restored, stats = restore_layout_markup(markdown, record)
            return restored.replace("The ledger recorded the result.", ""), stats

        with tempfile.TemporaryDirectory() as temp:
            with patch("book_translation_pro.ingest.restore_layout_markup", side_effect=lose_a_sentence):
                project, _markdown = prepare_layout_book(Path(temp))
            conversion = load_json(project / "analysis" / "book-profile.json")["conversion"]
            self.assertEqual(conversion["pdf_layout"]["missing_characters"], len("Theledgerrecordedtheresult"))
            self.assertEqual(conversion["pdf_layout"]["words_not_found"], ["recorded", "result"])
            self.assertTrue(any("missing from the working draft" in warning for warning in conversion["layout_warnings"]))

    def test_scanned_pdf_without_text_layer_still_prepares(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            page.draw_rect(fitz.Rect(40, 40, 380, 560), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
            document.save(root / "scan.pdf")
            document.close()
            record = clean_pdf(root / "scan.pdf", root / "clean.pdf")
            self.assertEqual((record["tables"], record["footnotes"], record["figures"]), ({}, {}, []))
            self.assertTrue((root / "clean.pdf").is_file())


class DestructiveRulesNeedTwoSignals(unittest.TestCase):
    """Text that only looks like page furniture must survive cleaning."""

    def test_short_chapter_running_head_repeating_the_bare_title_is_removed(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            opener = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            opener.insert_text((40, 90), "Chapter 1 The Factory", fontsize=18)
            opener.insert_textbox(fitz.Rect(40, 120, 380, 400), SENTENCE * 6, fontsize=10)
            # One odd page carries the bare chapter title: below the repeat threshold.
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            page.insert_text((160, 30), "THE FACTORY", fontsize=8)
            page.insert_textbox(fitz.Rect(40, 60, 380, 400), SENTENCE * 6, fontsize=10)
            # The same words in small type inside the text block are not furniture.
            page.insert_text((40, 420), "The Factory", fontsize=8)
            document.save(root / "short.pdf")
            document.close()
            record = clean_pdf(root / "short.pdf", root / "clean.pdf")
            self.assertEqual([(item["page"], item["text"]) for item in record["removed"]], [(2, "THE FACTORY")])
            with fitz.open(root / "clean.pdf") as cleaned:
                self.assertIn("The Factory", cleaned[1].get_text())

    def test_poems_epigraphs_and_roman_letter_words_are_kept(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            for number in range(1, 5):
                page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
                for line in range(8):  # a stanza starting inside the top band, set at 0.9 x body
                    page.insert_text((60, 36 + line * 14), f"Stanza line {line + 1} of poem {number}, the river and the field", fontsize=9)
                page.insert_textbox(fitz.Rect(40, 200, 380, 400), SENTENCE * 4, fontsize=10)
            document[1].insert_text((60, 20), "Small epigraph above the poem", fontsize=8)
            document[2].insert_text((200, 585), "civil", fontsize=10)
            document[0].insert_text((205, 22), "2", fontsize=10)  # a lone section number
            document[3].insert_text((190, 585), "1998", fontsize=10)  # a lone year
            for index, folio in ((1, "xii"), (2, "xiii"), (3, "xiv")):  # front-matter folios advance with the page
                document[index].insert_text((360, 585), folio, fontsize=8)
            document.save(root / "poems.pdf")
            document.close()
            record = clean_pdf(root / "poems.pdf", root / "clean.pdf")
            self.assertEqual([(item["page"], item["kind"], item["text"]) for item in record["removed"]], [
                (2, "page_number", "xii"), (3, "page_number", "xiii"), (4, "page_number", "xiv"),
            ])
            self.assertEqual(record["warnings"], [])
            with fitz.open(root / "clean.pdf") as cleaned:
                text = "\n".join(page.get_text() for page in cleaned)
            for number in range(1, 5):
                self.assertIn(f"Stanza line 1 of poem {number}", text)
            for kept in ("Small epigraph above the poem", "civil", "\n2\n", "1998"):
                self.assertIn(kept, text)

    def test_numbered_labels_at_page_tops_are_not_running_heads(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            for problem in (7, 9, 12, 13):  # numbers that do not advance with the page
                page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
                page.insert_text((40, 30), f"Problem {problem}", fontsize=10)
                page.insert_textbox(fitz.Rect(40, 80, 380, 300), SENTENCE * 4, fontsize=10)
            document.save(root / "workbook.pdf")
            document.close()
            record = clean_pdf(root / "workbook.pdf", root / "clean.pdf")
            self.assertEqual(record["removed"], [])

    def test_removal_log_records_every_deleted_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_layout_pdf(root / "book.pdf")
            record = clean_pdf(root / "book.pdf", root / "clean.pdf")
            kinds = Counter(item["kind"] for item in record["removed"])
            self.assertEqual(kinds["header"] + kinds["page_number"], record["stats"]["furniture_lines_removed"])
            entries = [(item["page"], item["kind"], item["text"]) for item in record["removed"]]
            self.assertIn((2, "header", "A HISTORY OF WORK"), entries)
            self.assertIn((3, "figure_label", "gearbox"), entries)
            self.assertIn((2, "footnote", "1 Source: factory ledger, 1998, p. 42."), entries)
            self.assertIn((2, "footnote_marker", "1"), entries)
            self.assertIn((4, "table", "1,450"), entries)
            self.assertTrue(all(len(item["bbox"]) == 4 for item in record["removed"]))

    def test_page_n_of_m_footers_are_running_feet(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            for number in range(1, 5):
                page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
                page.insert_textbox(fitz.Rect(40, 80, 380, 300), SENTENCE * 4, fontsize=10)
                page.insert_text((170, 585), f"Page {number} of 120", fontsize=8)
            document.save(root / "report.pdf")
            document.close()
            record = clean_pdf(root / "report.pdf", root / "clean.pdf")
            self.assertEqual([item["text"] for item in record["removed"]], [f"Page {number} of 120" for number in range(1, 5)])

    def test_page_is_restored_when_removed_text_does_not_balance(self) -> None:
        import fitz

        def greedy(rect, *_shape):  # a redaction box that also cuts into the body text below
            return fitz.Rect(rect.x0, rect.y0, rect.x1 + 300, rect.y1 + 40)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_layout_pdf(root / "book.pdf")
            with patch("book_translation_pro.pdf_text._inner", side_effect=greedy):
                record = clean_pdf(root / "book.pdf", root / "clean.pdf")
            self.assertTrue(any(warning.startswith("Layout cleaning was undone on page 2:") for warning in record["warnings"]))
            self.assertFalse(any(item["page"] == 2 for item in record["removed"]))
            self.assertFalse(any(note["page"] == 2 for note in record["footnotes"].values()))
            with fitz.open(root / "book.pdf") as original, fitz.open(root / "clean.pdf") as cleaned:
                self.assertEqual(cleaned[1].get_text(), original[1].get_text())
            self.assertEqual(record["stats"]["pages_restored_unchanged"], len([w for w in record["warnings"] if w.startswith("Layout cleaning was undone")]))

    def test_table_whose_cells_disagree_with_the_page_is_left_as_text(self) -> None:
        import fitz

        extract = fitz.table.Table.extract

        def drop_a_cell(table):
            rows = extract(table)
            rows[2][1] = ""
            return rows

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_layout_pdf(root / "book.pdf")
            with patch.object(fitz.table.Table, "extract", drop_a_cell):
                record = clean_pdf(root / "book.pdf", root / "clean.pdf")
            self.assertEqual(record["tables"], {})
            self.assertTrue(any(warning.startswith("Table on page 4 left as text") for warning in record["warnings"]))
            with fitz.open(root / "clean.pdf") as cleaned:
                self.assertIn("1,450", cleaned[3].get_text())

    def test_headerless_tables_get_an_empty_header_row(self) -> None:
        data = [["1998", "1,200"], ["1999", "1,450"]]
        self.assertFalse(_has_header(data, []))
        self.assertEqual(_pipe_table(data, False), "|  |  |\n|---|---|\n| 1998 | 1,200 |\n| 1999 | 1,450 |")
        self.assertTrue(_has_header([["Year", "Units"], ["1998", "1,200"]], []))
        self.assertFalse(_has_header([["Apples", "red"], ["Lemons", "yellow"]], []))
        self.assertTrue(_has_header([["Fruit", "Colour"], ["Lemons", "yellow"]], [{"text": "Fruit", "flags": 16}, {"text": "Colour", "flags": 16}]))


def _span(text: str, x0: float, x1: float, *, size: float = 10.0, flags: int = 0, y0: float = 100.0) -> dict:
    return {"text": text, "size": size, "flags": flags, "bbox": (x0, y0, x1, y0 + size * 1.2)}


def _fake_line(*spans: dict) -> dict:
    return {"spans": list(spans), "dir": (1, 0), "bbox": (spans[0]["bbox"][0], spans[0]["bbox"][1], spans[-1]["bbox"][2], spans[-1]["bbox"][3])}


def _body_page(document, text: str = SENTENCE * 4):
    import fitz

    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_textbox(fitz.Rect(40, 60, 380, 300), text, fontsize=10)
    return page


class BookChapterPages(unittest.TestCase):
    """Layouts met in a real book chapter: sideways exhibits, print slugs, prose grids."""

    def test_sideways_exhibit_becomes_one_upright_figure_under_its_caption(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            _body_page(document)
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            page.insert_text((60, 520), "EXHIBIT 6.9 Cost Categories by Segment", fontsize=10, rotate=90, fontname="hebo")
            for column, x in enumerate((110, 160, 210, 260)):
                for row, y in enumerate((540, 420, 300, 180)):
                    page.insert_text((x, y), f"R{row}C{column} 1,{row}{column}0", fontsize=9, rotate=90)
                    page.draw_rect(fitz.Rect(x - 14, y - 110, x + 6, y + 4), color=(0, 0, 0))
            # An upright page with one sideways margin note is not an exhibit.
            _body_page(document).insert_text((400, 400), "SIDE NOTE", fontsize=8, rotate=90)
            document.save(root / "book.pdf")
            document.close()
            record = clean_pdf(root / "book.pdf", root / "clean.pdf")
            self.assertEqual([(item["page"], item["caption"], item["rotation"]) for item in record["figures"]],
                             [(2, "EXHIBIT 6.9 Cost Categories by Segment", 90)])
            self.assertEqual(record["captions"], {"0001": {"page": 2, "text": "EXHIBIT 6.9 Cost Categories by Segment"}})
            kinds = Counter(item["kind"] for item in record["removed"] if item["page"] == 2)
            self.assertEqual(kinds, {"figure_label": 16, "caption": 1})
            self.assertFalse(any(item["page"] == 3 for item in record["removed"]))
            self.assertEqual(record["stats"]["pages_restored_unchanged"], 0)
            with fitz.open(root / "clean.pdf") as cleaned:
                self.assertEqual(cleaned[1].get_text().split(), ["BTPCP0001Z"])
                self.assertEqual(len(cleaned[1].get_images()), 1)
                self.assertIn("SIDE NOTE", cleaned[2].get_text())
            restored, stats = restore_layout_markup("Before.\n\nBTPCP0001Z\n\n![](page-0002.png)\n\nAfter.\n", record)
            self.assertIn("EXHIBIT 6.9 Cost Categories by Segment", restored)
            self.assertNotIn("BTPCP", restored)
            self.assertEqual(stats["missing_tokens"], [])

    def test_redaction_inset_runs_across_the_line_and_is_capped_by_type_size(self) -> None:
        import fitz

        vertical = _inner(fitz.Rect(100, 300, 112, 500), (0, -1), 9)
        # Shrinking along a vertical line would leave its ends on the page.
        self.assertEqual((vertical.y0, vertical.y1), (300.5, 499.5))
        self.assertAlmostEqual(vertical.x0, 102.7)
        # A tall line (a letter stacked over a column of dots) loses only 0.3 x size.
        tall = _inner(fitz.Rect(40, 100, 300, 160), (1, 0), 10)
        self.assertEqual((tall.y0, tall.y1), (103, 157))

    def test_ruled_grid_of_paragraphs_is_left_as_text(self) -> None:
        import fitz

        long = ("Each customer segment receives a different mix of service attributes, and the cost of every "
                "attribute is traced to the activities that create it, so that managers can see which earn their keep. ")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            # A flowchart drawn as a ruled grid: step labels over paragraph boxes.
            for row, (top, bottom) in enumerate(((100, 130), (130, 400))):
                for column, x in enumerate((40, 210)):
                    page.draw_rect(fitz.Rect(x, top, x + 170, bottom), color=(0, 0, 0))
                    if row == 0:
                        page.insert_text((x + 5, top + 18), f"Step {column + 1}", fontsize=9)
                    else:
                        page.insert_textbox(fitz.Rect(x + 5, top + 5, x + 165, bottom - 5), long * 2, fontsize=9)
            document.save(root / "flow.pdf")
            document.close()
            record = clean_pdf(root / "flow.pdf", root / "clean.pdf")
            self.assertEqual(record["tables"], {})
            with fitz.open(root / "clean.pdf") as cleaned:
                self.assertIn("managers can see which", cleaned[0].get_text())

    def test_print_slugs_outside_the_trim_box_are_removed_and_logged(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            for number in range(2):
                page = document.new_page(width=480, height=660)
                page.insert_text((40, 20), f"ch06.qxd 2/2/07 3:40 PM Page {122 + number}", fontsize=8)
                page.insert_textbox(fitz.Rect(70, 90, 410, 330), SENTENCE * 4, fontsize=10)
                page.set_cropbox(fitz.Rect(30, 30, 450, 630))
            document.save(root / "slug.pdf")
            document.close()
            record = clean_pdf(root / "slug.pdf", root / "clean.pdf")
            self.assertEqual(record["print_marks"], [
                {"page": 1, "text": "ch06.qxd 2/2/07 3:40 PM Page 122"},
                {"page": 2, "text": "ch06.qxd 2/2/07 3:40 PM Page 123"},
            ])
            self.assertEqual(record["stats"]["print_marks_removed"], 2)
            flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_MEDIABOX_CLIP
            with fitz.open(root / "clean.pdf") as cleaned:
                for page in cleaned:
                    self.assertNotIn("qxd", page.get_text("text", flags=flags, clip=fitz.INFINITE_RECT()))
                    self.assertIn("operators inspected", page.get_text())

    def test_line_wraps_are_recorded_for_prose_but_not_for_verse(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = fitz.open()
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            # A centered epigraph sets each line as its own block.
            page.insert_text((40, 60), "The more you know about your customers, the better you can serve and", fontsize=10)
            page.insert_text((180, 74), "sell them.", fontsize=10)
            for index, text in enumerate(("the river runs on and on", "and the long grass bending", "under the wind")):
                page.insert_text((80, 200 + index * 14), text, fontsize=10)
            for index, text in enumerate(("beside the mill the water", "turns the wheel")):  # verse, one line per block
                page.insert_text((80, 300 + index * 14), text, fontsize=10)
            document.save(root / "wraps.pdf")
            document.close()
            record = clean_pdf(root / "wraps.pdf", root / "clean.pdf")
            self.assertEqual(record["line_wraps"], [{"tail": "er you can serve and", "head": "sell them."}])

    def test_captions_need_type_of_their_own(self) -> None:
        prose = _fake_line(_span("Exhibit 6.13 shows the cost of each segment.", 40, 300))
        self.assertFalse(_caption_candidate(prose, 10))
        self.assertTrue(_caption_candidate(_fake_line(_span("Exhibit 6.13 Segment costs", 40, 200, size=9)), 10))
        self.assertTrue(_caption_candidate(_fake_line(_span("EXHIBIT 6.13 Segment costs", 40, 200, flags=16)), 10))
        self.assertFalse(_caption_candidate(_fake_line(_span("Exhibit 6.13 Segment costs", 40, 200, size=12, flags=16)), 10))

    def test_small_capitals_join_into_words(self) -> None:
        line = _fake_line(_span("O", 40, 48, size=12), _span("N", 48.5, 55, size=9), _span("T", 60, 67, size=12), _span("ARGET", 67.2, 100, size=9))
        self.assertEqual(_line_text(line), "ON TARGET")


class BookChapterRestore(unittest.TestCase):
    """Calibre output put back together using what the PDF layout recorded."""

    def test_superscripts_are_marked_after_their_word_or_reported(self) -> None:
        superscripts = {
            "0001": {"page": 1, "text": "1", "after": "them."},
            "0002": {"page": 2, "text": "2", "after": "Baggaley,"},
            "0003": {"page": 3, "text": "4", "after": "missing"},
        }
        marked, unplaced = _mark_superscripts("*sell them.* *1*\n\nAs Baggaley,2 said in 2004.\n", superscripts)
        self.assertEqual(marked, "*sell them.*^1^\n\nAs Baggaley,^2^ said in 2004.\n")
        self.assertEqual(unplaced, ["superscript 4 (page 3)"])
        # The first digit of a year after the same word is not a marker.
        year, unplaced = _mark_superscripts("founded in 2005.\n", {"0001": {"page": 1, "text": "2", "after": "in"}})
        self.assertEqual((year, unplaced), ("founded in 2005.\n", ["superscript 2 (page 1)"]))

    def test_line_end_hyphens_follow_the_book_spelling(self) -> None:
        vocabulary = ({"customer-driven", "value-add"}, {"customer", "driven", "operators", "value", "add"})
        self.assertEqual(_hyphen_decisions("the op-", "erators checked", vocabulary), {"op-erators": "operators"})
        self.assertEqual(_hyphen_decisions("is customer-", "driven and", vocabulary),
                         {"customer-driven": "customer-driven", "customerdriven": "customer-driven"})
        # The part after the break is itself hyphenated: the break is a real hyphen.
        self.assertEqual(_hyphen_decisions("the business-", "value-add work", vocabulary),
                         {"business-value": "business-value", "businessvalue": "business-value"})
        # A glued form the book prints itself is never rewritten.
        self.assertEqual(_hyphen_decisions("the data-", "base grew", ({"data-base"}, {"database"})), {"data-base": "data-base"})
        self.assertEqual(_hyphen_decisions("Smith-", "Jones said", vocabulary), {})
        self.assertEqual(_hyphen_decisions("the end.", "next line", vocabulary), {})

    def test_hyphenation_rejoins_within_a_paragraph_only(self) -> None:
        decisions = {"op-erators": "operators", "de-fined": "defined", "value-add": "value-add", "valueadd": "value-add"}
        rejoined = _rejoin_hyphenation("the op-\nerators and *de-* *fined* the valueadd.\n\nop-\n\nerators\n", decisions)
        self.assertEqual(rejoined, "the operators and *defined* the value-add.\n\nop-\n\nerators\n")
        self.assertEqual(_join_blocks("a business-", "value-add view", {"business-value": "business-value"}), "a business-value-add view")
        self.assertEqual(_join_blocks("the op-", "erators"), "the operators")

    def test_paragraphs_split_at_a_line_wrap_are_rejoined(self) -> None:
        first = "The more you know, the better you can serve and"
        wraps = [{"tail": _normalize(first)[-20:], "head": "sell them."}]
        joined, count = _rejoin_wrapped_lines([first, "sell them.", "Unrelated paragraph.", "sell them."], wraps)
        self.assertEqual((joined, count), ([f"{first} sell them.", "Unrelated paragraph.", "sell them."], 1))
        # A heading or a table row with the same words is never a wrapped line.
        for block in (f"## {first}", f"| {first}"):
            self.assertEqual(_rejoin_wrapped_lines([block, "sell them."], wraps), ([block, "sell them."], 0))

    def test_captions_run_onto_a_paragraph_get_their_own_block(self) -> None:
        captions = ["EXHIBIT 6.11 GTI Customer Segments"]
        self.assertEqual(
            _split_trailing_captions(["Clearly, the segments differ. **EXHIBIT 6.11 GTI Customer Segments**"], captions),
            ["Clearly, the segments differ.", "**EXHIBIT 6.11 GTI Customer Segments**"],
        )
        self.assertEqual(
            _split_trailing_captions(["**EXHIBIT 6.11 GTI Customer Segments** costs rose as well."], captions),
            ["**EXHIBIT 6.11 GTI Customer Segments**", "costs rose as well."],
        )
        # A citation in the sentence is not bold; an unknown bold caption is not this figure's.
        for block in ("as shown in EXHIBIT 6.11 GTI Customer Segments", "Text. **EXHIBIT 9.9 Other Things**"):
            self.assertEqual(_split_trailing_captions([block], captions), [block])

    def test_page_break_continues_into_a_number(self) -> None:
        self.assertTrue(_continues("for 2005 actual expenditures,", "2006 projected expenditures"))
        self.assertFalse(_continues("the year ended.", "2006 was better"))
        self.assertFalse(_continues("as shown in", "Exhibit 6.2"))

    def test_style_only_calibre_spans_are_stripped(self) -> None:
        markdown = (
            '<span class="calibre3">The</span> <span class="calibre5">O</span><span class="calibre6">N</span> line '
            '<span id="calibre_link-4" class="calibre2">kept</span> **Ex**<span class="calibre1">**hibit**</span> done\n'
            '```\n<span class="calibre1">code</span>\n```\n'
        )
        stripped, removed = strip_calibre_spans(markdown)
        self.assertEqual(stripped, (
            'The ON line <span id="calibre_link-4" class="calibre2">kept</span> **Exhibit** done\n'
            '```\n<span class="calibre1">code</span>\n```\n'
        ))
        self.assertEqual(removed, 8)

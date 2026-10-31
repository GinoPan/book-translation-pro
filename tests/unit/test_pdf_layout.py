"""PDF page furniture, footnotes, figures, and tables in the extracted working draft."""

from __future__ import annotations

import re
import tempfile
from collections import Counter
import unittest
from pathlib import Path
from unittest.mock import patch

from book_translation_pro.artifacts import BTPError, load_json
from book_translation_pro.pdf_text import _has_header, _pipe_table, clean_pdf, promote_headings, restore_layout_markup
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
        self.assertEqual(stats, {"tables_restored": 1, "footnotes_restored": 1, "page_breaks_joined": 1, "missing_tokens": []})

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

    def test_page_map_does_not_claim_cleaning_on_a_restored_page(self) -> None:
        import fitz

        def greedy(rect):
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

        def greedy(rect):  # a redaction box that also cuts into the body text below
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

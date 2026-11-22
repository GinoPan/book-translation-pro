from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from book_translation_pro import figure_render
from book_translation_pro.artifacts import BTPError, validate_schema
from book_translation_pro.exhibits import boxes_in, build_inventory
from book_translation_pro.retrofit import plan_retrofit, rewrite_units

BODY = (
    "Running text of the book is set in a serif face at ten points and carries most of the characters, "
    "which is how the inventory tells body text from exhibit text."
)


def _body(page: fitz.Page, top: float, lines: int = 6) -> None:
    for row in range(lines):
        page.insert_text((72, top + 13 * row), BODY[:95], fontname="tiro", fontsize=10)


def _diagram_pdf(path: Path) -> None:
    """Two boxed labels joined by a line, and a label knocked out of an arrow by a white patch."""
    document = fitz.open()
    page = document.new_page()
    _body(page, 60)
    page.draw_rect(fitz.Rect(100, 180, 250, 220), color=(0, 0, 0), width=0.8)
    page.insert_text((112, 203), "Customer Needs", fontname="helv", fontsize=8)
    page.draw_rect(fitz.Rect(320, 180, 470, 220), color=(0, 0, 0), width=0.8)
    page.insert_text((332, 203), "Strategy Changes", fontname="helv", fontsize=8)
    page.draw_line((250, 200), (320, 200), color=(0, 0, 0), width=0.8)
    page.draw_line((100, 220), (100, 245), color=(0, 0, 0), width=0.8)
    page.draw_line((100, 245), (470, 245), color=(0, 0, 0), width=0.8)
    page.draw_line((470, 245), (470, 220), color=(0, 0, 0), width=0.8)
    page.draw_rect(fitz.Rect(253, 239, 291, 251), color=None, fill=(1, 1, 1), width=0)
    page.insert_text((256, 248), "Feedback", fontname="helv", fontsize=8)
    page.insert_text((72, 280), "EXHIBIT 1.1 Planning Loop", fontname="hebo", fontsize=9)
    _body(page, 380)
    document.save(path)
    document.close()


def _table_pdf(path: Path, total: str) -> None:
    document = fitz.open()
    page = document.new_page()
    _body(page, 60)
    page.insert_text((72, 160), "EXHIBIT 2.1 Value Stream Costs", fontname="hebo", fontsize=9)
    page.insert_text((300, 180), "Amount", fontname="hebo", fontsize=8)
    page.insert_text((380, 180), "Share", fontname="hebo", fontsize=8)
    rows = [("Material costs", "111,431", "50.0%"), ("Employee costs", "49,515", "30.0%"),
            ("Other costs", "3,290", "20.0%"), ("TOTAL COSTS", total, "100.0%")]
    for index, (label, amount, share) in enumerate(rows):
        y = 194 + 12 * index
        page.insert_text((80, y), label, fontname="helv", fontsize=8)
        page.insert_text((300, y), amount, fontname="helv", fontsize=8)
        page.insert_text((380, y), share, fontname="helv", fontsize=8)
    _body(page, 270)
    document.save(path)
    document.close()


class InventoryTests(unittest.TestCase):
    def test_boxed_and_knocked_out_labels_make_a_diagram(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _diagram_pdf(pdf)
            inventory = build_inventory(pdf)
            validate_schema(inventory, "exhibit-inventory.schema.json")
            (exhibit,) = inventory["exhibits"]
            self.assertEqual(exhibit["classification"], "diagram")
            self.assertEqual(exhibit["orientation"], "above_caption")
            self.assertEqual(sorted(label["text"] for label in exhibit["labels"]),
                             ["Customer Needs", "Feedback", "Strategy Changes"])

    def test_a_line_or_an_open_path_is_not_a_box(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.draw_line((100, 100), (300, 160), color=(0, 0, 0), width=2)
        page.draw_polyline([(100, 200), (200, 200), (200, 260)], color=(0, 0, 0), width=1)
        page.draw_rect(fitz.Rect(320, 100, 420, 150), color=(0, 0, 0), width=1)
        boxes = boxes_in(page, page.rect)
        self.assertEqual([tuple(round(v) for v in box) for box in boxes], [(320, 100, 420, 150)])

    def test_table_is_rebuilt_and_its_total_checks_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _table_pdf(pdf, "164,236")
            (exhibit,) = build_inventory(pdf)["exhibits"]
            self.assertEqual(exhibit["classification"], "table_grid")
            table = exhibit["table"]
            self.assertEqual(table["header"], ["", "Amount", "Share"])
            self.assertEqual(table["rows"][0], ["Material costs", "111,431", "50.0%"])
            self.assertEqual(table["rows"][-1], ["TOTAL COSTS", "164,236", "100.0%"])
            self.assertEqual(exhibit["checks"]["characters_missing"], "")
            self.assertEqual({t["column"]: t["verdict"] for t in exhibit["checks"]["totals"]},
                             {"Amount": "exact", "Share": "exact"})

    def test_misprinted_total_is_reported_not_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _table_pdf(pdf, "150,000")
            (exhibit,) = build_inventory(pdf)["exhibits"]
            totals = {t["column"]: t for t in exhibit["checks"]["totals"]}
            self.assertEqual(totals["Amount"]["verdict"], "mismatch")
            self.assertEqual(totals["Amount"]["stated"], 150000)
            self.assertEqual(totals["Amount"]["sum_of_rows"], 164236)
            # The printed value stays in the table; correction is an editorial step.
            self.assertEqual(exhibit["table"]["rows"][-1][1], "150,000")


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            figure_render.font_files()
        except BTPError:
            self.skipTest("no CJK font installed")
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.pdf = self.root / "book.pdf"
        _diagram_pdf(self.pdf)
        (self.exhibit,) = build_inventory(self.pdf)["exhibits"]
        names = {"Customer Needs": "客户需求", "Strategy Changes": "战略变化", "Feedback": "反馈"}
        self.translations = [names[label["text"]] for label in self.exhibit["labels"]]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_labels_are_replaced_and_the_old_knockout_is_removed(self) -> None:
        report = figure_render.render_diagram(self.pdf, self.exhibit, self.translations, self.root / "out.png")
        self.assertTrue(report["passed"], report["problems"])
        self.assertEqual(report["replaced"], 3)
        self.assertEqual(report["patches"]["removed"], 1)
        self.assertTrue((self.root / "out.png").is_file())

    def test_keeping_the_old_knockout_leaves_a_cut_line_and_fails(self) -> None:
        def keep_all(_page, rects):
            return [], list(rects)

        with mock.patch.object(figure_render, "remove_patches", keep_all):
            report = figure_render.render_diagram(self.pdf, self.exhibit, self.translations, self.root / "out.png")
        self.assertFalse(report["passed"])
        self.assertTrue(any(problem.startswith("line cut without text") for problem in report["problems"]))

    def test_label_count_mismatch_fails_loudly(self) -> None:
        with self.assertRaises(BTPError):
            figure_render.render_diagram(self.pdf, self.exhibit, self.translations[:-1], self.root / "out.png")

    def test_stale_inventory_fails_loudly(self) -> None:
        stale = dict(self.exhibit, labels=[dict(label, text=label["text"] + "s") for label in self.exhibit["labels"]])
        with self.assertRaises(BTPError):
            figure_render.render_diagram(self.pdf, stale, self.translations, self.root / "out.png")


def _units(*texts: str) -> list[dict]:
    return [{"id": f"unit-{n:04d}", "text": text, "lines": text.split("\n")} for n, text in enumerate(texts, 1)]


DIAGRAM = {
    "key": "1.1", "label": "EXHIBIT", "part": 1, "parts": 1, "page": 12, "classification": "diagram", "orientation": "above_caption",
    "caption": "EXHIBIT 1.1 Planning Loop", "reason": "3 label(s) on vector line art",
    "labels": [{"text": "Customer Needs", "keep": False, "lines": 1, "bbox": [0, 0, 1, 1]},
               {"text": "Strategy Changes", "keep": False, "lines": 1, "bbox": [0, 0, 1, 1]},
               {"text": "Feedback", "keep": False, "lines": 1, "bbox": [0, 0, 1, 1]}],
}
PROSE_BEFORE = "Strategy is fed by weekly results and by the needs customers express."
PROSE_AFTER = "The feedback loop closes when customer needs change."
FRAGMENTS = (
    '<span class="calibre3">**Cu**</span><span class="calibre3">**stomer**</span> <span class="calibre3">**Strategy**</span>'
)


class RetrofitTests(unittest.TestCase):
    def _plan(self, units: list[dict], exhibit: dict = DIAGRAM) -> list[dict]:
        with tempfile.TemporaryDirectory() as directory:
            return plan_retrofit(Path(directory), units, {"exhibits": [exhibit]})

    def test_fragments_become_image_and_label_block_and_prose_stays(self) -> None:
        units = _units(
            "\n\n".join([PROSE_BEFORE, "**12** Lean Accounting", FRAGMENTS, "**Needs** **Changes**"]) + "\n",
            "\n\n".join(["**EXHIBIT 1.1** **Planning Loop**", "**Feedback** ch01_4772.qxd 2/2/07 3:39 PM Page 12",
                         PROSE_AFTER]) + "\n",
        )
        (decision,) = self._plan(units)
        self.assertEqual(decision["status"], "replace")
        self.assertEqual(decision["coverage"], 1.0)
        rewritten = rewrite_units(units, [decision])
        first, second = rewritten[0], rewritten[1]
        # Prose and the running folio on either side survive verbatim.
        self.assertIn(PROSE_BEFORE, first)
        self.assertIn("**12** Lean Accounting", first)
        self.assertNotIn("Cu**", first)
        self.assertNotIn("**Needs**", first)
        self.assertIn(PROSE_AFTER, second)
        # The print stamp glued to a fragment is kept exactly, underscore included.
        self.assertIn("\nch01_4772.qxd 2/2/07 3:39 PM Page 12\n", second)
        self.assertNotIn("**Feedback**", second)
        # Figure above its caption: image and label block come first.
        self.assertTrue(second.startswith("![](assets/original/images/exhibit-1-1.png)\n\n"
                                          '<!-- btp:figure-labels exhibit="1.1" -->\n'
                                          "1. Customer Needs\n2. Strategy Changes\n3. Feedback\n"
                                          "<!-- /btp:figure-labels -->\n\n**EXHIBIT 1.1**"))

    def test_prose_between_caption_and_fragments_stops_the_walk(self) -> None:
        units = _units("\n\n".join([FRAGMENTS, "**Needs** **Changes** **Feedback**",
                                    PROSE_BEFORE, "**EXHIBIT 1.1** **Planning Loop**", PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units)
        self.assertEqual(decision["status"], "unmatched")
        self.assertEqual(decision["coverage"], 0.0)
        self.assertEqual(rewrite_units(units, [decision]), {})

    def test_a_short_line_of_label_words_plus_one_other_word_is_kept(self) -> None:
        units = _units("\n\n".join(["**EXHIBIT 1.1** **Planning Loop**", "Customer Needs matter.",
                                    FRAGMENTS, "**Needs** **Changes** **Feedback**"]) + "\n")
        (decision,) = self._plan(units)
        self.assertEqual(decision["status"], "unmatched")
        self.assertEqual(rewrite_units(units, [decision]), {})

    def test_walk_does_not_reach_beyond_the_neighbouring_unit(self) -> None:
        units = _units(FRAGMENTS + "\n\n**Needs** **Changes** **Feedback**\n", "**7** Lean Accounting\n",
                       "**8** Lean Accounting\n", "**EXHIBIT 1.1** **Planning Loop**\n")
        (decision,) = self._plan(units)
        self.assertEqual(decision["status"], "unmatched")

    def test_second_run_finds_the_exhibit_already_retrofitted(self) -> None:
        units = _units("\n\n".join(["**EXHIBIT 1.1** **Planning Loop**", FRAGMENTS,
                                    "**Needs** **Changes** **Feedback**"]) + "\n")
        (decision,) = self._plan(units)
        text = rewrite_units(units, [decision])[0]
        (again,) = self._plan(_units(text))
        self.assertEqual(again["status"], "already_retrofitted")

    def test_rebuilt_table_replaces_its_fragment_line(self) -> None:
        table = {
            "key": "2.1", "label": "EXHIBIT", "part": 1, "parts": 1, "page": 3, "classification": "table_grid",
            "orientation": "below_caption", "caption": "EXHIBIT 2.1 Costs", "reason": "rebuilt",
            "table": {"mode": "grid", "header": ["", "Amount"], "meta": [], "continues": False,
                      "rows": [["Material costs", "$111,431"], ["TOTAL COSTS", "$111,431"]], "numeric_columns": [1]},
        }
        units = _units("\n\n".join(["**EXHIBIT 2.1** **Costs**",
                                    "Amount Material costs \\$111,431 TOTAL COSTS \\$111,431", PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units, table)
        self.assertEqual(decision["status"], "replace")
        text = rewrite_units(units, [decision])[0]
        self.assertIn("**EXHIBIT 2.1** **Costs**\n\n<!-- btp:exhibit-table exhibit=\"2.1\" -->\n\n"
                      "|  | Amount |\n|---|---|\n| Material costs | \\$111,431 |\n| TOTAL COSTS | \\$111,431 |\n\n"
                      "<!-- /btp:exhibit-table -->", text)
        self.assertNotIn("Amount Material costs", text)
        self.assertIn(PROSE_AFTER, text)

    def test_untreated_exhibits_are_left_alone(self) -> None:
        raster = dict(DIAGRAM, classification="raster", reason="embedded picture")
        units = _units("**EXHIBIT 1.1** **Planning Loop**\n\n" + FRAGMENTS + "\n")
        (decision,) = self._plan(units, raster)
        self.assertEqual(decision["status"], "untouched")


if __name__ == "__main__":
    unittest.main()

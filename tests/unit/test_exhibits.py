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


def _table_pdf(path: Path, total: str, heading: str | None = None) -> None:
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
    if heading:
        # A section heading right under the table, set larger than the body.
        page.insert_text((72, 252), heading, fontname="hebo", fontsize=12)
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

    def test_a_diagram_on_a_sideways_page_is_flagged_before_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _diagram_pdf(pdf)
            # The same page, but read through an upright copy of a sideways page.
            with mock.patch("book_translation_pro.exhibits.upright_page",
                            side_effect=lambda document, number: (document[number - 1], -90, None)):
                (exhibit,) = build_inventory(pdf)["exhibits"]
            self.assertEqual((exhibit["classification"], exhibit["rotation"]), ("uncertain", -90))
            self.assertIn("sideways page", exhibit["reason"])
            # Upright, it is a diagram as before.
            (exhibit,) = build_inventory(pdf)["exhibits"]
            self.assertEqual(exhibit["classification"], "diagram")

    def test_two_exhibits_with_one_number_on_a_page_are_parts_one_and_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _diagram_pdf(pdf)
            document = fitz.open(pdf)
            document[0].insert_text((72, 560), "TABLE 1.1 Costs by Value Stream", fontname="hebo", fontsize=9)
            page = document.new_page()
            page.insert_text((72, 300), "EXHIBIT 1.1 Continued", fontname="hebo", fontsize=9)
            document.saveIncr()
            document.close()
            inventory = build_inventory(pdf)
            self.assertEqual([(e["page"], e["part"], e["parts"]) for e in inventory["exhibits"]],
                             [(1, 1, 3), (1, 2, 3), (2, 3, 3)])

    def test_a_caption_title_on_two_lines_keeps_its_second_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _diagram_pdf(pdf)
            document = fitz.open(pdf)
            document[0].insert_text((72, 291), "of the Business", fontname="hebo", fontsize=9)
            document.saveIncr()
            document.close()
            (exhibit,) = build_inventory(pdf)["exhibits"]
            self.assertEqual(exhibit["caption"], "EXHIBIT 1.1 Planning Loop")
            self.assertEqual(exhibit["caption_tail"], "of the Business")
            self.assertNotIn("of the Business", [label["text"] for label in exhibit["labels"]])
            # A caption on one line has no second line.
            _diagram_pdf(pdf)
            (exhibit,) = build_inventory(pdf)["exhibits"]
            self.assertNotIn("caption_tail", exhibit)

    def test_prose_citing_an_exhibit_at_the_start_of_a_line_is_not_a_caption(self) -> None:
        from book_translation_pro.exhibits import _captions
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 100), "The basic structure of the planning loop is presented in", fontname="tiro", fontsize=11)
        page.insert_text((72, 114), "Exhibit 1.1. As can be seen, it starts", fontname="tiro", fontsize=11)
        # A real caption right under an unfinished line, but in its own face.
        page.insert_text((72, 200), "The figure below shows the loop as it runs in the", fontname="tiro", fontsize=11)
        page.insert_text((72, 212), "EXHIBIT 1.2 Planning Loop", fontname="hebo", fontsize=9)
        # A label of the figure, narrower than the caption under it, is not running text.
        page.insert_text((72, 300), "Feedback", fontname="hebo", fontsize=9)
        page.insert_text((72, 310), "EXHIBIT 1.3 Planning Loop", fontname="hebo", fontsize=9)
        self.assertEqual([caption["key"] for caption in _captions(page)], ["1.2", "1.3"])

    def test_a_title_set_larger_than_the_body_is_a_label_but_body_text_is_not(self) -> None:
        from book_translation_pro.exhibits import label_lines
        document = fitz.open()
        page = document.new_page()
        page.insert_text((100, 120), "Value Stream", fontname="hebo", fontsize=14)    # title in the figure
        page.insert_text((100, 140), "Customer Needs", fontname="helv", fontsize=8)   # ordinary label
        page.insert_text((100, 160), "the running text", fontname="tiro", fontsize=10)  # body size: prose
        page.insert_text((100, 175), "a touch larger", fontname="tiro", fontsize=10.3)  # still body-like
        texts = [line["text"] for line in label_lines(page, fitz.Rect(90, 100, 400, 190), fitz.Rect(0, 0, 0, 0), 10.0)]
        self.assertEqual(texts, ["Value Stream", "Customer Needs"])

    def test_each_list_item_is_one_label_with_its_wrapped_lines(self) -> None:
        from book_translation_pro.exhibits import group_labels

        def line(text: str, x: float, y: float) -> dict:
            return {"text": text, "bbox": fitz.Rect(x, y, x + 120, y + 8), "size": 7.5, "font": "Optima",
                    "color": 0, "dir": (1, 0)}

        lines = [line("• Use measures that can be understood", 100, 100), line("by all employees.", 105, 109),
                 line("• Use measures that show employees", 100, 118), line("how their efforts drive", 105, 127),
                 line("overall success.", 105, 136),
                 line("• Provide information when needed", 100, 145),          # aligned bullet: a new item
                 line("FR4—Achieve FR1 through FR3 in spite of", 100, 170), line("variation.", 124, 179),
                 line("FR5—Identify a problem condition", 100, 188),
                 line("Value Stream", 100, 210), line("Costs", 100, 219)]      # an ordinary two-line label
        texts = [g["text"] for g in group_labels(lines, [])]
        self.assertEqual(texts, ["• Use measures that can be understood by all employees.",
                                 "• Use measures that show employees how their efforts drive overall success.",
                                 "• Provide information when needed",
                                 "FR4—Achieve FR1 through FR3 in spite of variation.",
                                 "FR5—Identify a problem condition", "Value Stream Costs"])

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

    def test_a_heading_over_several_columns_does_not_join_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            document = fitz.open()
            page = document.new_page()
            _body(page, 60)
            page.insert_text((72, 150), "EXHIBIT 7.4 Plant-wide Income Statement", fontname="hebo", fontsize=9)
            page.insert_text((215, 166), "VALUE STREAMS", fontname="helv", fontsize=8)   # over Motors and Systems
            for x, word in ((210, "Motors"), (262, "Systems"), (330, "Later")):           # Later: no data yet
                page.insert_text((x, 178), word, fontname="helv", fontsize=8)
            rows = [("Sales", "326,250", "748,894"), ("Material costs", "111,431", "232,774"),
                    ("Conversion costs", "57,628", "70,406"), ("TOTAL COSTS", "169,059", "303,180")]
            for n, (label, a, b) in enumerate(rows):
                y = 192 + 12 * n
                page.insert_text((80, y), label, fontname="helv", fontsize=8)
                page.insert_text((210, y), a, fontname="helv", fontsize=8)
                page.insert_text((262, y), b, fontname="helv", fontsize=8)
            _body(page, 270)
            document.save(pdf)
            document.close()
            (exhibit,) = build_inventory(pdf)["exhibits"]
            table = exhibit["table"]
            self.assertEqual(table["header"], ["", "Motors", "Systems", "Later"])
            self.assertIn(["VALUE STREAMS"], table["meta"])
            self.assertEqual(table["rows"][1], ["Material costs", "111,431", "232,774", ""])
            self.assertEqual(exhibit["checks"]["merged_cells"], [])
            self.assertEqual(exhibit["checks"]["characters_missing"], "")

    def test_section_heading_under_the_table_stays_out_of_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _table_pdf(pdf, "164,236", heading="2.2 EASY TO AGREE WITH")
            (exhibit,) = build_inventory(pdf)["exhibits"]
            self.assertEqual(exhibit["classification"], "table_grid")
            cells = [cell for row in exhibit["table"]["rows"] for cell in row]
            self.assertNotIn("EASY", " ".join(cells))
            self.assertEqual(exhibit["table"]["rows"][-1], ["TOTAL COSTS", "164,236", "100.0%"])
            self.assertLess(exhibit["region"][3], 252 - 12)

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


def _rotated_chart_pdf(path: Path) -> None:
    """A bar chart: a y-axis title reading upward, bar names slanted at 45° hanging from the
    x-axis (one of them a code), horizontal tick labels, and a horizontal title."""
    document = fitz.open()
    page = document.new_page()
    _body(page, 60)
    page.draw_line((100, 150), (100, 300), color=(0, 0, 0), width=0.8)
    page.draw_line((100, 300), (400, 300), color=(0, 0, 0), width=0.8)
    for x, top in ((140, 200), (220, 230), (300, 260)):
        page.draw_rect(fitz.Rect(x, top, x + 30, 300), color=(0, 0, 0), fill=(0.6, 0.6, 0.6), width=0.5)
    page.insert_text((80, 304), "0%", fontname="helv", fontsize=8)
    page.insert_text((76, 154), "50%", fontname="helv", fontsize=8)
    page.insert_text((180, 145), "Share of firms", fontname="helv", fontsize=9)
    # Reading upward, centred on the axis (morph turns in the PDF's upward y: +90 reads up).
    origin = fitz.Point(70, 245)
    page.insert_text(origin, "Percent", fontname="helv", fontsize=8, morph=(origin, fitz.Matrix(90)))
    for name, x in (("Port security", 155), ("ATON", 235), ("Search and rescue", 315)):
        length = fitz.get_text_length(name, fontname="helv", fontsize=8)
        end = fitz.Point(x, 308)  # just under the axis
        start = fitz.Point(end.x - length * 0.7071, end.y + length * 0.7071)
        page.insert_text(start, name, fontname="helv", fontsize=8, morph=(start, fitz.Matrix(45)))
    page.insert_text((72, 380), "EXHIBIT 3.1 Rotated Labels", fontname="hebo", fontsize=9)
    _body(page, 420)
    document.save(path)
    document.close()


class RotatedLabelTests(unittest.TestCase):
    NAMES = {"Share of firms": "企业占比", "Percent": "百分比", "Port security": "港口安全",
             "Search and rescue": "搜索与救援"}

    def setUp(self) -> None:
        try:
            figure_render.font_files()
        except BTPError:
            self.skipTest("no CJK font installed")
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.pdf = self.root / "book.pdf"
        _rotated_chart_pdf(self.pdf)
        inventory = build_inventory(self.pdf)
        validate_schema(inventory, "exhibit-inventory.schema.json")
        (self.exhibit,) = inventory["exhibits"]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_rotated_labels_make_a_diagram_with_their_angles(self) -> None:
        self.assertEqual(self.exhibit["classification"], "diagram")
        angles = {label["text"]: label.get("angle", 0) for label in self.exhibit["labels"]}
        self.assertEqual(angles["Percent"], -90)
        self.assertEqual(angles["Port security"], -45)
        self.assertEqual(angles["ATON"], -45)          # a slanted label of its own, not joined to a neighbour
        self.assertEqual(angles["Share of firms"], 0)

    def test_rotated_labels_are_redrawn_at_their_angle_and_kept_codes_survive(self) -> None:
        translations = [self.NAMES.get(label["text"], label["text"]) for label in self.exhibit["labels"]]
        captured: dict[str, list] = {}
        original = fitz.Page.get_pixmap

        def capture(page, *args, **kwargs):
            captured["lines"] = [(tuple(round(v, 2) for v in line["dir"]), "".join(c["c"] for s in line["spans"] for c in s["chars"]))
                                 for block in page.get_text("rawdict")["blocks"] for line in block.get("lines", [])]
            return original(page, *args, **kwargs)

        with mock.patch.object(fitz.Page, "get_pixmap", autospec=True, side_effect=capture):
            report = figure_render.render_diagram(self.pdf, self.exhibit, translations, self.root / "out.png")
        self.assertTrue(report["passed"], report["problems"])
        rows = {row["source"]: row for row in report["rows"]}
        self.assertEqual(rows["Percent"]["anchor"], "center")
        self.assertEqual(rows["Port security"]["anchor"], "end")  # hangs from the axis
        drawn = dict((text, direction) for direction, text in captured["lines"])
        # The vertical title goes into Chinese stacked upright, read top to bottom.
        stacked = [(direction, text) for direction, text in captured["lines"] if text in "百分比"]
        self.assertEqual([text for _d, text in stacked], ["百", "分", "比"])
        self.assertTrue(all(direction == (1.0, 0.0) for direction, _t in stacked))
        self.assertEqual(rows["Percent"]["lines"], 1)
        # A slanted bar name keeps its slant.
        self.assertEqual(drawn.get("港口安全"), (0.71, -0.71))
        self.assertIn("ATON", drawn)                     # the neighbouring code is not erased
        self.assertFalse({"Percent", "Port security", "Search and rescue"} & set(drawn))

    def test_two_upward_lines_of_one_title_group_but_a_distant_one_does_not(self) -> None:
        from book_translation_pro.exhibits import group_labels, label_lines
        document = fitz.open()
        page = document.new_page()
        # Set at 10.5 pt leading for 8.5 pt type, as a book sets two lines of one title.
        for text, x, middle in (("Planning and Tactical", 60, 200), ("Processes", 70.5, 200), ("Support", 140, 200)):
            length = fitz.get_text_length(text, fontname="helv", fontsize=8.5)
            origin = fitz.Point(x, middle + length / 2)
            page.insert_text(origin, text, fontname="helv", fontsize=8.5, morph=(origin, fitz.Matrix(90)))
        lines = label_lines(page, fitz.Rect(40, 100, 300, 300), fitz.Rect(0, 0, 0, 0), 11.0)
        self.assertEqual([g["text"] for g in group_labels(lines, [])], ["Planning and Tactical Processes", "Support"])

    def test_a_rotated_translation_longer_than_its_label_shrinks_or_fails_loudly(self) -> None:
        long = "这是一个远远长于原标签的译文，无法放进原来的位置"
        translations = [long if label["text"] == "Percent" else self.NAMES.get(label["text"], label["text"])
                        for label in self.exhibit["labels"]]
        report = figure_render.render_diagram(self.pdf, self.exhibit, translations, self.root / "out.png")
        self.assertFalse(report["passed"])
        self.assertTrue(any("does_not_fit" in problem for problem in report["problems"]), report["problems"])


def _row_box_pdf(path: Path) -> None:
    """Two stacked labels side by side in one box, an underlined label, a slanted arrow."""
    document = fitz.open()
    page = document.new_page()
    _body(page, 60)
    page.draw_rect(fitz.Rect(60, 150, 400, 205), color=(0, 0, 0), width=0.8)
    page.insert_text((70, 170), "Cost", fontname="helv", fontsize=8)
    page.insert_text((70, 182), "Throughput Time", fontname="helv", fontsize=8)
    page.insert_text((300, 170), "$900,000", fontname="helv", fontsize=8)
    page.insert_text((300, 182), "23 minutes", fontname="helv", fontsize=8)
    page.insert_text((160, 199), "Quality Rates", fontname="helv", fontsize=8)
    width = fitz.get_text_length("Quality Rates", fontname="helv", fontsize=8)
    page.draw_line((160, 200.5), (160 + width, 200.5), color=(0, 0, 0), width=0.4)
    page.draw_line((300, 230), (380, 260), color=(0, 0, 0), width=0.8)
    page.insert_text((72, 290), "EXHIBIT 2.4 Comparison", fontname="hebo", fontsize=9)
    _body(page, 390)
    document.save(path)
    document.close()


class RendererRuleTests(unittest.TestCase):
    """The placement rules, each with the look-alike it must leave alone."""

    def test_a_path_of_several_rectangles_is_several_boxes(self) -> None:
        document = fitz.open()
        page = document.new_page()
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(100, 100, 160, 120))
        shape.draw_rect(fitz.Rect(200, 100, 260, 120))
        shape.finish(color=(0, 0, 0), width=0.8)
        shape.commit()
        boxes = boxes_in(page, fitz.Rect(50, 50, 400, 300))
        self.assertEqual(sorted(tuple(round(v) for v in b) for b in boxes),
                         [(100, 100, 160, 120), (200, 100, 260, 120)])

    def test_a_curve_is_followed_not_cut_by_its_chord(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.draw_circle((200, 200), 50, color=(0, 0, 0), width=0.8)
        pieces, _masks, _cover = figure_render.line_art(page, fitz.Rect(100, 100, 300, 300))
        distances = [abs(p - fitz.Point(200, 200)) for a, b, _o in pieces for p in (a, b, (a + b) / 2)]
        self.assertGreater(min(distances), 49.0)
        # A label inside the circle does not touch it.
        points = figure_render.visible_points(figure_render.line_art(page, fitz.Rect(100, 100, 300, 300)))
        self.assertFalse(figure_render.hits(fitz.Rect(170, 190, 230, 210), points))

    def test_a_filled_shape_hides_the_line_under_it_and_a_white_background_is_not_a_line(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.draw_rect(fitz.Rect(40, 40, 400, 400), color=None, fill=(1, 1, 1), width=0)  # page panel
        page.draw_line((50, 200), (350, 200), color=(0, 0, 0), width=0.8)
        page.draw_rect(fitz.Rect(150, 190, 250, 210), color=(0, 0, 0), fill=(0.8, 0.8, 0.8), width=0.8)
        points = figure_render.visible_points(figure_render.line_art(page, fitz.Rect(30, 30, 410, 410)))
        self.assertFalse(figure_render.hits(fitz.Rect(160, 195, 240, 205), points))   # under the box
        self.assertTrue(figure_render.hits(fitz.Rect(60, 195, 140, 205), points))     # the line itself
        self.assertFalse(figure_render.hits(fitz.Rect(38, 100, 45, 300), points))     # panel edge

    def test_forced_breaks_bullets_and_lists(self) -> None:
        self.assertEqual(figure_render.break_units("成本||吞吐时间"), ["成本", "\n", "吞吐时间"])
        self.assertEqual(figure_render.break_units("• 股东价值|• 结果导向"), ["• 股东价值", "• 结果导向"])
        self.assertEqual(figure_render.list_breaks(["• Customer value", "• Results"], "• 客户价值|• 结果导向"),
                         "• 客户价值||• 结果导向")
        # Not a bulleted list in the source: left as the translator marked it.
        self.assertEqual(figure_render.list_breaks(["Customer value •", "Results"], "客户价值|• 结果"), "客户价值|• 结果")

    def test_balanced_lines_match_trying_every_split(self) -> None:
        from itertools import combinations
        try:
            font = fitz.Font(fontfile=figure_render.font_files()[0])
        except BTPError:
            self.skipTest("no CJK font installed")
        units = figure_render.break_units("再实施 PS2。|PS1 将 FR1 实现到目标水平。|FR2 随 PS1 而变化。|PS2 将 FR2 实现到目标水平。")
        for count, lines in enumerate(figure_render._balanced(units, 10 ** 6, font, 9), 1):
            brute = min(max(font.text_length(figure_render.joined(units[b[i]:b[i + 1]]), 9) for i in range(count))
                        for cuts in combinations(range(1, len(units)), count - 1) for b in [(0, *cuts, len(units))])
            self.assertAlmostEqual(max(font.text_length(line, 9) for line in lines), brute, places=3)

    def test_underline_goes_with_its_words_but_a_rule_or_box_edge_stays(self) -> None:
        line = {"bbox": fitz.Rect(70, 233, 120, 242), "size": 8}
        group = {"lines": [line]}
        P = fitz.Point
        underline = (P(70, 241.5), P(119, 241.5), 3)
        box_edge = (P(60, 242), P(400, 242), 4)     # runs past the words
        stem = (P(90, 230), P(90, 250), 5)          # vertical
        shaft_end = (P(94, 241.5), P(96.2, 241.5), 6)  # the top of an arrow shaft starting under the words
        found = figure_render.underlines_of(group, [underline, box_edge, stem, shaft_end])
        self.assertEqual([(round(r.x0), round(r.x1)) for r in found], [(70, 119)])

    def test_items_one_per_source_line_keep_their_rows(self) -> None:
        try:
            font = fitz.Font(fontfile=figure_render.font_files()[0])
        except BTPError:
            self.skipTest("no CJK font installed")

        def group(rows: list[tuple[float, float, str]]) -> dict:
            lines = [{"bbox": fitz.Rect(x0, y, x1, y + 3.8), "size": 3.8, "text": text} for x0, x1, y, text in
                     [(x0, x1, 100 + 5.08 * i, text) for i, (x0, x1, text) in enumerate(rows)]]
            return {"lines": lines, "size": 3.8, "bbox": lines[0]["bbox"]}

        points = figure_render.PointGrid([])
        # A table column at 5.08 pt pitch: each item on its own source row, left edges snapped.
        steps = group([(60, 90, "Receive request"), (60.8, 95, "Maintain open req file"), (60, 99, "Review account no")])
        plan = figure_render.plan_rows(steps, "接收请求||维护未结请购文件||审核账号", font, 60, fitz.TEXT_ALIGN_CENTER, points)
        self.assertEqual(plan["status"], "ok")
        tops = [round(row.y0 + figure_render.INK_MARGIN * plan["size"], 2) for row in plan["rows"]]
        self.assertEqual([round(b - a, 2) for a, b in zip(tops, tops[1:])], [5.08, 5.08])
        self.assertEqual({round(row.x0, 2) for row in plan["rows"]}, {60.0})   # left, snapped, though "centre" was asked
        # A column of figures, centred in the source, stays centred.
        figures = group([(100, 140, "$900,000"), (104, 136, "23 minutes"), (115, 125, "2X")])
        plan = figure_render.plan_rows(figures, "$900,000||23 分钟||2X", font, 60, fitz.TEXT_ALIGN_LEFT, points)
        centres = {round((row.x0 + row.x1) / 2, 1) for row in plan["rows"]}
        self.assertEqual(centres, {120.0})
        # Item count differing from the line count: not row-locked.
        self.assertIsNone(figure_render.plan_rows(steps, "接收请求||维护未结请购文件", font, 60, fitz.TEXT_ALIGN_LEFT, points))

    def test_tightly_set_list_items_do_not_clash_but_overlapping_text_does(self) -> None:
        font = None
        try:
            font = fitz.Font(fontfile=figure_render.font_files()[0])
        except BTPError:
            self.skipTest("no CJK font installed")
        rect, size = fitz.Rect(100, 100, 300, 140), 7.5
        first = figure_render.ink_boxes(["希望为实现目标作出贡献"], rect, 100.0, size, 0, font)[0]
        tight = figure_render.ink_boxes(["被赋予决策权"], rect, 108.0, size, 0, font)[0]      # 8 pt pitch
        into = figure_render.ink_boxes(["接受交叉培训"], rect, 105.0, size, 0, font)[0]      # 5 pt pitch
        self.assertEqual(figure_render.clashes([(1, first, size), (2, tight, size)]), [])
        self.assertEqual(figure_render.clashes([(1, first, size), (3, into, size)]), [(1, 3)])

    def test_the_top_of_a_box_narrower_than_its_title_is_not_an_underline(self) -> None:
        line = {"bbox": fitz.Rect(70, 233, 120, 242), "size": 8}
        P = fitz.Point
        top, left, right = (P(74, 241.5), P(110, 241.5), 3), (P(74, 241.5), P(74, 300), 3), (P(110, 241.5), P(110, 300), 3)
        self.assertEqual(figure_render.underlines_of({"lines": [line]}, [top, left, right]), [])
        # An underline that an arrow merely touches at one end is still an underline.
        underline, arrow = (P(70, 241.5), P(119, 241.5), 4), (P(119, 241.5), P(119, 280), 5)
        found = figure_render.underlines_of({"lines": [line]}, [underline, arrow])
        self.assertEqual([(round(r.x0), round(r.x1)) for r in found], [(70, 119)])

    def test_an_underline_is_painted_out_in_the_colour_under_it(self) -> None:
        strip = fitz.Rect(100, 120, 140, 121)
        page_panel = (fitz.Rect(0, 0, 400, 400), 1, (1.0, 1.0, 1.0))
        grey_box = (fitz.Rect(90, 100, 200, 140), 5, (0.8, 0.8, 0.8))
        elsewhere = (fitz.Rect(300, 300, 350, 350), 9, (0.2, 0.2, 0.2))
        self.assertEqual(figure_render.ground_color([page_panel, grey_box, elsewhere], strip), (0.8, 0.8, 0.8))
        self.assertEqual(figure_render.ground_color([elsewhere], strip), (1, 1, 1))

    def test_a_knockout_hides_an_earlier_line_but_a_box_background_does_not(self) -> None:
        P = fitz.Point
        group = {"bbox": fitz.Rect(100, 100, 140, 108), "size": 8}
        arrow = (P(80, 104), P(160, 104), 1)                       # drawn first
        patch = (fitz.Rect(98, 99, 142, 109), 2)                   # white patch over it
        background = (fitz.Rect(97, 98, 143, 110), 5)              # white box fill ...
        edges = [(P(97, 98), P(143, 98), 6), (P(97, 110), P(143, 110), 6)]  # ... edged after it
        self.assertEqual(figure_render.knockouts_of(group, [patch], [arrow]), [patch[0]])
        self.assertEqual(figure_render.knockouts_of(group, [background], [arrow] + edges), [])

    def test_drawn_ink_stays_inside_the_boxes_placement_checks(self) -> None:
        try:
            fonts = figure_render.Fonts()
        except BTPError:
            self.skipTest("no CJK font installed")
        document = fitz.open()
        page = document.new_page(width=300, height=200)
        page.insert_font(fontname="zh", fontfile=fonts.files["zh"])
        size, top, lines = 9.0, 50.0, ["测试字样", "管理会计", "精益思维", "价值流", "系统有效性"]
        boxes = figure_render.ink_boxes(lines, fitz.Rect(20, 0, 280, 200), top, size, fitz.TEXT_ALIGN_LEFT,
                                        fonts.objects["zh"])
        figure_render.set_lines(page, lines, boxes, "zh", size, (0, 0, 0), fonts)
        scale = 8
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
        rows = [y for y in range(pix.height) if min(pix.samples[y * pix.stride: y * pix.stride + pix.width]) < 128]
        # Five lines down, the last line's ink still ends inside its modelled box.
        self.assertGreaterEqual(rows[0] / scale, boxes[0].y0)
        self.assertLessEqual((rows[-1] + 1) / scale, boxes[-1].y1)

    def test_the_picture_stops_short_of_its_caption(self) -> None:
        crop = fitz.Rect(50, 100, 400, 300)
        above = figure_render.picture_area(crop, fitz.Rect(55, 104, 300, 113))
        self.assertEqual(round(above.y0), 114)
        below = figure_render.picture_area(crop, fitz.Rect(55, 290, 300, 299))
        self.assertEqual(round(below.y1), 289)
        outside = figure_render.picture_area(crop, fitz.Rect(55, 310, 300, 319))
        self.assertEqual(outside, crop)

    def test_side_by_side_labels_stay_in_their_own_column_and_underlines_go(self) -> None:
        try:
            figure_render.font_files()
        except BTPError:
            self.skipTest("no CJK font installed")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            _row_box_pdf(pdf)
            (exhibit,) = build_inventory(pdf)["exhibits"]
            self.assertEqual(exhibit["classification"], "diagram")
            names = {"Cost Throughput Time": "成本||吞吐时间", "$900,000 23 minutes": "$900,000||23 分钟",
                     "Quality Rates": "质量比率"}
            report = figure_render.render_diagram(pdf, exhibit, [names[l["text"]] for l in exhibit["labels"]],
                                                  Path(directory) / "out.png")
            self.assertTrue(report["passed"], report["problems"])
            rows = {row["target"]: row for row in report["rows"]}
            self.assertEqual(rows["成本吞吐时间"]["lines"], 2)
            self.assertEqual(report["patches"]["underlines_removed"], 1)


def _diagram_project(root: Path) -> tuple[dict, list[str], Path]:
    """A one-unit project whose target translates the diagram of ``_diagram_pdf``."""
    import json

    for folder in ("source", "analysis", "target/edited", "qa", "source-work/work-units", "state"):
        (root / folder).mkdir(parents=True)
    _diagram_pdf(root / "source" / "original.pdf")
    inventory = build_inventory(root / "source" / "original.pdf")
    (root / "analysis" / "exhibit-inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    digest = "a" * 64
    manifest = {"schema_version": 1, "source_fingerprint": digest, "resolved_config_hash": digest,
                "book_map_hash": digest, "style_guide_hash": digest, "units": [{
                    "unit_id": "unit-0001", "order": 0, "book_node_id": None, "segment_ids": ["seg-1"],
                    "source_file": "source-work/work-units/unit-0001.md", "source_hash": digest,
                    "draft_file": "target/draft/unit-0001.md", "edit_file": "target/edited/unit-0001.md",
                    "capsule_file": "context/capsules/unit-0001.json",
                    "observation_file": "state/observations/unit-0001.json"}]}
    (root / "state" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    from book_translation_pro.state import initialize_state
    state = initialize_state(root, "demo", digest, digest, manifest, ["exhibits"], {"applicable": True})
    assert [r["status"] for r in state["human_reviews"]] == ["pending"]  # a PDF book has exhibits to review
    (root / "source-work" / "work-units" / "unit-0001.md").write_text(LABELS_SOURCE, encoding="utf-8")
    config = {"source": {"path": "source/original.pdf", "format": "pdf"}, "passes": {"edit": True},
              "execution": {"checkpoints": ["exhibits"]}}
    names = {"Customer Needs": "客户需求", "Strategy Changes": "战略变化", "Feedback": "反馈"}
    labels = [names[label["text"]] for label in inventory["exhibits"][0]["labels"]]
    unit = root / "target" / "edited" / "unit-0001.md"
    unit.write_text(_labels_target(*labels), encoding="utf-8")
    return config, labels, unit


class ExhibitReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            figure_render.font_files()
        except BTPError:
            self.skipTest("no CJK font installed")
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config, self.labels, self.unit = _diagram_project(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _template(self) -> dict:
        import json

        from book_translation_pro.exhibit_review import TEMPLATE, build_exhibit_review
        summary = build_exhibit_review(self.root, self.config)
        self.assertEqual(summary["exhibits"], 1)
        return json.loads((self.root / TEMPLATE).read_text(encoding="utf-8"))

    def _record(self, review: dict) -> dict:
        import json

        from book_translation_pro.exhibit_review import record_exhibit_review
        path = self.root / "review-filled.json"
        path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        return record_exhibit_review(self.root, self.config, path)

    def test_every_exhibit_is_judged_and_the_record_is_bound_to_what_is_there(self) -> None:
        from book_translation_pro.exhibit_review import OVERVIEW, review_evidence
        from book_translation_pro.figure_render import render_project_figures
        from book_translation_pro.qa import _exhibit_review_checks

        render_project_figures(self.root, self.config)
        review = self._template()
        (item,) = review["items"]
        self.assertEqual((item["id"], item["treatment"], item["auto_problems"]), ("1.1", "diagram", []))
        self.assertTrue((self.root / "qa" / "exhibit-review" / item["image"]).is_file())
        self.assertTrue((self.root / OVERVIEW).is_file())
        with self.assertRaisesRegex(BTPError, "pending"):
            self._record(review)                                   # nothing judged yet
        item["verdict"] = "ok"
        with self.assertRaisesRegex(BTPError, "reviewer"):
            self._record(review)
        review["reviewer"] = "model"
        self.assertEqual(self._record(review)["flagged"], [])
        self.assertTrue(review_evidence(self.root, self.config)["exhibit_review"])
        checks: list[dict] = []
        _exhibit_review_checks(self.root, self.config, checks)
        self.assertEqual([c["status"] for c in checks], ["pass"])

        # A changed label makes the recorded review stale: it cannot be recorded or approved.
        self.unit.write_text(_labels_target(*self.labels[:-1], "回馈"), encoding="utf-8")
        render_project_figures(self.root, self.config)
        with self.assertRaisesRegex(BTPError, "does not match"):
            self._record(review)
        with self.assertRaisesRegex(BTPError, "stale"):
            review_evidence(self.root, self.config)
        checks = []
        _exhibit_review_checks(self.root, self.config, checks)
        self.assertEqual([(c["id"], c["status"]) for c in checks], [("exhibit.review_record", "fail")])

    def test_an_exhibit_with_an_automatic_problem_must_be_flagged_with_a_note(self) -> None:
        from book_translation_pro.qa import _exhibit_review_checks

        # Not rendered: the review says so, and only a flagged verdict with a note is accepted.
        review = self._template()
        (item,) = review["items"]
        self.assertTrue(item["auto_problems"])
        review["reviewer"] = "model"
        item["verdict"] = "ok"
        with self.assertRaisesRegex(BTPError, "must be flagged"):
            self._record(review)
        item["verdict"] = "flagged"
        with self.assertRaisesRegex(BTPError, "need a note"):
            self._record(review)
        item["notes"] = "not rendered yet"
        self.assertEqual(self._record(review)["flagged"], ["1.1"])
        checks: list[dict] = []
        _exhibit_review_checks(self.root, self.config, checks)
        self.assertIn(("exhibit.review_flagged", "warn"), [(c["id"], c["status"]) for c in checks])

    def test_only_a_pdf_book_waits_for_an_exhibit_review(self) -> None:
        from book_translation_pro.state import exhibits_review_default
        self.assertEqual(exhibits_review_default(True)[0], "pending")
        self.assertEqual(exhibits_review_default(False)[0], "not_required")

    def test_prepare_adopts_a_review_policy_change_for_a_manifest_without_an_extraction_hash(self) -> None:
        from book_translation_pro.artifacts import hash_data
        from book_translation_pro.config import extraction_config_hash
        from book_translation_pro.project import _prepared_extraction_hash

        previous = {"source": {"path": "a.pdf"}, "chunking": {}, "visual": {}, "execution": {"checkpoints": []}}
        manifest = {"resolved_config_hash": hash_data(previous)}
        self.assertEqual(_prepared_extraction_hash(manifest, previous), extraction_config_hash(previous))
        # A previous configuration that is not the one the manifest was prepared with proves nothing.
        other = dict(previous, chunking={"target_characters": 4000})
        self.assertIsNone(_prepared_extraction_hash(manifest, other))
        self.assertIsNone(_prepared_extraction_hash(manifest, None))

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

    def test_an_extracted_image_beside_its_caption_becomes_the_label_block(self) -> None:
        # A newly prepared book: extraction cropped the figure and dropped its labels.
        image = '<img src="assets\\original/images/000000.png" class="calibre2" />'
        units = _units("\n\n".join([PROSE_BEFORE, image, "**EXHIBIT 1.1** **Planning Loop**", PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units)
        self.assertEqual((decision["status"], decision["extraction_image"]), ("replace", image))
        text = rewrite_units(units, [decision])[0]
        self.assertNotIn("000000.png", text)
        self.assertIn(PROSE_BEFORE + "\n\n![](assets/original/images/exhibit-1-1.png)\n\n"
                      '<!-- btp:figure-labels exhibit="1.1" -->\n', text)
        self.assertIn("<!-- /btp:figure-labels -->\n\n**EXHIBIT 1.1** **Planning Loop**\n\n" + PROSE_AFTER, text)

    def test_headings_and_a_table_made_of_whole_labels_are_fragments(self) -> None:
        # Large labels came out as headings, side-by-side ones as a converter table;
        # one heading even became a unit of its own.
        units = _units("\n\n".join([PROSE_BEFORE, "**Customer Needs**", "|  |  |\n|---|---|\n|  | Feedback |"]) + "\n",
                       "### Strategy Changes\n", "**EXHIBIT 1.1** **Planning Loop**\n\n" + PROSE_AFTER + "\n")
        (decision,) = self._plan(units)
        self.assertEqual((decision["status"], decision["coverage"]), ("replace", 1.0))
        rewritten = rewrite_units(units, [decision])
        self.assertIn(PROSE_BEFORE, rewritten[0])
        self.assertNotIn("Feedback |", rewritten[0])
        self.assertEqual(rewritten[1].strip(), "")
        # A heading that is not a label is the book's own: the walk stops there.
        units = _units("\n\n".join([PROSE_BEFORE, "**Customer Needs** **Feedback**"]) + "\n",
                       "### Strategy Review\n", "**EXHIBIT 1.1** **Planning Loop**\n")
        (decision,) = self._plan(units)
        self.assertEqual(decision["status"], "unmatched")

    def test_the_caption_second_line_stays_with_the_caption(self) -> None:
        # "Changes" could pass for a label, so before this it was taken as a fragment
        # and the extracted image beside the caption was never used.
        exhibit = dict(DIAGRAM, caption="EXHIBIT 1.1 Planning Loop and", caption_tail="Changes")
        image = '<img src="assets\\original/images/000000.png" class="calibre2" />'
        units = _units("\n\n".join([PROSE_BEFORE, image, "**EXHIBIT 1.1 Planning Loop and**", "**Changes**",
                                    PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units, exhibit)
        self.assertEqual(decision["status"], "replace")
        text = rewrite_units(units, [decision])[0]
        self.assertIn("**EXHIBIT 1.1 Planning Loop and**\n\n**Changes**\n\n" + PROSE_AFTER, text)
        self.assertNotIn("000000.png", text)

    def test_a_word_cut_by_a_font_switch_is_matched_by_its_letters(self) -> None:
        cut = "Custo **mer** Needs **Strategy** Changes **Feedback**"
        units = _units("\n\n".join([PROSE_BEFORE, cut, "**EXHIBIT 1.1** **Planning Loop**"]) + "\n")
        (decision,) = self._plan(units)
        self.assertEqual((decision["status"], decision["coverage"]), ("replace", 1.0))
        # The same pieces with no font switch read as words the exhibit does not have.
        units = _units("\n\n".join([PROSE_BEFORE, "Custo mer Needs Strategy Changes Feedback",
                                    "**EXHIBIT 1.1** **Planning Loop**"]) + "\n")
        (decision,) = self._plan(units)
        self.assertEqual(decision["status"], "unmatched")

    def test_running_text_glued_to_a_caption_goes_back_to_its_paragraph(self) -> None:
        from book_translation_pro.retrofit import rejoin_caption_text
        head = "A figure set inside a paragraph splits the sentence it interrupts, and the rest of the sen-"
        lines = [[head, "", "![](assets/original/images/exhibit-1-1.png)"],
                 ["**EXHIBIT 1.1** **Planning Loop** tence comes after the caption.", "", PROSE_AFTER]]
        changed, log = rejoin_caption_text(lines, [DIAGRAM])
        self.assertEqual(changed, {0, 1})
        self.assertEqual(lines[0][0], head[:-1] + "tence comes after the caption.")
        self.assertEqual(lines[1][0], "**EXHIBIT 1.1** **Planning Loop**")
        self.assertTrue(log[0]["rejoined"])
        # A paragraph that ends its sentence is complete: the text stays where it is.
        lines = [[PROSE_BEFORE], ["**EXHIBIT 1.1** **Planning Loop** comes after the caption."]]
        changed, log = rejoin_caption_text(lines, [DIAGRAM])
        self.assertEqual((changed, log[0]["rejoined"]), (set(), False))
        self.assertEqual(lines[1][0], "**EXHIBIT 1.1** **Planning Loop** comes after the caption.")
        # Text starting a new sentence after the caption is not a continuation.
        lines = [[head], ["**EXHIBIT 1.1** **Planning Loop** The next paragraph starts here."]]
        self.assertEqual(rejoin_caption_text(lines, [DIAGRAM]), (set(), []))
        # The rest of the paragraph after the caption (and its label block) comes back too,
        # after what was on the caption's line, as long as the sentence is still open.
        lines = [[head, "", "**EXHIBIT 1.1** **Planning Loop** tence goes on past the",
                  "", "![](assets/original/images/exhibit-1-1.png)", "", '<!-- btp:figure-labels exhibit="1.1" -->',
                  "1. Customer Needs", "<!-- /btp:figure-labels -->", "", "caption and ends here.", "", PROSE_AFTER]]
        changed, _log = rejoin_caption_text(lines, [DIAGRAM])
        self.assertEqual(lines[0][0], head[:-1] + "tence goes on past the caption and ends here.")
        self.assertEqual(lines[0][2], "**EXHIBIT 1.1** **Planning Loop**")
        self.assertEqual(lines[0][10], "")
        self.assertEqual(lines[0][12], PROSE_AFTER)
        # A paragraph after the caption that starts a sentence stays.
        lines = [[head, "", "**EXHIBIT 1.1** **Planning Loop**", "", PROSE_AFTER]]
        self.assertEqual(rejoin_caption_text(lines, [DIAGRAM]), (set(), []))

    def test_a_caption_glued_to_the_end_of_a_paragraph_gets_its_own_line(self) -> None:
        from book_translation_pro.retrofit import split_glued_captions
        head = "Caspian is considering bringing these outside processing"
        units = _units(f"{head} **EXHIBIT 1.1** **Planning Loop**\n\nactivities in-house.\n")
        log = split_glued_captions(units, [DIAGRAM])
        self.assertEqual(units[0]["lines"][:3], [head, "", "**EXHIBIT 1.1** **Planning Loop**"])
        self.assertEqual(units[0]["text"], "\n".join(units[0]["lines"]))
        self.assertEqual([item["exhibit"] for item in log], ["1.1"])
        # Left alone: a bold cross-reference in prose case, bold that is not the caption,
        # a caption already on its own line, and a caption with text after it.
        kept = ["See the loop in **Exhibit 1.1 Planning Loop**",
                "The loop has a **Planning Loop** step",
                "**EXHIBIT 1.1** **Planning Loop**",
                "It runs **EXHIBIT 1.1** **Planning Loop** again"]
        units = _units("\n\n".join(kept) + "\n")
        self.assertEqual(split_glued_captions(units, [DIAGRAM]), [])
        self.assertEqual(units[0]["text"], "\n\n".join(kept) + "\n")

    def test_a_list_exhibit_whose_last_line_runs_into_the_paragraph(self) -> None:
        from collections import Counter

        from book_translation_pro.retrofit import WORD_RE, _last_words, _letters, rejoin_caption_text
        items = ["FR1—Produce the quantity every interval.", "FR2—Provide a safe working environment."]
        exhibit = {"key": "11.10", "label": "EXHIBIT", "part": 1, "parts": 1, "page": 3, "classification": "text_list",
                   "orientation": "below_caption", "caption": "EXHIBIT 11.10 The Requirements", "reason": "list",
                   "table": {"mode": "list", "header": None, "meta": [], "rows": [[item] for item in items],
                             "numeric_columns": [], "continues": False}}
        head = "The people who operate their piece of the system in"
        units = _units("\n\n".join([head, "**EXHIBIT 11.10 The Requirements**", items[0],
                                    f"{items[1]} the plants. These customers work there."]) + "\n")
        (decision,) = self._plan(units, exhibit)
        self.assertEqual(decision["status"], "replace")
        self.assertEqual(decision["remove"][-1]["keeps"], ["the plants. These customers work there."])
        lines = [rewrite_units(units, [decision])[0].split("\n")]
        self.assertIn("- FR2—Provide a safe working environment.", lines[0])
        rejoin_caption_text(lines, [exhibit])
        self.assertEqual(lines[0][0], head + " the plants. These customers work there.")
        # The last words are taken only when they use up the exhibit exactly and the
        # rest goes on in lower case.
        pool, words = _letters(items[1]), Counter(w.casefold() for w in WORD_RE.findall(items[1]))
        line = f"{items[1]} the plants."
        self.assertEqual(_last_words(line, pool, words), (items[1], "the plants."))
        self.assertIsNone(_last_words(f"{items[1]} The plants.", pool, words))
        self.assertIsNone(_last_words(f"FR2—Provide a safe the plants.", pool, words))
        self.assertIsNone(_last_words(f"{items[1]} *the* plants.", pool, words))

    def test_a_table_pictured_page_by_page_replaces_every_page_picture(self) -> None:
        exhibit = {"key": "10.4", "label": "EXHIBIT", "part": 1, "parts": 1, "page": 3, "classification": "table_grid",
                   "orientation": "below_caption", "caption": "EXHIBIT 10.4 Integrating Risks", "reason": "rows",
                   "continued": [{"page": 4, "region": [0, 0, 1, 1]}],
                   "table": {"mode": "grid", "header": ["", "Risks"], "meta": [], "rows": [["1", "Lost orders"], ["2", "Late orders"]],
                             "numeric_columns": [0], "continues": False}}
        pages = ['<img src="p287.png" />', '<img src="p288.png" />']
        units = _units("\n\n".join(["**EXHIBIT 10.4 Integrating Risks**", *pages, PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units, exhibit)
        self.assertEqual(decision["status"], "replace")
        self.assertEqual([item["text"] for item in decision["remove"]], pages)
        # A picture set right above another caption is that exhibit's, not a further page.
        units = _units("\n\n".join(["**EXHIBIT 10.4 Integrating Risks**", *pages, "**EXHIBIT 10.5 Next**"]) + "\n")
        (decision,) = self._plan(units, exhibit)
        self.assertEqual([item["text"] for item in decision["remove"]], pages[:1])

    def test_a_stray_picture_of_a_replaced_exhibit_is_removed(self) -> None:
        from book_translation_pro.retrofit import drop_duplicate_pictures
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            document = fitz.open()
            page = document.new_page()
            for n in range(6):
                page.draw_rect(fitz.Rect(80 + 40 * n, 120, 110 + 40 * n, 200 + 20 * n), color=(0, 0, 0), fill=(0.2 * (n % 3), 0.5, 0.8))
            page.insert_text((90, 330), "Process Flow", fontsize=12)
            crop = fitz.Rect(70, 110, 340, 340)
            images = project / "assets" / "original" / "images"
            images.mkdir(parents=True)
            page.get_pixmap(dpi=150, clip=crop).save(images / "002940.png")       # the exhibit again
            page.get_pixmap(dpi=150, clip=fitz.Rect(70, 110, 200, 340)).save(images / "000026.png")  # part of it only
            page.get_pixmap(dpi=150, clip=fitz.Rect(70, 250, 340, 480)).save(images / "003651.png")  # same shape, other content
            page.get_pixmap(dpi=150, clip=crop).save(images / "exhibit-10-3.png")  # the exhibit's own crop
            document.save(project / "book.pdf")
            document.close()
            inventory = {"exhibits": [{"key": "10.3", "part": 1, "page": 1, "crop": list(crop)}]}
            decisions = [{"key": "10.3", "part": 1, "page": 1, "status": "replace"}]
            lines = [["![](assets/original/images/exhibit-10-3.png)", "", '<img src="assets\\original/images/002940.png" />',
                      "", "![](assets/original/images/000026.png)", "", "See ![](assets/original/images/002940.png) here",
                      "", "![](assets/original/images/003651.png)"]]
            removed = drop_duplicate_pictures(project, project / "book.pdf", lines, inventory, decisions)
            self.assertEqual([(item["picture"], item["exhibit"]) for item in removed],
                             [("assets/original/images/002940.png", "10.3")])
            self.assertEqual(lines[0][2], "")
            self.assertEqual([lines[0][0], lines[0][4], lines[0][6], lines[0][8]],
                             ["![](assets/original/images/exhibit-10-3.png)", "![](assets/original/images/000026.png)",
                              "See ![](assets/original/images/002940.png) here", "![](assets/original/images/003651.png)"])
            # An exhibit left untouched (a picture kept as printed) is never matched.
            lines = [['<img src="assets\\original/images/002940.png" />']]
            self.assertEqual(drop_duplicate_pictures(project, project / "book.pdf", lines, inventory,
                                                     [dict(decisions[0], status="untouched")]), [])

    def test_each_page_of_a_diagram_has_its_own_crop(self) -> None:
        from book_translation_pro.retrofit import crop_asset_name
        self.assertEqual(crop_asset_name({"key": "11.6", "part": 1}), "exhibit-11-6.png")
        self.assertEqual(crop_asset_name({"key": "11.6", "part": 2}), "exhibit-11-6-p2.png")

    def test_the_extracted_image_is_replaced_where_extraction_put_it(self) -> None:
        caption = "**EXHIBIT 1.1** **Planning Loop**"
        # Printed above its caption, extracted below it: the block takes the image's place.
        units = _units("\n\n".join([PROSE_BEFORE, caption, "![](assets/original/images/below.png)", PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units)
        self.assertEqual(decision["status"], "replace")
        text = rewrite_units(units, [decision])[0]
        self.assertIn(caption + "\n\n![](assets/original/images/exhibit-1-1.png)", text)
        self.assertNotIn("below.png", text)
        # An image on each side: the printed side's is the exhibit, the other stays.
        units = _units("\n\n".join(["![](assets/original/images/above.png)", caption,
                                    "![](assets/original/images/below.png)"]) + "\n")
        (decision,) = self._plan(units)
        text = rewrite_units(units, [decision])[0]
        self.assertNotIn("above.png", text)
        self.assertIn(caption + "\n\n![](assets/original/images/below.png)", text)

    def test_an_extracted_table_image_becomes_the_rebuilt_table(self) -> None:
        table = dict(DIAGRAM, key="2.1", classification="table_grid", orientation="below_caption",
                     caption="EXHIBIT 2.1 Costs", labels=[],
                     table={"mode": "grid", "header": ["", "Amount"], "meta": [], "continues": False,
                            "rows": [["Material", "$111,431"], ["Total", "$111,431"]]})
        units = _units("\n\n".join([PROSE_BEFORE, '<img src="assets\\original/images/000027.png" />',
                                    "**EXHIBIT 2.1** **Costs**", PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units, table)
        self.assertEqual(decision["status"], "replace")
        text = rewrite_units(units, [decision])[0]
        self.assertNotIn("000027.png", text)
        self.assertIn(PROSE_BEFORE + '\n\n<!-- btp:exhibit-table exhibit="2.1" -->', text)
        self.assertIn("| Material | \\$111,431 |", text)
        self.assertIn("<!-- /btp:exhibit-table -->\n\n**EXHIBIT 2.1** **Costs**\n\n" + PROSE_AFTER, text)

    def test_an_image_is_taken_only_when_nothing_else_is_there(self) -> None:
        image = "![](assets/original/images/000000.png)"
        caption = "**EXHIBIT 1.1** **Planning Loop**"
        cases = {
            # Some label text survived: the extraction is not understood well enough.
            "labels left": [image, "**Customer**", caption],
            # Prose between the image and the caption: the image may be another figure's.
            "prose between": [image, PROSE_BEFORE, caption],
            "prose on both sides": [image, PROSE_BEFORE, caption, PROSE_AFTER, image],
        }
        for name, lines in cases.items():
            with self.subTest(name):
                units = _units("\n\n".join(lines) + "\n")
                (decision,) = self._plan(units)
                self.assertEqual(decision["status"], "unmatched")
                self.assertNotIn("extraction_image", decision)
                self.assertEqual(rewrite_units(units, [decision]), {})

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

    def test_a_table_whose_columns_ran_together_is_not_spliced_in(self) -> None:
        from book_translation_pro.exhibits import merged_cells
        rows = [["Sales", "$326,250", "$748,894 $453,215"], ["ROS", "31.8%", "47.1% 38.8%"]]
        self.assertEqual(merged_cells(rows), ["$748,894 $453,215", "47.1% 38.8%"])
        # A label with a number in it, or a cell set on two lines, is not a merged cell.
        self.assertEqual(merged_cells([["Phase 2 results", "$900,000"], ["2 hours<br>2 days", "4"]]), [])
        table = {
            "key": "2.1", "label": "EXHIBIT", "part": 1, "parts": 1, "page": 3, "classification": "table_grid",
            "orientation": "below_caption", "caption": "EXHIBIT 2.1 Costs", "reason": "rebuilt",
            "table": {"mode": "grid", "header": ["", "Amount"], "meta": [], "continues": False,
                      "rows": [["Material costs", "$111,431 $5"], ["TOTAL COSTS", "$111,436"]], "numeric_columns": [1]},
            "checks": {"characters_missing": "", "characters_extra": "", "numbers": 3, "totals": [],
                       "merged_cells": ["$111,431 $5"]},
        }
        units = _units("\n\n".join(["**EXHIBIT 2.1** **Costs**",
                                    "Amount Material costs \\$111,431 \\$5 TOTAL COSTS \\$111,436", PROSE_AFTER]) + "\n")
        (decision,) = self._plan(units, table)
        self.assertEqual(decision["status"], "needs_hand_rebuild")
        self.assertIn("$111,431 $5", decision["reason"])

    def test_untreated_exhibits_are_left_alone(self) -> None:
        raster = dict(DIAGRAM, classification="raster", reason="embedded picture")
        units = _units("**EXHIBIT 1.1** **Planning Loop**\n\n" + FRAGMENTS + "\n")
        (decision,) = self._plan(units, raster)
        self.assertEqual(decision["status"], "untouched")


class PrepareHookTests(unittest.TestCase):
    """prepare treats exhibits itself, before capsules, the Page Map and the manifest."""

    def _prepare(self, root: Path, make_pdf, mode: str = "study") -> Path:
        from book_translation_pro.project import initialize_project, prepare_project

        make_pdf(root / "book.pdf")
        project = Path(initialize_project(root / "book.pdf", root / "project", "zh-Hans", "en", mode, ["markdown"])["project"])
        with mock.patch("book_translation_pro.ingest._convert_with_tools", side_effect=BTPError("no calibre in unit tests")):
            self.result = prepare_project(project)
        return project

    def test_a_new_book_gets_label_blocks_and_crops_and_the_manifest_sees_them(self) -> None:
        import json

        from book_translation_pro.artifacts import sha256_file
        from book_translation_pro.retrofit import exhibit_blocks

        with tempfile.TemporaryDirectory() as directory:
            project = self._prepare(Path(directory), _diagram_pdf)
            self.assertEqual(self.result["exhibits"]["replace"], 1)
            unit = project / "source-work" / "work-units" / "unit-0001.md"
            text = unit.read_text(encoding="utf-8")
            (block,) = exhibit_blocks(text)
            self.assertEqual(sorted(block["labels"]), ["Customer Needs", "Feedback", "Strategy Changes"])
            self.assertIn("![](assets/original/images/exhibit-1-1.png)", text)
            self.assertTrue((project / "assets" / "original" / "images" / "exhibit-1-1.png").stat().st_size > 0)
            manifest = json.loads((project / "state" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["units"][0]["source_hash"], sha256_file(unit))
            log = json.loads((project / "analysis" / "exhibit-retrofit-log.json").read_text(encoding="utf-8"))
            self.assertEqual((log["applied_by"], log["decisions"][0]["status"]), ("prepare", "replace"))
            self.assertTrue((project / "analysis" / "exhibit-inventory.json").is_file())

    def test_a_new_book_gets_its_tables_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._prepare(Path(directory), lambda path: _table_pdf(path, "164,236"))
            text = (project / "source-work" / "work-units" / "unit-0001.md").read_text(encoding="utf-8")
            self.assertIn('<!-- btp:exhibit-table exhibit="2.1" -->', text)
            self.assertIn("| Material costs | 111,431 | 50.0% |", text)
            self.assertIn("| TOTAL COSTS | 164,236 | 100.0% |", text)

    def test_fast_mode_has_no_exhibit_checkpoint_and_leaves_exhibits_as_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._prepare(Path(directory), _diagram_pdf, mode="fast")
            self.assertNotIn("exhibits", self.result)
            self.assertFalse((project / "analysis" / "exhibit-inventory.json").exists())
            text = (project / "source-work" / "work-units" / "unit-0001.md").read_text(encoding="utf-8")
            self.assertNotIn("btp:figure-labels", text)


def _text(page: fitz.Page, x: float, y: float, text: str, font: str = "helv", size: float = 8, **kw) -> None:
    page.insert_text((x, y), text, fontname=font, fontsize=size, **kw)


def _rows(page: fitz.Page, top: float, rows: list[tuple[str, ...]], xs: tuple[float, ...], pitch: float = 11) -> None:
    for n, row in enumerate(rows):
        for x, cell in zip(xs, row):
            if cell:
                _text(page, x, top + pitch * n, cell)


class TableRebuildTests(unittest.TestCase):
    """Tables as books set them: stacked under one caption, run on to the next page,
    with footnotes, group labels up the side, and labels wrapped onto the value line."""

    def _inventory(self, build) -> list[dict]:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "book.pdf"
            document = fitz.open()
            build(document)
            document.save(pdf)
            document.close()
            inventory = build_inventory(pdf)
            validate_schema(inventory, "exhibit-inventory.schema.json")
            return inventory["exhibits"]

    def test_number_rules(self) -> None:
        from book_translation_pro.exhibits import numeric, quantity
        self.assertTrue(numeric("(3,000)"))
        self.assertTrue(numeric("$(5,061.07)"))
        self.assertFalse(numeric("100%)"))          # the end of a heading "Total (Must equal 100%)"
        self.assertTrue(quantity("4–6 weeks"))
        self.assertTrue(quantity("$30 million"))
        self.assertTrue(quantity("2 hours–2 days"))
        self.assertFalse(quantity("Month 1"))        # a heading
        self.assertFalse(quantity("5-Feb"))          # a date heading

    def test_tables_set_one_under_another_are_each_rebuilt(self) -> None:
        def build(document: fitz.Document) -> None:
            page = document.new_page()
            _body(page, 60)
            _text(page, 72, 160, "EXHIBIT 7.12 Valuing Inventory", font="hebo", size=9)
            _rows(page, 176, [("", "Days", "Total Cost"), ("", "20", "$849,526")], (80, 170, 240))
            _rows(page, 210, [("", "Days", "Value"), ("Raw material", "10", "$424,763"),
                              ("Finished goods", "4", "$169,905"), ("TOTAL", "14", "$594,668")], (80, 170, 240))
            _body(page, 300)

        (exhibit,) = self._inventory(build)
        self.assertEqual(exhibit["classification"], "table_grid")
        table = exhibit["table"]
        self.assertEqual(table["meta"], [])            # the first table is not flattened into text
        self.assertEqual(table["rows"], [["20", "$849,526"]])
        (second,) = table["sections"]
        self.assertEqual(second["header"], ["", "Days", "Value"])
        self.assertEqual(second["rows"][-1], ["TOTAL", "14", "$594,668"])
        self.assertEqual({t["column"]: t["verdict"] for t in exhibit["checks"]["totals"]}, {"Days": "exact", "Value": "exact"})
        self.assertEqual(exhibit["checks"]["characters_missing"], "")

    def _running_on(self, second_page) -> list[dict]:
        def build(document: fitz.Document) -> None:
            page = document.new_page()
            _body(page, 60, lines=20)
            # The table reaches the foot of the page (below three quarters of its height).
            _text(page, 72, 470, "EXHIBIT 9.5 Forecast", font="hebo", size=9)
            _rows(page, 486, [("", "Month 1", "Month 2")] + [(f"Product {c}", str(10 * n), str(20 * n))
                                                              for n, c in enumerate("ABCDEFGHIJKLMNOPQRS", 1)],
                  (80, 220, 300))
            second_page(document.new_page())

        return self._inventory(build)

    def test_a_table_runs_on_to_the_next_page(self) -> None:
        def second(page: fitz.Page) -> None:
            _rows(page, 100, [("Variable Margin", "5", "6"), ("Operating Expenses", "100", "100"),
                              ("Profit", "-95", "-94")], (80, 220, 300))
            _body(page, 160)

        (exhibit,) = self._running_on(second)
        self.assertEqual(exhibit["classification"], "table_grid")
        self.assertEqual([c["page"] for c in exhibit["continued"]], [2])
        (more,) = exhibit["table"]["sections"]
        self.assertEqual(more["rows"][0], ["Variable Margin", "5", "6"])
        self.assertEqual(more["rows"][-1], ["Profit", "-95", "-94"])
        self.assertEqual(exhibit["checks"]["characters_missing"], "")

    def test_body_text_on_the_next_page_is_not_a_continuation(self) -> None:
        (exhibit,) = self._running_on(lambda page: _body(page, 100))
        self.assertEqual(exhibit["classification"], "table_grid")
        self.assertNotIn("continued", exhibit)
        self.assertNotIn("sections", exhibit["table"])

    def test_a_continuation_that_cannot_be_rebuilt_whole_leaves_the_exhibit_uncertain(self) -> None:
        def second(page: fitz.Page) -> None:
            # Rows whose label and value touch: no column can be told apart.
            for n, label in enumerate(("Variable Margin", "Operating Expenses", "Profit")):
                y = 100 + 11 * n
                _text(page, 80, y, label)
                _text(page, 80 + fitz.get_text_length(label, "helv", 8) + 0.5, y, str(5 + n), font="hebo")
            _body(page, 160)

        (exhibit,) = self._running_on(second)
        self.assertEqual(exhibit["classification"], "uncertain")
        self.assertIn("runs on to page 2", exhibit["reason"])
        self.assertNotIn("table", exhibit)

    def test_quantities_footnote_and_a_sentence_above_the_table(self) -> None:
        def build(document: fitz.Document) -> None:
            page = document.new_page()
            _body(page, 60)
            _text(page, 72, 160, "EXHIBIT 3.1 Before and After", font="hebo", size=9)
            _text(page, 80, 174, "For each measure please compare the figures before lean with the figures after lean in the")
            _text(page, 80, 184, "plants.")
            _rows(page, 198, [("", "Before", "After"), ("Throughput time", "4-6 weeks", "2 hours-2 days"),
                              ("Gross profit", "38%", "51%"), ("Working capital*", "21.8%", "6.7%")], (80, 230, 300))
            _text(page, 80, 250, "* W/C = A/R + Inv - Trade Payables")
            _body(page, 290)

        (exhibit,) = self._inventory(build)
        table = exhibit["table"]
        self.assertEqual(table["meta"], [["For each measure please compare the figures before lean with the figures "
                                          "after lean in the"], ["plants."]])
        self.assertEqual(table["header"], ["", "Before", "After"])
        self.assertEqual(table["rows"][0], ["Throughput time", "4-6 weeks", "2 hours-2 days"])
        self.assertEqual(table["notes"], ["* W/C = A/R + Inv - Trade Payables"])
        self.assertEqual(exhibit["checks"]["characters_missing"], "")

    def test_group_labels_up_the_side_name_their_block_of_rows(self) -> None:
        def build(document: fitz.Document) -> None:
            page = document.new_page()
            _body(page, 60)
            _text(page, 72, 160, "EXHIBIT 7.5 Box Score", font="hebo", size=9)
            _rows(page, 176, [("", "Week 1", "Week 2"), ("Units per person", "31.77", "30.46"),
                              ("On-time shipment", "96.2%", "98.2%")], (100, 230, 300))
            _rows(page, 216, [("Revenue", "$366,487", "$321,499"), ("Inventory", "$310,622", "$295,712")],
                  (100, 230, 300))
            _text(page, 92, 208, "OPS", rotate=90)       # beside the first block, reading up
            _text(page, 92, 230, "MONEY", rotate=90)     # beside the second block
            _text(page, 82, 230, "57", rotate=90)        # a page number up the edge: not a label
            _body(page, 300)

        (exhibit,) = self._inventory(build)
        rows = exhibit["table"]["rows"]
        self.assertEqual([row[0] for row in rows], ["OPS", "", "MONEY", ""])
        self.assertEqual(rows[2][1:], ["Revenue", "$366,487", "$321,499"])
        self.assertNotIn("57", [cell for row in rows for cell in row])

    def test_a_label_wrapped_onto_its_value_line_and_a_totals_word_left_of_its_column(self) -> None:
        def build(document: fitz.Document) -> None:
            page = document.new_page()
            _body(page, 60)
            _text(page, 72, 160, "EXHIBIT 6.7 Process Costs", font="hebo", size=9)
            _rows(page, 176, [("Code", "Description", "Cost"), ("1", "Plan the work", "$100"),
                              ("2", "Manage legal, military, and academic", ""),
                              ("", "records and relationships", "$200")], (80, 120, 320))
            _text(page, 100, 220, "TOTALS")
            _text(page, 320, 220, "$300")
            _body(page, 300)

        (exhibit,) = self._inventory(build)
        table = exhibit["table"]
        self.assertEqual(table["header"], ["Code", "Description", "Cost"])
        self.assertEqual(table["rows"], [["1", "Plan the work", "$100"],
                                         ["2", "Manage legal, military, and academic records and relationships", "$200"],
                                         ["", "TOTALS", "$300"]])
        self.assertEqual([t["verdict"] for t in exhibit["checks"]["totals"]], ["exact"])

    def test_numbers_printed_as_one_span_and_labels_across_columns_keep_the_columns_apart(self) -> None:
        def build(document: fitz.Document) -> None:
            page = document.new_page()
            _body(page, 60)
            _text(page, 72, 150, "EXHIBIT 7.4 Plant-wide Income", font="hebo", size=9)
            _text(page, 160, 166, "VALUE STREAMS")  # a heading over several columns
            _rows(page, 176, [("", "Motors", "Design", "Support", "TOTAL"), ("Sales", "$326", "$87", "$12", "$425"),
                              ("Costs", "$111", "$203", "$37", "$351")], (72, 150, 210, 270, 350))
            # Two amounts a column apart, printed as one span.
            _text(page, 72, 209, "Profit")
            _text(page, 150, 209, "$215")
            _text(page, 210, 209, "($116)" + " " * 18 + "($25)")
            _text(page, 350, 209, "$74")
            # A total's label set across the two value-stream columns, beside its value.
            _text(page, 225, 220, "Opening inventory")
            _text(page, 350, 220, "$925")
            _body(page, 300)

        (exhibit,) = self._inventory(build)
        self.assertEqual(exhibit["classification"], "table_grid")
        self.assertEqual(exhibit["checks"]["merged_cells"], [])
        self.assertEqual(exhibit["table"]["header"], ["", "Motors", "Design", "Support", "TOTAL"])
        rows = exhibit["table"]["rows"]
        self.assertEqual(rows[2], ["Profit", "$215", "($116)", "($25)", "$74"])
        self.assertEqual(rows[3][-1], "$925")
        self.assertIn("Opening inventory", rows[3])

    def _unicode_font(self) -> str:
        """A font with "•" and "—" (the base-14 fonts print them as "·")."""
        try:
            return figure_render.font_files()[0]
        except BTPError:
            self.skipTest("no CJK font installed")

    def test_a_list_of_marked_items_is_rebuilt_as_a_list(self) -> None:
        fontfile = self._unicode_font()

        def build(document: fitz.Document, stray: bool = False) -> None:
            page = document.new_page()
            page.insert_font(fontname="uni", fontfile=fontfile)
            _body(page, 60)
            _text(page, 72, 160, "EXHIBIT 11.10 The Six Requirements", font="hebo", size=9)
            _text(page, 72, 176, "FR1—Produce the quantity every interval.", font="uni")
            _text(page, 72, 187, "FR2—Achieve FR1 in spite of internal and external", font="uni")
            _text(page, 90, 198, "variation.", font="uni")                   # wrapped under its text
            _text(page, 72, 209, "FR3—Provide a safe working environment.", font="uni")
            if stray:
                _text(page, 40, 220, "A line set left of the items", font="uni")
            _body(page, 300)

        (exhibit,) = self._inventory(build)
        self.assertEqual(exhibit["classification"], "text_list")
        self.assertEqual([row[0] for row in exhibit["table"]["rows"]],
                         ["FR1—Produce the quantity every interval.",
                          "FR2—Achieve FR1 in spite of internal and external variation.",
                          "FR3—Provide a safe working environment."])
        # A line that is neither an item nor wrapped under one: no list.
        (exhibit,) = self._inventory(lambda document: build(document, stray=True) or None)
        self.assertNotEqual(exhibit["classification"], "text_list")

    def test_bulleted_lists_side_by_side_under_plain_headings(self) -> None:
        fontfile = self._unicode_font()

        def build(document: fitz.Document, bullet: str = "•") -> None:
            page = document.new_page()
            page.insert_font(fontname="uni", fontfile=fontfile)
            _body(page, 60)
            _text(page, 72, 160, "EXHIBIT 8.3A Cultural Comparisons", font="hebo", size=9)
            _text(page, 90, 176, "Business as Usual")    # headings in the body face, centred over their lists
            _text(page, 290, 176, "Lean")
            for n, row in enumerate([("•", "Shareholder perspective", bullet, "Stakeholder perspective"),
                                     ("•", "Competing individuals:", bullet, "Cooperating teams:"),
                                     ("", "Market forces", "", "Shared goals")]):
                for x, cell in zip((72, 82, 260, 270), row):
                    if cell:
                        _text(page, x, 187 + 11 * n, cell, font="uni")
            _body(page, 300)

        (exhibit,) = self._inventory(build)
        self.assertEqual(exhibit["classification"], "table_lists")
        self.assertEqual(exhibit["table"]["header"], ["Business as Usual", "Lean"])
        self.assertEqual(exhibit["table"]["items"],
                         [["• Shareholder perspective", "• Competing individuals: Market forces"],
                          ["• Stakeholder perspective", "• Cooperating teams: Shared goals"]])
        # A column with no bullets has no items to hang its lines on: no list table.
        (exhibit,) = self._inventory(lambda document: build(document, bullet="") or None)
        self.assertNotEqual(exhibit["classification"], "table_lists")

    def test_a_text_table_with_numbered_rows_runs_on_to_the_next_page(self) -> None:
        def build(document: fitz.Document, second: str = "2") -> None:
            page = document.new_page()               # 842 high: the table reaches its foot
            _body(page, 60)
            _text(page, 72, 600, "EXHIBIT 10.4 Integrating Risks", font="hebo", size=9)
            _rows(page, 616, [("", "Risks", "Ref", "Activities")], (60, 80, 230, 280))
            lines = [(630, "1", "Out-of-date price", "p. 80", "Monthly PO audit"),
                     (640, "", "  information", "", "  over $1,000"),
                     (650, "", "-pay the right price", "", "Spending controls"),
                     (665, second, "Purchase orders lost", "p. 82", "AP compares"),
                     (675, "", "-record the POs", "", "  against PO file")]
            for y, number, risk, ref, activity in lines:
                for x, cell in ((60, number), (80, risk), (230, ref), (280, activity)):
                    if cell.strip():
                        _text(page, x + (9 if cell.startswith("  ") else 0), y, cell.strip())
            page = document.new_page()
            for y, number, risk, ref, activity in [(100, "", "", "", "Std work review"),
                                                   (115, "3", "Inadequate policies", "p. 82", "Yearly review"),
                                                   (125, "", "  to prevent use", "", ""),
                                                   (140, "", "Other Risks Not Included", "", ""),
                                                   (155, "", "Poor communication", "p. 79", ""),
                                                   (165, "", "  of needs", "", "")]:
                for x, cell in ((60, number), (80, risk), (230, ref), (280, activity)):
                    if cell.strip():
                        _text(page, x + (9 if cell.startswith("  ") else 0), y, cell.strip())
            _body(page, 300)

        (exhibit,) = self._inventory(build)
        self.assertEqual(exhibit["classification"], "table_grid")
        self.assertEqual([c["page"] for c in exhibit["continued"]], [2])
        self.assertEqual(exhibit["table"]["header"], ["", "Risks", "Ref", "Activities"])
        self.assertEqual(exhibit["table"]["rows"], [
            ["1", "Out-of-date price information<br>-pay the right price", "p. 80", "Monthly PO audit over $1,000<br>Spending controls"],
            ["2", "Purchase orders lost<br>-record the POs", "p. 82", "AP compares against PO file<br>Std work review"],
            ["3", "Inadequate policies to prevent use", "p. 82", "Yearly review"],
            ["", "Other Risks Not Included", "", ""],
            ["", "Poor communication of needs", "p. 79", ""]])
        # Numbers out of sequence: not a numbered table.
        (exhibit,) = self._inventory(lambda document: build(document, second="5") or None)
        self.assertEqual(exhibit["classification"], "uncertain")

    def test_numbers_with_words_between_them_are_not_merged_columns(self) -> None:
        from book_translation_pro.exhibits import merged_cells
        self.assertEqual(merged_cells([["Network Phases 1 and 2", "Steps 9 and 10<br>$87,909"]]), [])
        self.assertEqual(merged_cells([["$87,909 $12,764", "x<br>(364) (57)"]]), ["$87,909 $12,764", "x<br>(364) (57)"])

    def test_only_numbers_a_column_gap_apart_split_a_span(self) -> None:
        from book_translation_pro.exhibits import _numbers_apart

        def chars(*words: tuple[str, float]) -> list[dict]:
            out = []
            for word, x in words:
                for n, c in enumerate(word):
                    out.append({"c": c, "bbox": (x + 4 * n, 0, x + 4 * n + 4, 8)})
                out.append({"c": " ", "bbox": (x + 4 * len(word), 0, x + 4 * len(word) + 2, 8)})
            return out[:-1]
        self.assertEqual(len(_numbers_apart(chars(("$87", 0), ("$12", 40)), 8)), 2)
        self.assertIsNone(_numbers_apart(chars(("$87", 0), ("$12", 14)), 8))       # a word space apart
        self.assertIsNone(_numbers_apart(chars(("Total", 0), ("$12", 60)), 8))     # a word is no number
        self.assertIsNone(_numbers_apart(chars(("$87", 0),), 8))

    def test_untitled_columns_fold_only_into_a_complementary_neighbour(self) -> None:
        from book_translation_pro.exhibits import _merge_complementary
        self.assertEqual(_merge_complementary(["", "Activity", "Cost"], [["N/A", "", "0%"], ["", "Register", "50%"]]),
                         (["Activity", "Cost"], [["N/A", "0%"], ["Register", "50%"]]))
        # A row-label column filled beside the titled one in the same rows stays apart.
        rows = [["Revenue", "x", "1"], ["Costs", "y", "2"]]
        self.assertEqual(_merge_complementary(["", "Note", "Cost"], rows), (["", "Note", "Cost"], rows))
        # So does an untitled column of numbers.
        rows = [["46.00%", "", "Hurdle"], ["", "a", "b"]]
        self.assertEqual(_merge_complementary(["", "Label", "Week"], rows), (["", "Label", "Week"], rows))

    def test_sections_and_footnotes_reach_the_source_unit(self) -> None:
        from book_translation_pro.retrofit import _block, _exhibit_texts
        exhibit = {"key": "7.12", "classification": "table_grid", "table": {
            "mode": "grid", "header": ["Days", "Cost"], "meta": [], "rows": [["20", "$849,526"]],
            "numeric_columns": [0, 1], "continues": False, "notes": ["* At month end"],
            "sections": [{"header": ["", "Value"], "meta": [], "rows": [["Raw material", "$424,763"]],
                          "numeric_columns": [1]}]}}
        text = "\n".join(_block(exhibit))
        self.assertEqual(text.count("|---|"), 2)
        self.assertIn("| Raw material | \\$424,763 |", text)
        self.assertIn("\n\\* At month end\n", text)
        self.assertIn("Raw material", _exhibit_texts(exhibit))
        self.assertIn("* At month end", _exhibit_texts(exhibit))


if __name__ == "__main__":
    unittest.main()


from book_translation_pro.qa import _corrected_back, _declared_corrections, _exhibit_unit_checks, _number_tokens

LABELS_SOURCE = (
    "![](assets/original/images/exhibit-1-1.png)\n\n"
    '<!-- btp:figure-labels exhibit="1.1" -->\n1. Customer Needs\n2. FR1\n3. Feedback\n<!-- /btp:figure-labels -->\n\n'
    "**EXHIBIT 1.1** **Planning Loop**\n"
)
TABLE_SOURCE = (
    "**EXHIBIT 7.3** **Income**\n\n"
    '<!-- btp:exhibit-table exhibit="7.3" -->\n\n'
    "|  |  |  |\n|---|---|---|\n| Material costs | \\$111,431 | 34.2% |\n| Employee costs | \\$49,515 | 3.4% |\n\n"
    "<!-- /btp:exhibit-table -->\n"
)


def _labels_target(*labels: str) -> str:
    body = "\n".join(f"{n}. {label}" for n, label in enumerate(labels, 1))
    return ("![](assets/original/images/exhibit-1-1.png)\n\n"
            f'<!-- btp:figure-labels exhibit="1.1" -->\n{body}\n<!-- /btp:figure-labels -->\n\n**图表 1.1** **规划循环**\n')


def _table_target(employee_share: str, note: str = "") -> str:
    return (
        "**图表 7.3** **收入**\n\n"
        '<!-- btp:exhibit-table exhibit="7.3" -->\n\n'
        f"|  |  |  |\n|---|---|---|\n| 材料成本 | \\$111,431 | 34.2% |\n| 员工成本 | \\$49,515 | {employee_share} |\n\n"
        "<!-- /btp:exhibit-table -->\n" + note
    )


class ExhibitQATests(unittest.TestCase):
    def _fails(self, source: str, target: str, issues: list[str] | None = None) -> list[str]:
        checks: list[dict] = []
        _exhibit_unit_checks(checks, "unit-0001", source, target, {"source_issues": issues or []})
        return [f"{c['id']}: {c['message']}" for c in checks if c["status"] == "fail"]

    def test_leaked_tool_text_is_caught_but_book_text_is_not(self) -> None:
        from book_translation_pro.qa import TOOL_TEXT_RE
        leaked = ["*** Add File: work/observations/unit-0201.json", "*** Begin Patch", "  *** Update File: a.md",
                  '{"schema_version":1,"new_entries":[],"conflicts":[]}']
        book = ["*** 重要提示 ***", "正文提到 *** Add File: 不在行首", '{"schema_version": 1}', "> 译者注：原书此处印作 3.4%"]
        self.assertEqual([bool(TOOL_TEXT_RE.match(line)) for line in leaked], [True] * len(leaked))
        self.assertEqual([bool(TOOL_TEXT_RE.match(line)) for line in book], [False] * len(book))

    def test_translated_labels_with_break_marks_pass(self) -> None:
        self.assertEqual(self._fails(LABELS_SOURCE, _labels_target("客户|需求", "FR1", "反馈")), [])

    def test_dropped_or_merged_label_fails(self) -> None:
        fails = self._fails(LABELS_SOURCE, _labels_target("客户需求与FR1", "反馈"))
        self.assertTrue(any(f.startswith("exhibit.labels") for f in fails), fails)

    def test_changed_code_and_untranslated_label_fail(self) -> None:
        fails = self._fails(LABELS_SOURCE, _labels_target("Customer Needs", "FR一", "反馈"))
        self.assertEqual(len([f for f in fails if f.startswith("exhibit.labels")]), 2, fails)

    def test_indexed_symbol_runs_are_codes_but_words_are_not(self) -> None:
        from book_translation_pro.exhibits import keeps_source_form
        self.assertTrue(all(map(keeps_source_form, ["R8...... Rn", "V1 … Vn", "$V1", "WIP", "FR2", "–"])))
        self.assertFalse(any(map(keeps_source_form, ["Rn Is Here", "Go To", "Customer Needs", "Value"])))

    def test_a_short_capitalised_label_translated_as_a_word_needs_a_declaration(self) -> None:
        source = LABELS_SOURCE.replace("3. Feedback", "3. MAN")
        target = _labels_target("客户|需求", "FR1", "人员")
        self.assertTrue(any("code 'MAN' must be copied unchanged" in f for f in self._fails(source, target)))
        declared = ["label_is_word: 1.1#3 (the cause category man, not an abbreviation)"]
        checks: list[dict] = []
        _exhibit_unit_checks(checks, "unit-0001", source, target, {"source_issues": declared})
        self.assertEqual([(c["status"], c["message"]) for c in checks if c["id"] == "exhibit.labels"],
                         [("warn", "Exhibit 1.1 label 3: 'MAN' declared a word, translated as '人员': "
                                   "the cause category man, not an abbreviation")])
        # A declaration for another label, or with no reason, excuses nothing.
        for issues in (["label_is_word: 1.1#2 (x)"], ["label_is_word: 1.1#3 ()"]):
            self.assertTrue(self._fails(source, target, issues), issues)

    def test_figure_audit_sees_the_image_across_a_long_label_block(self) -> None:
        from book_translation_pro.figures import _caption_images
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            image = project / "assets/original/images/exhibit-1-1.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"png")
            labels = "\n".join(f"{n}. 标签{n}" for n in range(1, 13))
            target = ("![](assets/original/images/exhibit-1-1.png)\n\n<!-- btp:figure-labels exhibit=\"1.1\" -->\n"
                      f"{labels}\n<!-- /btp:figure-labels -->\n\n**图表 1.1**\n")
            present, missing = _caption_images(project, target, "**图表 1.1**")
            self.assertEqual(list(present), ["assets/original/images/exhibit-1-1.png"])
            self.assertEqual(missing, [])

    def test_missing_block_markers_fail(self) -> None:
        target = "![](assets/original/images/exhibit-1-1.png)\n\n1. 客户需求\n2. FR1\n3. 反馈\n\n**图表 1.1**\n"
        fails = self._fails(LABELS_SOURCE, target)
        self.assertTrue(any(f.startswith("exhibit.blocks") for f in fails), fails)

    def test_changed_table_number_without_declaration_fails(self) -> None:
        fails = self._fails(TABLE_SOURCE, _table_target("15.2%"))
        self.assertTrue(any(f.startswith("exhibit.table") for f in fails), fails)

    def test_declared_correction_with_note_passes_and_numbers_read_as_printed(self) -> None:
        issue = "source_error_corrected: 3.4% -> 15.2% (exhibit 7.3): rows sum to 15.2%"
        target = _table_target("15.2%", "\n> 译者注：原书此处印作 3.4%，按各行合计应为 15.2%，已改正。\n")
        checks: list[dict] = []
        stripped = _exhibit_unit_checks(checks, "unit-0001", TABLE_SOURCE, target, {"source_issues": [issue]})
        self.assertEqual([c for c in checks if c["status"] == "fail"], [])
        self.assertNotIn("译者注", stripped)
        corrections = _declared_corrections({"source_issues": [issue]})
        self.assertEqual(_corrected_back(_number_tokens(stripped), corrections), _number_tokens(TABLE_SOURCE))

    def test_declared_correction_without_note_fails(self) -> None:
        issue = "source_error_corrected: 3.4% -> 15.2% (exhibit 7.3): rows sum to 15.2%"
        fails = self._fails(TABLE_SOURCE, _table_target("15.2%"), [issue])
        self.assertTrue(any(f.startswith("exhibit.correction_note") for f in fails), fails)

    def test_declaration_does_not_excuse_a_second_changed_number(self) -> None:
        issue = "source_error_corrected: 3.4% -> 15.2% (exhibit 7.3): rows sum to 15.2%"
        target = _table_target("15.2%", "\n> 译者注：原书此处印作 3.4%。\n").replace("111,431", "111,432")
        fails = self._fails(TABLE_SOURCE, target, [issue])
        self.assertTrue(any(f.startswith("exhibit.table") for f in fails), fails)

    def test_render_figures_renders_caches_and_qa_sees_stale_labels(self) -> None:
        import json

        from book_translation_pro.figure_render import ExhibitPublisher, render_project_figures
        from book_translation_pro.qa import _exhibit_book_checks

        try:
            figure_render.font_files()
        except BTPError:
            self.skipTest("no CJK font installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, labels, unit = _diagram_project(root)

            first = render_project_figures(root, config)
            self.assertEqual((first["rendered"], first["failed"]), (1, 0), first)
            self.assertTrue((root / "assets" / "translated" / "figures" / "exhibit-1-1.png").is_file())
            self.assertEqual(render_project_figures(root, config)["cached"], 1)
            checks: list[dict] = []
            _exhibit_book_checks(root, config, checks, set())
            self.assertEqual([c["status"] for c in checks if c["id"] == "exhibit.render"], ["pass"])

            # Publication: the reader sees the translated diagram and no label list;
            # the bilingual original side keeps the source image.
            published = ExhibitPublisher(root, config).target(unit.read_text(encoding="utf-8") + "\n" + TABLE_SOURCE)
            self.assertTrue(published.startswith("![](assets/translated/figures/exhibit-1-1.png)\n\n**图表 1.1**"))
            self.assertNotIn("btp:", published)
            self.assertNotIn("1. 客户需求", published)
            self.assertIn("| Material costs |", published)
            original = ExhibitPublisher(root, config).source(LABELS_SOURCE)
            self.assertTrue(original.startswith("![](assets/original/images/exhibit-1-1.png)\n\n**EXHIBIT 1.1**"))
            self.assertNotIn("Customer Needs", original)

            unit.write_text(_labels_target(*labels[:-1], "回馈"), encoding="utf-8")
            with self.assertRaises(BTPError):
                ExhibitPublisher(root, config).target(unit.read_text(encoding="utf-8"))
            checks = []
            _exhibit_book_checks(root, config, checks, set())
            stale = [c for c in checks if c["id"] == "exhibit.render" and c["status"] == "fail"]
            self.assertEqual(len(stale), 1)
            self.assertIn("run render-figures", stale[0]["message"])

    def test_bare_correction_without_reason_is_not_a_declaration(self) -> None:
        self.assertEqual(_declared_corrections({"source_issues": ["source_error_corrected: 3.4% -> 15.2% (exhibit 7.3)"]}), [])

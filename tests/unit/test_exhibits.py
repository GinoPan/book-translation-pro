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

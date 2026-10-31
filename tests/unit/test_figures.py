from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from book_translation_pro.figures import reconstruct_figures, run_figure_audit


class FigureAuditTests(unittest.TestCase):
    def _project(self, root: Path, target: str) -> dict:
        (root / "source").mkdir(parents=True)
        (root / "target" / "edited").mkdir(parents=True)
        (root / "state" / "observations").mkdir(parents=True)
        (root / "qa").mkdir(parents=True)
        import fitz

        document = fitz.open()
        page = document.new_page()
        page.draw_rect(fitz.Rect(72, 120, 360, 300), color=(0, 0, 0))
        page.insert_text((72, 72), "EXHIBIT 1.1", fontsize=10)
        document.save(root / "source" / "original.pdf")
        document.close()
        (root / "target" / "edited" / "unit-0001.md").write_text(target, encoding="utf-8")
        (root / "state" / "observations" / "unit-0001.json").write_text(
            json.dumps({"source_issues": []}), encoding="utf-8"
        )
        return {
            "source": {"path": "source/original.pdf", "format": "pdf"},
            "target": {"language": "zh-Hans"},
            "passes": {"edit": True},
        }

    def test_vector_exhibit_without_target_asset_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(
                root,
                "正文提到图表 1.1。<span class=\"calibre26\">1</span>\n",
            )
            report = run_figure_audit(root, config)
            self.assertFalse(report["passed"])
            self.assertEqual(report["figures"][0]["disposition"], "missing_target_caption")

    def test_target_asset_satisfies_vector_exhibit_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "**图表 1.1**　示例\n")
            image_dir = root / "assets" / "original" / "images"
            image_dir.mkdir(parents=True)
            Image.new("RGB", (64, 64), "white").save(image_dir / "exhibit-1-1.png")
            (root / "target" / "edited" / "unit-0001.md").write_text(
                "![](assets/original/images/exhibit-1-1.png)\n\n**图表 1.1**　示例\n",
                encoding="utf-8",
            )
            report = run_figure_audit(root, config)
            self.assertTrue(report["passed"])
            self.assertEqual(report["figures"][0]["disposition"], "target_asset")

    def _pdf(self, root: Path, lines: list[str], drawings: bool = True) -> None:
        import fitz

        document = fitz.open()
        page = document.new_page()
        if drawings:
            for offset in range(3):
                page.draw_rect(fitz.Rect(72, 300 + offset * 40, 360, 330 + offset * 40), color=(0, 0, 0))
        for index, line in enumerate(lines):
            page.insert_text((72, 72 + index * 20), line, fontsize=10)
        document.save(root / "source" / "original.pdf")
        document.close()

    def test_captions_with_titles_are_recognized_but_prose_citations_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "")
            self._pdf(root, [
                "Figure 3.2 The machine age", "Fig. 4: Output by year", "Chart 5", "Figure 6-2 Belt drive",
                "Plate II", "Figure 7 shows the output of the mill.", "Table 8 lists every station.",
            ])
            report = run_figure_audit(root, config)
            self.assertEqual(
                sorted((item["kind"], item["key"]) for item in report["figures"]),
                [("chart", "5"), ("figure", "3.2"), ("figure", "4"), ("figure", "6-2"), ("plate", "II")],
            )

    def test_list_of_figures_entries_are_not_cropped(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "**图表 1.1**　示例\n")
            document = fitz.open()
            listing = document.new_page()
            listing.insert_text((72, 100), "Exhibit 1.1 Example ........ 3", fontsize=10)
            listing.insert_text((72, 120), "Exhibit 1.2 Second example 4", fontsize=10)
            document.new_page()
            page = document.new_page()
            for offset in range(3):
                page.draw_rect(fitz.Rect(72, 120 + offset * 40, 360, 150 + offset * 40), color=(0, 0, 0))
            page.insert_text((72, 300), "EXHIBIT 1.1", fontsize=10)
            page.insert_text((72, 330), "Exhibit 1.2 Second example", fontsize=10)
            document.save(root / "source" / "original.pdf")
            document.close()
            pages = {item["key"]: item["source_pages"] for item in run_figure_audit(root, config)["figures"]}
            # The dot-leader entry is dropped; a leaderless entry is listed
            # but the crop still comes from the page with the visual.
            self.assertEqual(pages, {"1.1": [3], "1.2": [1, 3]})
            generated = {item["key"]: item["source_page"] for item in reconstruct_figures(root, config)["generated"]}
            self.assertEqual(generated, {"1.1": 3, "1.2": 3})

    def test_split_label_span_is_recognized(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "")
            document = fitz.open(root / "source" / "original.pdf")
            page = document[0]
            page.insert_text((72, 400), "Figure", fontsize=10, fontname="hebo")
            page.insert_text((110, 400), "2.1 Gear train", fontsize=10)
            document.saveIncr()
            document.close()
            keys = {item["key"] for item in run_figure_audit(root, config)["figures"]}
            self.assertEqual(keys, {"1.1", "2.1"})

    def test_figure_caption_does_not_satisfy_table_of_the_same_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "")
            self._pdf(root, ["Figure 1", "Table 1"])
            image_dir = root / "assets" / "original" / "images"
            image_dir.mkdir(parents=True)
            Image.new("RGB", (64, 64), "white").save(image_dir / "figure-1.png")
            (root / "target" / "edited" / "unit-0001.md").write_text(
                "![](assets/original/images/figure-1.png)\n\n**图 1**　示例\n", encoding="utf-8"
            )
            report = run_figure_audit(root, config)
            dispositions = {(item["kind"], item["key"]): item["disposition"] for item in report["figures"]}
            self.assertEqual(dispositions, {("figure", "1"): "target_asset", ("table", "1"): "missing_target_caption"})
            self.assertFalse(report["passed"])

    def test_missing_image_file_does_not_count_as_a_target_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "![](assets/original/images/missing.png)\n\n**图表 1.1**　示例\n")
            item = run_figure_audit(root, config)["figures"][0]
            self.assertEqual((item["disposition"], item["status"]), ("missing_visual_asset", "fail"))
            self.assertIn("missing.png", item["message"])

    def test_another_exhibits_image_does_not_satisfy_this_caption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "")
            self._pdf(root, ["EXHIBIT 1.1", "EXHIBIT 1.2"])
            image_dir = root / "assets" / "original" / "images"
            image_dir.mkdir(parents=True)
            Image.new("RGB", (64, 64), "white").save(image_dir / "exhibit-1-1.png")
            (root / "target" / "edited" / "unit-0001.md").write_text(
                "![](assets/original/images/exhibit-1-1.png)\n\n**图表 1.1**　示例\n\n正文。\n\n**图表 1.2**　其二\n",
                encoding="utf-8",
            )
            dispositions = {item["key"]: item["disposition"] for item in run_figure_audit(root, config)["figures"]}
            self.assertEqual(dispositions, {"1.1": "target_asset", "1.2": "missing_visual_asset"})
            # A heading-style caption bounds the search just like a bold one.
            (root / "target" / "edited" / "unit-0001.md").write_text(
                "**图表 1.1**　示例\n\n## 图表 1.2　其二\n\n![](assets/original/images/exhibit-1-1.png)\n",
                encoding="utf-8",
            )
            dispositions = {item["key"]: item["disposition"] for item in run_figure_audit(root, config)["figures"]}
            self.assertEqual(dispositions, {"1.1": "missing_visual_asset", "1.2": "target_asset"})

    def test_uncaptioned_visuals_are_reported_not_passed_silently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "")
            self._pdf(root, ["Plain body text without any exhibit label."])
            report = run_figure_audit(root, config)
            self.assertEqual(report["figures"], [])
            self.assertEqual(len(report["warnings"]), 1)
            self.assertIn("1 source page(s)", report["warnings"][0])
            self._pdf(root, ["Plain body text without any exhibit label."], drawings=False)
            report = run_figure_audit(root, config)
            self.assertEqual((report["warnings"], report["passed"]), ([], True))

    def test_english_target_captions_are_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {**self._project(root, "**Exhibit 1.1** Example\n"), "target": {"language": "en"}}
            image_dir = root / "assets" / "original" / "images"
            image_dir.mkdir(parents=True)
            Image.new("RGB", (64, 64), "white").save(image_dir / "exhibit-1-1.png")
            (root / "target" / "edited" / "unit-0001.md").write_text(
                "![](assets/original/images/exhibit-1-1.png)\n\n**Exhibit 1.1** Example\n", encoding="utf-8"
            )
            self.assertEqual(run_figure_audit(root, config)["figures"][0]["disposition"], "target_asset")

    def test_unrecognized_target_language_only_warns_about_missing_captions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {**self._project(root, "**図 1.1** 例\n"), "target": {"language": "ja"}}
            item = run_figure_audit(root, config)["figures"][0]
            self.assertEqual((item["disposition"], item["status"]), ("missing_target_caption", "warn"))

    def test_reconstruct_figures_creates_audited_source_crop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "**图表 1.1**　示例\n")
            report = reconstruct_figures(root, config)
            asset = root / "assets" / "original" / "images" / "exhibit-1-1.png"
            self.assertEqual(report["generated_count"], 1)
            self.assertTrue(asset.is_file())
            self.assertEqual(report["generated"][0]["render_method"], "source_pdf_render_180dpi")
            target = (root / "target" / "edited" / "unit-0001.md").read_text(encoding="utf-8")
            self.assertIn("![](assets/original/images/exhibit-1-1.png)", target)

    def test_reconstruct_figures_removes_orphaned_ocr_label_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root, "**图表 1.1**　示例\n")
            (root / "target" / "edited" / "unit-0001.md").write_text(
                "正文保留。\n\n<span class=\"calibre3\">**流程材料**</span>\n"
                "\n<span class=\"calibre3\">**生产率**　**质量**</span>\n",
                encoding="utf-8",
            )
            (root / "target" / "edited" / "unit-0002.md").write_text(
                "**图表 1.1**　示例\n", encoding="utf-8"
            )
            report = reconstruct_figures(root, config)
            self.assertEqual(report["target_repairs"]["repaired"][0]["ocr_fragment_units"], ["unit-0001"])
            cleaned = (root / "target" / "edited" / "unit-0001.md").read_text(encoding="utf-8")
            self.assertIn("正文保留。", cleaned)
            self.assertNotIn("流程材料", cleaned)

    def test_reconstruct_figures_does_not_duplicate_native_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(
                root,
                "**图表 1.1**　示例\n\n| 左 | 右 |\n|---|---|\n| A | B |\n",
            )
            report = reconstruct_figures(root, config)
            target = (root / "target" / "edited" / "unit-0001.md").read_text(encoding="utf-8")
            self.assertNotIn("![](assets/original/images/exhibit-1-1.png)", target)
            self.assertEqual(report["target_repairs"]["repaired"][0]["skipped_reason"], "native_table_reconstruction")


if __name__ == "__main__":
    unittest.main()

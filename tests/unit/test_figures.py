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

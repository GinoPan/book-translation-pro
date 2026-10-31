from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from book_translation_pro.artifacts import atomic_write_json, hash_data, sha256_file
from book_translation_pro.visual import detect_layout, run_visual_qa, semantic_page, visual_dependency_hash


def _block(x0: float, y0: float, x1: float, y1: float, text: str) -> dict:
    return {
        "type": 0,
        "bbox": [x0, y0, x1, y1],
        "lines": [{"spans": [{"text": text, "size": 10}]}],
    }


class VisualTests(unittest.TestCase):
    def test_two_column_layout_detection(self) -> None:
        blocks = [
            _block(40, 100, 260, 300, "Left column contains enough source text for robust detection."),
            _block(340, 100, 560, 300, "Right column contains enough source text for robust detection."),
        ]
        self.assertEqual(detect_layout(blocks, 600), "multi_column")

    def _project(self, root: Path) -> tuple[dict, dict]:
        (root / "source").mkdir(parents=True)
        (root / "analysis").mkdir()
        (root / "qa" / "visual-reviews").mkdir(parents=True)
        (root / "assets" / "page-images").mkdir(parents=True)
        import fitz

        document = fitz.open()
        document.new_page()
        document.save(root / "source" / "original.pdf")
        document.close()
        image_path = root / "assets" / "page-images" / "page-0001.png"
        Image.new("RGB", (800, 1000), "white").save(image_path)
        page = {
            "page_index": 1,
            "image_ref": "assets/page-images/page-0001.png",
            "image_sha256": sha256_file(image_path),
            "pixel_width": 800,
            "pixel_height": 1000,
            "page_kind": "body",
            "layout": "multi_column",
            "reading_order_confidence": 0.55,
            "text_characters": 100,
            "text_sha256": hash_data("source"),
            "source_unit_ids": ["unit-0001"],
            "ocr_status": "not_needed",
            "ocr_issues": [],
            "regions": [],
            "risk": 2,
            "risk_reasons": ["multi_column_reading_order"],
            "review_status": "pending",
        }
        page_map = {
            "schema_version": 1,
            "source_fingerprint": sha256_file(root / "source" / "original.pdf"),
            "page_count": 1,
            "render": {"renderer": "pymupdf", "dpi": 180},
            "pages": [page],
        }
        atomic_write_json(root / "analysis" / "page-map.json", page_map)
        review = {
            "schema_version": 1,
            "page_index": 1,
            "page_hash": hash_data(semantic_page(page)),
            "status": "approved",
            "reviewer": "tester",
            "reviewed_at": "2026-09-05T00:00:00Z",
            "notes": "Column-major order confirmed.",
            "findings": [],
        }
        atomic_write_json(root / "qa" / "visual-reviews" / "page-0001.json", review)
        config = {
            "source": {"path": "source/original.pdf", "format": "pdf"},
            "visual": {"minimum_review_risk": 2},
        }
        return config, review

    def test_approved_low_confidence_page_warns_but_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = self._project(root)
            report = run_visual_qa(root, config)
            self.assertTrue(report["passed"])
            self.assertIn("visual.reading_order_reviewed", {item["id"] for item in report["checks"]})

    def test_visual_dependency_is_page_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, review = self._project(root)
            before_page_one = visual_dependency_hash(root, [1])
            before_unmapped = visual_dependency_hash(root, [2])
            review["findings"] = [{
                "id": "reading-order-1",
                "category": "reading_order",
                "severity": 2,
                "description": "Confirmed corrected column order.",
                "region_ids": [],
                "unit_ids": ["unit-0001"],
                "affects_translation": True,
                "resolution": "Use left column before right column.",
                "status": "resolved",
            }]
            atomic_write_json(root / "qa" / "visual-reviews" / "page-0001.json", review)
            self.assertNotEqual(before_page_one, visual_dependency_hash(root, [1]))
            self.assertEqual(before_unmapped, visual_dependency_hash(root, [2]))

    def test_missing_page_image_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = self._project(root)
            (root / "assets" / "page-images" / "page-0001.png").unlink()
            report = run_visual_qa(root, config)
            self.assertFalse(report["passed"])
            self.assertIn("visual.page_image", {item["id"] for item in report["checks"]})

    def test_unavailable_ocr_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, review = self._project(root)
            page_map_path = root / "analysis" / "page-map.json"
            page_map = __import__("json").loads(page_map_path.read_text(encoding="utf-8"))
            page = page_map["pages"][0]
            page.update({"ocr_status": "unavailable", "ocr_issues": ["tesseract_unavailable"], "risk": 3})
            atomic_write_json(page_map_path, page_map)
            review["page_hash"] = hash_data(semantic_page(page))
            atomic_write_json(root / "qa" / "visual-reviews" / "page-0001.json", review)
            report = run_visual_qa(root, config)
            self.assertFalse(report["passed"])
            self.assertIn("visual.ocr_unresolved", {item["id"] for item in report["checks"]})


def _scanned_pdf(path: Path, text: str, font: str, running_head: str = "") -> None:
    """One image-only page holding ``text``, optionally with a text-layer running head."""
    import fitz

    rendered = fitz.open()
    page = rendered.new_page(width=432, height=648)
    page.insert_textbox(fitz.Rect(48, 60, 384, 600), text, fontsize=12, fontname=font)
    image = page.get_pixmap(dpi=200).tobytes("png")
    rendered.close()
    document = fitz.open()
    page = document.new_page(width=432, height=648)
    page.insert_image(page.rect, stream=image)
    if running_head:
        page.insert_text((120, 24), running_head, fontsize=8)
    document.save(path)
    document.close()


class ScannedPageOcrTests(unittest.TestCase):
    ENGLISH = "The factory opened in the spring and hired workers from the nearby towns. " * 8
    CHINESE = "工厂在春天开张，从附近城镇雇用了四百名工人。每个工位都保存产量账簿。" * 8

    def setUp(self) -> None:
        from book_translation_pro.runtime import find_binary
        from book_translation_pro.visual import _tesseract_languages

        self.tesseract = find_binary("tesseract")
        if not self.tesseract or not {"eng", "osd"} <= _tesseract_languages(self.tesseract):
            self.skipTest("Tesseract with eng and osd is not installed")

    def _ocr(self, root: Path, languages: set[str] | None = None) -> tuple[dict, str]:
        from unittest.mock import patch

        from book_translation_pro.visual import augment_markdown_with_scanned_page_ocr

        config = {
            "source": {"path": "book.pdf", "format": "pdf", "language": "auto"},
            "visual": {"render_dpi": 180, "minimum_text_characters": 20, "ocr_mode": "auto", "ocr_languages": []},
        }
        markdown = root / "input.md"
        markdown.write_text("# Book\n", encoding="utf-8")
        if languages is None:
            report = augment_markdown_with_scanned_page_ocr(root, config, markdown)
        else:
            with patch("book_translation_pro.visual._tesseract_languages", return_value=languages):
                report = augment_markdown_with_scanned_page_ocr(root, config, markdown)
        return report, markdown.read_text(encoding="utf-8")

    def test_auto_language_reads_a_latin_page_in_english(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _scanned_pdf(root / "book.pdf", self.ENGLISH, "helv")
            report, markdown = self._ocr(root)
            self.assertEqual((report["recovered_pages"], report["issues"]), ([1], []))
            self.assertIn("factory", markdown)

    def test_auto_language_never_forces_a_cjk_page_through_english(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _scanned_pdf(root / "book.pdf", self.CHINESE, "china-s")
            report, markdown = self._ocr(root, {"eng", "osd"})
            self.assertEqual(report["recovered_pages"], [])
            self.assertTrue(report["issues"][0].startswith("page-0001:ocr_language_unavailable"), report["issues"])
            self.assertNotIn("OCR Recovered", markdown)

    def test_running_head_text_layer_does_not_hide_a_scanned_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _scanned_pdf(root / "book.pdf", self.ENGLISH, "helv", running_head="THE MACHINE AGE — CHAPTER ONE")
            report, _markdown = self._ocr(root)
            self.assertEqual(report["scanned_pages"], [1])

    def test_empty_ocr_output_is_reported_as_too_short(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _scanned_pdf(root / "book.pdf", self.ENGLISH, "helv")
            with patch("book_translation_pro.visual._run_ocr", return_value=("", None)):
                report, _markdown = self._ocr(root)
            self.assertEqual(report["issues"], ["page-0001:ocr_output_too_short"])

    def test_low_confidence_and_resolution_are_reported(self) -> None:
        from unittest.mock import patch

        seen: list[tuple[int, int]] = []

        def weak_ocr(image_path: Path, executable: Path, language: str) -> tuple[str, float]:
            with Image.open(image_path) as image:
                seen.append(image.size)
            return "garbled wrds frm a pr scan of the page", 31.0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _scanned_pdf(root / "book.pdf", self.ENGLISH, "helv")
            with patch("book_translation_pro.visual._run_ocr", side_effect=weak_ocr):
                report, _markdown = self._ocr(root)
            self.assertIn("page-0001:ocr_low_confidence:31", report["issues"])
            self.assertGreaterEqual(seen[0][0], 432 * 300 // 72)  # rendered at 300 DPI, not 180


if __name__ == "__main__":
    unittest.main()

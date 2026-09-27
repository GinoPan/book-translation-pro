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


if __name__ == "__main__":
    unittest.main()

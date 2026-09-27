from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from book_translation_pro.artifacts import atomic_write_json, atomic_write_text, safe_project_path
from book_translation_pro.build import build_outputs
from book_translation_pro.config import load_resolved_project
from book_translation_pro.manifest import load_manifest
from book_translation_pro.project import initialize_project, prepare_project
from book_translation_pro.qa import run_qa
from book_translation_pro.state import plan_run, record_text_output


LONG_SOURCE = os.environ.get("BTP_LONG_BOOK_FIXTURE")
LONG_PDF = os.environ.get("BTP_LONG_BOOK_PAGE_EVIDENCE")
EXPECTED_SHA256 = os.environ.get("BTP_LONG_BOOK_EXPECTED_SHA256")


@unittest.skipUnless(LONG_SOURCE, "set BTP_LONG_BOOK_FIXTURE to a public-domain EPUB/DOCX/PDF")
class LongBookBaselineTests(unittest.TestCase):
    def test_public_domain_long_book_resume_qa_and_markdown_build(self) -> None:
        source = Path(str(LONG_SOURCE)).resolve()
        self.assertTrue(source.is_file())
        if EXPECTED_SHA256:
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), EXPECTED_SHA256)
        if LONG_PDF:
            import fitz

            with fitz.open(Path(LONG_PDF).resolve()) as document:
                self.assertGreaterEqual(len(document), 100)

        root = Path(tempfile.mkdtemp(prefix="BTP long public-domain 中文 "))
        try:
            project = root / "Pride and Prejudice 验证"
            initialize_project(source, project, "en", "en", "fast", ["markdown"])
            prepared = prepare_project(project)
            self.assertGreater(prepared["unit_count"], 20)
            config, _ = load_resolved_project(project)
            manifest = load_manifest(project)
            ordered = sorted(manifest["units"], key=lambda item: item["order"])
            midpoint = len(ordered) // 2
            for index, entry in enumerate(ordered):
                capsule_path = safe_project_path(project, entry["capsule_file"])
                capsule = __import__("json").loads(capsule_path.read_text(encoding="utf-8"))
                capsule["summary"] = f"Public-domain baseline continuity {entry['unit_id']}."
                atomic_write_json(capsule_path, capsule)
                atomic_write_json(safe_project_path(project, entry["observation_file"]), {
                    "schema_version": 1, "new_entries": [], "conflicts": [],
                    "source_issues": [], "used_glossary_ids": [],
                })
                source_path = safe_project_path(project, entry["source_file"])
                target_path = safe_project_path(project, entry["draft_file"])
                atomic_write_text(target_path, source_path.read_text(encoding="utf-8"))
                record_text_output(project, entry["unit_id"], "draft")
                if index + 1 == midpoint:
                    partial = plan_run(project, False, config["execution"]["max_attempts_per_unit"])
                    self.assertEqual(len(partial["unchanged_unit_ids"]), midpoint)
                    self.assertEqual(len(partial["translation_unit_ids"]), len(ordered) - midpoint)

            resumed = plan_run(project, False, config["execution"]["max_attempts_per_unit"])
            self.assertFalse(resumed["blocked"])
            self.assertFalse(resumed["translation_unit_ids"])
            report = run_qa(project, config)
            self.assertTrue(report["passed"], report)
            build = build_outputs(project, config)
            self.assertIn("markdown", build["generated"])
            self.assertIn("xhtml", build["generated"])
        finally:
            cleanup = Path("\\\\?\\" + str(root)) if os.name == "nt" else root
            shutil.rmtree(cleanup)


if __name__ == "__main__":
    unittest.main()

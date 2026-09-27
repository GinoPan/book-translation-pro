from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from book_translation_pro.artifacts import atomic_write_json, sha256_file
from book_translation_pro.build import build_outputs, typeset_preview
from book_translation_pro.config import load_resolved_project
from book_translation_pro.project import initialize_project, prepare_project
from book_translation_pro.publication_review import record_publication_review
from book_translation_pro.qa import run_qa
from book_translation_pro.runtime import doctor_report, find_binary
from book_translation_pro.state import record_review

from .test_real_formats import complete_fake_semantic_work


RUN_PUBLICATION = os.environ.get("BTP_RUN_PUBLICATION_E2E") == "1"


@unittest.skipUnless(RUN_PUBLICATION, "set BTP_RUN_PUBLICATION_E2E=1 with EPUBCheck available")
class PublicationReleaseTests(unittest.TestCase):
    def test_reviewed_epub_candidate_is_promoted_without_rebuild(self) -> None:
        self.assertTrue(doctor_report()["publication_final_ready"])
        root = Path(tempfile.mkdtemp(prefix="BTP publication 中文 "))
        try:
            source_md = root / "source.md"
            source_md.write_text(
                "# Release Fixture\n\nOpening text with a semantic note.[^1]\n\n"
                "## Chapter One\n\nA value of 42 mm remains exact.\n\n[^1]: Bidirectional note text.\n",
                encoding="utf-8",
            )
            source = root / "source.docx"
            pandoc = find_binary("pandoc")
            assert pandoc
            subprocess.run([str(pandoc), str(source_md), "--standalone", "-o", str(source)], check=True)
            project = root / "正式 项目"
            initialize_project(source, project, "en", "en", "publication", ["docx", "epub", "bilingual"])
            prepare_project(project)
            complete_fake_semantic_work(project)
            config, _ = load_resolved_project(project)
            candidate = typeset_preview(project, config)
            self.assertEqual(candidate["publication_validation"]["checks"]["epubcheck"]["status"], "passed")
            self.assertEqual(candidate["publication_validation"]["checks"]["bilingual_epub_epubcheck"]["status"], "passed")
            self.assertTrue({"bilingual_markdown", "bilingual_docx", "bilingual_epub"}.issubset(candidate["generated"]))

            review = __import__("json").loads(
                (project / "qa" / "publication-review-template.json").read_text(encoding="utf-8")
            )
            review["reviewer"] = "integration-test"
            review_path = project / "qa" / "publication-review-input.json"
            atomic_write_json(review_path, review)
            record_publication_review(project, review_path)
            record_review(project, "publication_signoff", True, "integration-test", "Rendered candidate approved")
            qa = run_qa(project, config)
            self.assertTrue(qa["passed"], qa)
            final = build_outputs(project, config)
            self.assertEqual(
                final["generated"]["epub"]["sha256"],
                candidate["generated"]["epub"]["sha256"],
            )
            self.assertEqual(sha256_file(project / "dist" / "book.epub"), candidate["generated"]["epub"]["sha256"])
            self.assertEqual(
                sha256_file(project / "dist" / "book-bilingual.epub"),
                candidate["generated"]["bilingual_epub"]["sha256"],
            )
        finally:
            cleanup = Path("\\\\?\\" + str(root)) if os.name == "nt" else root
            shutil.rmtree(cleanup)


if __name__ == "__main__":
    unittest.main()

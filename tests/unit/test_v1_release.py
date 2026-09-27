from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from book_translation_pro.alignment import create_segment_alignment, render_bilingual_markdown
from book_translation_pro.artifacts import atomic_write_json, atomic_write_text, hash_data, sha256_file
from book_translation_pro.config import load_resolved_project, write_yaml
from book_translation_pro.exceptions import approved_exception_ids, save_exceptions
from book_translation_pro.glossary import load_effective_glossary, save_glossary
from book_translation_pro.migration import migrate_project
from book_translation_pro.publication import validate_xhtml_document
from book_translation_pro.publication_review import create_review_template, record_publication_review
from book_translation_pro.runtime_adapters import conformance_cases
from book_translation_pro.release import release_check
from book_translation_pro.state import load_state, record_review, save_state

from .test_state_and_qa import make_project


class V1ContractTests(unittest.TestCase):
    def test_v1_project_migration_preserves_completed_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "state").mkdir()
            raw = {
                "schema_version": 1,
                "project_id": "migration-test",
                "source": {"path": "source/original.epub", "language": "en", "format": "epub", "edition": ""},
                "target": {"language": "zh-Hans"},
                "mode": "study",
                "outputs": ["markdown"],
                "workspace": ".",
            }
            write_yaml(root / "book-project.yaml", raw)
            old_hash = hash_data(raw)
            atomic_write_json(root / "resolved-config.json", raw)
            atomic_write_text(root / "resolved-config.sha256", old_hash + "\n")
            record = {
                "status": "completed", "attempts": 1,
                "dependency_hashes": {"config": old_hash}, "output_hashes": {"draft": "a" * 64},
                "reason_codes": [],
            }
            state = {
                "schema_version": 1, "project_id": "migration-test", "source_fingerprint": "a" * 64,
                "resolved_config_hash": old_hash, "stages": {"qa": dict(record), "build": dict(record)},
                "units": {"unit-0001": dict(record)}, "human_reviews": [],
            }
            atomic_write_json(root / "state" / "run-state.json", state)
            atomic_write_json(root / "state" / "manifest.json", {
                "schema_version": 1, "source_fingerprint": "a" * 64, "resolved_config_hash": old_hash,
                "book_map_hash": "b" * 64, "style_guide_hash": "c" * 64, "units": [],
            })

            audit = migrate_project(root)
            resolved, new_hash = load_resolved_project(root)
            migrated = load_state(root)
            self.assertEqual(resolved["schema_version"], 2)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["units"]["unit-0001"]["status"], "completed")
            self.assertEqual(migrated["units"]["unit-0001"]["dependency_hashes"]["config"], new_hash)
            self.assertEqual(audit["work_units_invalidated"], 0)
            self.assertEqual(migrated["stages"]["qa"]["status"], "invalidated")

    def test_segment_alignment_drives_bilingual_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", "# 轴承\n\n轴承预紧为 10 mm。\n")
            alignment = create_segment_alignment(root, config)
            rendered = render_bilingual_markdown(alignment)
            self.assertEqual(alignment["items"][0]["source_segment_ids"], ["seg-000001"])
            self.assertIn("bearing preload is 10 mm", rendered)
            self.assertIn("轴承预紧为 10 mm", rendered)
            self.assertIn('data-alignment-id="align-0001"', rendered)

    def test_project_glossary_overlays_reusable_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "terminology" / "libraries").mkdir(parents=True)
            library_entry = {
                "id": "library-preload", "source": "bearing preload", "target": "预载", "aliases": [],
                "category": "technical_term", "domain": "mechanical", "grammatical": {}, "status": "approved",
                "confidence": "high", "scopes": [], "evidence": [], "notes": "", "frequency": 0,
            }
            project_entry = {**library_entry, "id": "project-preload", "target": "轴承预紧"}
            atomic_write_json(root / "terminology" / "libraries" / "机械 术语.json", {"schema_version": 1, "entries": [library_entry]})
            save_glossary(root, {"schema_version": 1, "entries": [project_entry]})
            config = {"terminology": {"libraries": ["terminology/libraries/机械 术语.json"]}}
            effective = load_effective_glossary(root, config)
            self.assertEqual([(item["id"], item["target"]) for item in effective["entries"]], [("project-preload", "轴承预紧")])

    def test_four_runtime_profiles_share_one_contract(self) -> None:
        result = conformance_cases()
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["profiles"]), 4)
        self.assertEqual(next(item for item in result["profiles"] if item["adapter"] == "sequential")["concurrency"], 1)

    def test_release_contract_is_self_consistent(self) -> None:
        result = release_check()
        self.assertEqual(result["version"], "1.0.0")
        self.assertTrue(result["contract_ready"], result)

    def test_publication_signoff_is_bound_to_candidate_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _ = make_project(root)
            candidate_file = root / "dist" / "typeset-preview" / "book.md"
            atomic_write_text(candidate_file, "# Book\n")
            atomic_write_json(root / "qa" / "typeset-preview.json", {
                "schema_version": 1, "status": "review_candidate", "clean_master_sha256": sha256_file(candidate_file),
                "publication_validation": {"passed": True, "errors": [], "warnings": []},
                "generated": {"markdown": {"path": "dist/typeset-preview/book.md", "sha256": sha256_file(candidate_file)}},
            })
            save_exceptions(root, {"schema_version": 1, "exceptions": []})
            template = create_review_template(root)
            template["reviewer"] = "tester"
            review_input = root / "qa" / "review-input.json"
            atomic_write_json(review_input, template)
            record_publication_review(root, review_input)
            state = load_state(root)
            state["human_reviews"].append({"checkpoint": "publication_signoff", "status": "pending"})
            save_state(root, state)
            record_review(root, "publication_signoff", True, "tester", "approved")
            signoff = next(item for item in load_state(root)["human_reviews"] if item["checkpoint"] == "publication_signoff")
            self.assertIn("clean_master", signoff["evidence_hashes"])
            self.assertIn("publication_review", signoff["evidence_hashes"])

    def test_xhtml_note_requires_backlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "book.xhtml"
            atomic_write_text(path, '<html xmlns="http://www.w3.org/1999/xhtml"><body><p><a id="ref1" role="doc-noteref" href="#note1">1</a></p><aside id="note1" role="doc-footnote"><p>Note</p></aside></body></html>')
            self.assertFalse(validate_xhtml_document(path)["passed"])
            atomic_write_text(path, '<html xmlns="http://www.w3.org/1999/xhtml"><body><p><a id="ref1" role="doc-noteref" href="#note1">1</a></p><aside id="note1" role="doc-footnote"><p>Note <a href="#ref1">back</a></p></aside></body></html>')
            self.assertTrue(validate_xhtml_document(path)["passed"])

    def test_approved_exceptions_are_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save_exceptions(root, {"schema_version": 1, "exceptions": [{
                "id": "EX-layout", "check_id": "publication.format_warning", "scope": "publication",
                "rationale": "Renderer limitation reviewed", "status": "approved", "approved_by": "tester",
                "approved_at": "2026-09-08T00:00:00Z", "evidence": ["qa/rendered/page-1.png"],
            }]})
            self.assertEqual(approved_exception_ids(root, "publication.format_warning", "publication"), ["EX-layout"])


if __name__ == "__main__":
    unittest.main()

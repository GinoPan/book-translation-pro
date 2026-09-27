from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book_translation_pro.artifacts import BTPError, atomic_write_json, atomic_write_text, sha256_file
from book_translation_pro.build import build_outputs
from book_translation_pro.config import resolve_project, write_yaml
from book_translation_pro.glossary import empty_glossary, save_glossary
from book_translation_pro.manifest import create_manifest
from book_translation_pro.qa import _number_tokens, _urls, run_qa
from book_translation_pro.state import (
    initialize_state,
    load_state,
    plan_run,
    record_failure,
    record_review,
    record_text_output,
    save_state,
)


def make_project(root: Path, with_term: bool = False, unit_count: int = 1) -> tuple[dict, dict]:
    for relative in (
        "source", "analysis", "terminology", "config", "source-work/work-units",
        "target/draft", "target/edited", "context/capsules", "state/observations", "qa",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "project_id": "state-test",
        "source": {"path": "source/original.epub", "language": "en", "format": "epub", "edition": ""},
        "target": {"language": "zh-Hans"},
        "mode": "study",
        "outputs": ["markdown"],
        "workspace": ".",
    }
    write_yaml(root / "book-project.yaml", config)
    resolved, config_hash = resolve_project(root)
    atomic_write_text(root / "source" / "original.epub", "fixture")
    atomic_write_text(root / "config" / "style-guide.md", "RULE-ID: voice\nNatural.\n")
    for number in range(1, unit_count + 1):
        atomic_write_text(
            root / "source-work" / "work-units" / f"unit-{number:04d}.md",
            f"# Bearing {number}\n\nbearing preload is {number * 10} mm.\n",
        )
    segment_ids = [f"seg-{number:06d}" for number in range(1, unit_count + 1)]
    book_map = {
        "schema_version": 1,
        "source_fingerprint": "a" * 64,
        "root_ids": ["section-0001"],
        "nodes": [{
            "id": "section-0001", "kind": "chapter", "order": 0,
            "title": {"source": "Bearing"}, "source_span": {"segment_ids": segment_ids},
            "children": [], "artifact_refs": [], "visual_refs": [], "note_refs": [], "status": "mapped",
        }],
    }
    atomic_write_json(root / "analysis" / "book-map.json", book_map)
    atomic_write_json(root / "analysis" / "book-profile.json", {"source_format": "epub"})
    atomic_write_text(
        root / "source-work" / "segments.jsonl",
        "".join(f'{{"id":"{segment_id}"}}\n' for segment_id in segment_ids),
    )
    glossary = empty_glossary()
    if with_term:
        glossary["entries"].append({
            "id": "preload", "source": "bearing preload", "target": "轴承预紧", "aliases": [],
            "category": "technical_term", "domain": "mechanical", "grammatical": {},
            "status": "approved", "confidence": "high", "scopes": [],
            "evidence": [{"source_ref": "seg-000001", "quote": "bearing preload"}],
            "notes": "", "frequency": 1,
        })
    save_glossary(root, glossary)
    units = []
    for number, segment_id in enumerate(segment_ids, 1):
        unit_id = f"unit-{number:04d}"
        capsule = {
            "schema_version": 1, "unit_id": unit_id, "book_node_id": "section-0001",
            "incoming_capsule_ids": [f"unit-{number - 1:04d}"] if number > 1 else [],
            "summary": f"Bearing preload overview {number}.", "continuity": [],
            "entities": [], "cross_references": [], "visual_continuations": [], "unresolved": [],
        }
        atomic_write_json(root / "context" / "capsules" / f"{unit_id}.json", capsule)
        atomic_write_json(root / "state" / "observations" / f"{unit_id}.json", {
            "schema_version": 1, "new_entries": [], "conflicts": [],
            "source_issues": [], "used_glossary_ids": [],
        })
        units.append({"id": unit_id, "order": number - 1, "book_node_id": "section-0001", "segment_ids": [segment_id]})
    manifest = create_manifest(root, units, "a" * 64, config_hash, book_map, sha256_file(root / "config" / "style-guide.md"))
    initialize_state(root, resolved["project_id"], "a" * 64, config_hash, manifest, ["terminology"])
    return resolved, manifest


class StateTests(unittest.TestCase):
    def test_chinese_number_boundaries_and_percent_equivalence(self) -> None:
        self.assertEqual(_number_tokens("第9章 图8.5"), {"9": 1, "8.5": 1})
        self.assertEqual(_number_tokens("32 percent"), _number_tokens("32%"))
        self.assertEqual(_number_tokens("1 and"), {"1": 1})

    def test_percentage_points_remain_distinct_from_percent(self) -> None:
        self.assertNotEqual(_number_tokens("32 percentage points"), _number_tokens("32%"))

    def test_url_tokens_stop_at_cjk_punctuation_but_keep_balanced_parentheses(self) -> None:
        self.assertEqual(
            _urls("见 https://example.org/guide，下一句"),
            {"https://example.org/guide": 1},
        )
        self.assertNotEqual(
            _urls("https://en.wikipedia.org/wiki/Function_(mathematics)"),
            _urls("https://en.wikipedia.org/wiki/Function_(physics)"),
        )
        self.assertEqual(
            _urls("见（https://example.org/a）"),
            {"https://example.org/a": 1},
        )

    def test_edit_invalidates_publication_signoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _ = make_project(root)
            state = load_state(root)
            state["human_reviews"].append({
                "checkpoint": "publication_signoff",
                "status": "approved",
                "reviewer": "tester",
                "recorded_at": "2026-09-05T00:00:00Z",
                "notes": "approved",
            })
            save_state(root, state)
            valid = "# Bearing 1\n\nbearing preload is 10 mm.\n"
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "edit")
            signoff = next(item for item in load_state(root)["human_reviews"] if item["checkpoint"] == "publication_signoff")
            self.assertEqual(signoff["status"], "pending")

    def test_resume_and_glossary_selective_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root, with_term=True)
            first = plan_run(root, True, 2)
            self.assertIn("checkpoint_pending:terminology", first["blocked"])
            record_review(root, "terminology", True, "tester", "approved")
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", "# 轴承\n\n轴承预紧为 10 mm。\n")
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", "# 轴承\n\n轴承预紧为 10 mm。\n")
            record_text_output(root, "unit-0001", "edit")
            stable = plan_run(root, True, 2)
            self.assertEqual(stable["translation_unit_ids"], [])
            self.assertEqual(stable["edit_unit_ids"], [])
            state = load_state(root)
            self.assertEqual(state["stages"]["translate"]["status"], "completed")
            self.assertEqual(state["stages"]["edit"]["status"], "completed")

            glossary_path = root / "terminology" / "glossary.json"
            glossary = __import__("json").loads(glossary_path.read_text(encoding="utf-8"))
            glossary["entries"][0]["target"] = "预加载"
            save_glossary(root, glossary)
            changed = plan_run(root, True, 2)
            self.assertEqual(changed["translation_unit_ids"], ["unit-0001"])
            self.assertIn("dependency_changed:glossary_selection", changed["details"]["unit-0001"])
            self.assertEqual(load_state(root)["units"]["unit-0001"]["status"], "invalidated")

    def test_qa_catches_number_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            record_review(root, "terminology", True, "tester", "approved")
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", "# 轴承\n\n数值为 11 mm。\n")
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", "# 轴承\n\n数值为 11 mm。\n")
            record_text_output(root, "unit-0001", "edit")
            report = run_qa(root, config)
            self.assertFalse(report["passed"])
            self.assertIn("content.numbers", [item["id"] for item in report["checks"]])

    def test_publication_qa_blocks_unconfirmed_distribution_rights(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            config = {**config, "mode": "publication"}
            report = run_qa(root, config)
            rights_failures = [
                item for item in report["checks"]
                if item["id"].startswith("rights.")
            ]
            self.assertEqual(len(rights_failures), 2)
            self.assertTrue(all(item["status"] == "fail" for item in rights_failures))
            self.assertEqual(
                {item["id"] for item in rights_failures},
                {"rights.publication_status", "rights.redistribution"},
            )

    def test_study_qa_warns_when_rights_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            report = run_qa(root, config)
            warning = next(item for item in report["checks"] if item["id"] == "rights.declaration")
            self.assertEqual(warning["status"], "warn")

    def test_qa_ignores_translated_punctuation_after_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            source = "# Bearing 1\n\nbearing preload is 10 mm. See https://example.org/guide, today.\n"
            target = "# 轴承 1\n\n轴承预紧为 10 mm。参见 https://example.org/guide，今天。\n"
            atomic_write_text(root / "source-work" / "work-units" / "unit-0001.md", source)
            manifest = __import__("json").loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            manifest["units"][0]["source_hash"] = sha256_file(root / "source-work" / "work-units" / "unit-0001.md")
            atomic_write_json(root / "state" / "manifest.json", manifest)
            record_review(root, "terminology", True, "tester", "approved")
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", target)
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", target)
            record_text_output(root, "unit-0001", "edit")
            report = run_qa(root, config)
            self.assertNotIn("content.urls", [item["id"] for item in report["checks"]])

    def test_half_complete_run_resumes_without_repeating_completed_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _ = make_project(root, unit_count=2)
            record_review(root, "terminology", True, "tester", "approved")
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", "# Bearing 1\n\nbearing preload is 10 mm.\n")
            record_text_output(root, "unit-0001", "draft")
            queue = plan_run(root, False, 2)
            self.assertIn("unit-0001", queue["unchanged_unit_ids"])
            self.assertEqual(queue["translation_unit_ids"], ["unit-0002"])

    def test_source_hash_drift_blocks_and_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _ = make_project(root)
            record_review(root, "terminology", True, "tester", "approved")
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", "# Bearing 1\n\nbearing preload is 10 mm.\n")
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "source-work" / "work-units" / "unit-0001.md", "changed source\n")
            queue = plan_run(root, False, 2)
            self.assertIn("source_hash_changed:unit-0001", queue["blocked"])
            self.assertIn("unit-0001", queue["translation_unit_ids"])

    def test_orphan_output_fails_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            record_review(root, "terminology", True, "tester", "approved")
            valid = "# Bearing 1\n\nbearing preload is 10 mm.\n"
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "edit")
            atomic_write_text(root / "target" / "edited" / "unit-9999.md", "orphan\n")
            report = run_qa(root, config)
            self.assertIn("unit.orphan", [item["id"] for item in report["checks"]])

    def test_missing_build_dependency_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            record_review(root, "terminology", True, "tester", "approved")
            valid = "# Bearing 1\n\nbearing preload is 10 mm.\n"
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "edit")
            with patch("book_translation_pro.publication.find_binary", return_value=None):
                with self.assertRaisesRegex(BTPError, "Pandoc is required"):
                    build_outputs(root, config)

    def test_missing_worker_observation_fails_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            record_review(root, "terminology", True, "tester", "approved")
            valid = "# Bearing 1\n\nbearing preload is 10 mm.\n"
            atomic_write_text(root / "target" / "draft" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "draft")
            atomic_write_text(root / "target" / "edited" / "unit-0001.md", valid)
            record_text_output(root, "unit-0001", "edit")
            (root / "state" / "observations" / "unit-0001.json").unlink()
            report = run_qa(root, config)
            self.assertIn("observation.invalid", [item["id"] for item in report["checks"]])

    def test_corrupt_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_project(root)
            atomic_write_text(root / "state" / "run-state.json", "{broken")
            with self.assertRaises(BTPError):
                load_state(root)

    def test_attempt_limit_blocks_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_project(root)
            record_review(root, "terminology", True, "tester", "approved")
            record_failure(root, "unit-0001", "first", 2)
            record_failure(root, "unit-0001", "second", 2)
            queue = plan_run(root, False, 2)
            self.assertIn("attempt_limit_reached:unit-0001", queue["blocked"])


if __name__ == "__main__":
    unittest.main()

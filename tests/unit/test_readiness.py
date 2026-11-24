from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from book_translation_pro.artifacts import BTPError, sha256_file
from book_translation_pro.readiness import readiness_report
from book_translation_pro.state import load_state, plan_run, record_review, save_state

from tests.unit.test_exhibits import _body, _diagram_pdf


def _book_with_an_unrecognised_exhibit(path: Path) -> None:
    """A diagram the tool treats, then a caption with nothing it can read under it."""
    _diagram_pdf(path)
    document = fitz.open(path)
    page = document.new_page()
    _body(page, 60)
    page.insert_text((72, 200), "EXHIBIT 1.2 Survey Form", fontname="hebo", fontsize=9)
    _body(page, 220)
    document.saveIncr()
    document.close()


def _prepare(root: Path, make_pdf) -> tuple[Path, dict]:
    from book_translation_pro.project import initialize_project, prepare_project

    make_pdf(root / "book.pdf")
    project = Path(initialize_project(root / "book.pdf", root / "project", "zh-Hans", "en", "study", ["markdown"])["project"])
    with mock.patch("book_translation_pro.ingest._convert_with_tools", side_effect=BTPError("no calibre in unit tests")):
        result = prepare_project(project)
    return project, result


def _review(project: Path) -> dict:
    return next(item for item in load_state(project)["human_reviews"] if item["checkpoint"] == "source_readiness")


def _edit_unit(project: Path, extra: str) -> None:
    """Change a unit's source and record its hash, as a hand fix does."""
    unit = project / "source-work" / "work-units" / "unit-0001.md"
    unit.write_text(unit.read_text(encoding="utf-8") + extra, encoding="utf-8")
    path = project / "state" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["units"][0]["source_hash"] = sha256_file(unit)
    path.write_text(json.dumps(manifest), encoding="utf-8")


class SourceReadinessTests(unittest.TestCase):
    def test_prepare_reports_an_unrecognised_exhibit_and_translation_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, result = _prepare(Path(directory), _book_with_an_unrecognised_exhibit)
            summary = result["source_readiness"]
            self.assertEqual((summary["status"], summary["blocking"]), ("blocked", 1))
            report = json.loads((project / "qa" / "source-readiness.json").read_text(encoding="utf-8"))
            (problem,) = [p for p in report["problems"] if p["severity"] == "block"]
            self.assertEqual((problem["kind"], problem["exhibit"]), ("exhibit_uncertain", "1.2"))
            self.assertIn("exhibit_uncertain", (project / "qa" / "source-readiness.md").read_text(encoding="utf-8"))
            self.assertEqual(_review(project)["status"], "pending")
            self.assertIn("checkpoint_pending:source_readiness", plan_run(project, True, 3)["blocked"])

    def test_blocking_problems_are_accepted_only_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _result = _prepare(Path(directory), _book_with_an_unrecognised_exhibit)
            with self.assertRaisesRegex(BTPError, "1 blocking problem"):
                record_review(project, "source_readiness", True, "reviewer", "")
            record_review(project, "source_readiness", True, "reviewer", "1.2 is a blank form; nothing to translate")
            self.assertEqual(_review(project)["status"], "approved")
            blocked = plan_run(project, True, 3)["blocked"]
            self.assertFalse([item for item in blocked if "source_readiness" in item])

    def test_a_clean_book_is_ready_and_approves_without_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, result = _prepare(Path(directory), _diagram_pdf)
            self.assertEqual((result["source_readiness"]["status"], result["source_readiness"]["blocking"]), ("ready", 0))
            record_review(project, "source_readiness", True, "reviewer", "")
            self.assertEqual(_review(project)["status"], "approved")

    def test_a_unit_changed_after_approval_makes_it_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _result = _prepare(Path(directory), _diagram_pdf)
            record_review(project, "source_readiness", True, "reviewer", "")
            _edit_unit(project, "\nA sentence added by hand.\n")
            self.assertIn("checkpoint_stale:source_readiness", plan_run(project, True, 3)["blocked"])
            # The old report no longer describes the units: approval needs a fresh one.
            with self.assertRaisesRegex(BTPError, "changed after the readiness report"):
                record_review(project, "source_readiness", True, "reviewer", "")
            readiness_report(project)
            record_review(project, "source_readiness", True, "reviewer", "")
            self.assertFalse([item for item in plan_run(project, True, 3)["blocked"] if "source_readiness" in item])

    def test_print_stamps_left_in_a_unit_are_listed_for_review_not_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _result = _prepare(Path(directory), _diagram_pdf)
            _edit_unit(project, "\nch01_4772.qxd 2/2/07 3:35 PM Page 12\n")
            summary = readiness_report(project)
            self.assertEqual((summary["status"], summary["by_kind"].get("print_stamps")), ("ready", 1))

    def test_a_project_prepared_before_the_gate_is_not_blocked_by_it(self) -> None:
        from book_translation_pro.config import persist_overrides
        from book_translation_pro.project import prepare_project

        with tempfile.TemporaryDirectory() as directory:
            project, _result = _prepare(Path(directory), _diagram_pdf)
            state = load_state(project)
            state["human_reviews"] = [r for r in state["human_reviews"] if r["checkpoint"] != "source_readiness"]
            save_state(project, state)  # as an older version of the tool left it
            persist_overrides(project, {"outputs": ["markdown", "docx"]})
            with mock.patch("book_translation_pro.ingest._convert_with_tools", side_effect=BTPError("no calibre")):
                refreshed = prepare_project(project)
            self.assertTrue(refreshed.get("config_refreshed"))
            self.assertEqual(_review(project)["status"], "not_required")
            self.assertFalse([item for item in plan_run(project, True, 3)["blocked"] if "source_readiness" in item])


if __name__ == "__main__":
    unittest.main()

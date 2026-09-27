from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema


SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "references" / "schemas"


def samples() -> dict[str, dict]:
    digest = "a" * 64
    record = {"status": "pending", "attempts": 0, "dependency_hashes": {}, "output_hashes": {}, "reason_codes": []}
    return {
        "project-config.schema.json": {
            "schema_version": 2, "project_id": "sample", "source": {"path": "source/original.epub", "language": "en"},
            "target": {"language": "zh-Hans"}, "mode": "study", "outputs": ["epub"], "workspace": ".",
            "metadata": {"title": "示例书", "original_title": "Example Book"},
            "rights": {"status": "unknown", "basis": "", "intended_use": "personal-study",
                       "redistribution_allowed": False, "source_upload_allowed": False,
                       "attribution": "", "notes": "", "include_notice_in_outputs": True},
        },
        "book-map.schema.json": {
            "schema_version": 1, "source_fingerprint": digest, "root_ids": ["chapter-1"],
            "nodes": [{"id": "chapter-1", "kind": "chapter", "order": 0, "title": {"source": "One"},
                       "source_span": {"segment_ids": ["seg-1"]}, "children": [], "status": "mapped"}],
        },
        "page-map.schema.json": {
            "schema_version": 1, "source_fingerprint": digest, "page_count": 1,
            "render": {"renderer": "pymupdf", "dpi": 180},
            "pages": [{"page_index": 1, "image_ref": "pages/0001.png", "page_kind": "body",
                       "image_sha256": digest, "pixel_width": 1200, "pixel_height": 1600,
                       "layout": "single_column", "reading_order_confidence": 1,
                       "text_characters": 10, "text_sha256": digest, "source_unit_ids": [],
                       "ocr_status": "not_needed", "ocr_issues": [], "regions": [], "risk": 0,
                       "risk_reasons": [], "review_status": "not_required"}],
        },
        "visual-review.schema.json": {
            "schema_version": 1, "page_index": 1, "page_hash": digest, "status": "approved",
            "reviewer": "tester", "reviewed_at": "2026-09-05T00:00:00Z", "notes": "", "findings": [],
        },
        "glossary.schema.json": {"schema_version": 1, "entries": []},
        "context-capsule.schema.json": {
            "schema_version": 1, "unit_id": "unit-1", "book_node_id": "chapter-1", "summary": "Summary",
            "continuity": [], "entities": [], "cross_references": [], "unresolved": [],
        },
        "manifest.schema.json": {
            "schema_version": 1, "source_fingerprint": digest, "resolved_config_hash": digest,
            "book_map_hash": digest, "style_guide_hash": digest, "units": [],
        },
        "run-state.schema.json": {
            "schema_version": 2, "project_id": "sample", "source_fingerprint": digest,
            "resolved_config_hash": digest, "stages": {"prepare": record}, "units": {},
            "metrics": {"started_at": "2026-09-08T00:00:00Z", "updated_at": "2026-09-08T00:00:00Z"},
        },
        "segment-alignment.schema.json": {
            "schema_version": 1, "source_fingerprint": digest, "target_language": "zh-Hans",
            "items": [{"id": "align-0001", "order": 0, "unit_id": "unit-0001", "book_node_id": "chapter-1",
                       "source_segment_ids": ["seg-1"], "source_text": "Source", "target_text": "目标",
                       "source_sha256": digest, "target_sha256": digest}],
        },
        "qa-exceptions.schema.json": {
            "schema_version": 1,
            "exceptions": [{"id": "EX-demo", "check_id": "visual.sample", "scope": "publication",
                            "rationale": "Reviewed source limitation", "status": "approved",
                            "approved_by": "tester", "approved_at": "2026-09-08T00:00:00Z",
                            "evidence": ["page 1"]}],
        },
        "publication-review.schema.json": {
            "schema_version": 1, "candidate_sha256": digest, "clean_master_sha256": digest,
            "reviewer": "tester", "reviewed_at": "2026-09-08T00:00:00Z",
            "checks": [{"id": check_id, "status": "approved", "notes": "checked"} for check_id in (
                "cover", "toc", "chapter_openings", "running_heads_feet", "figures", "dense_tables", "narrow_mobile"
            )],
        },
    }


class ContractTests(unittest.TestCase):
    def test_all_schemas_are_valid_and_have_legal_samples(self) -> None:
        schema_files = sorted(SCHEMA_ROOT.glob("*.schema.json"))
        self.assertEqual({path.name for path in schema_files}, set(samples()))
        for path in schema_files:
            schema = json.loads(path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.Draft202012Validator(schema).validate(samples()[path.name])

    def test_every_schema_rejects_wrong_version(self) -> None:
        for name, value in samples().items():
            schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
            invalid = {**value, "schema_version": 999}
            with self.assertRaises(jsonschema.ValidationError, msg=name):
                jsonschema.Draft202012Validator(schema).validate(invalid)

    def test_context_capsule_rejects_string_unresolved_items(self) -> None:
        value = samples()["context-capsule.schema.json"]
        value["unresolved"] = ["Does this note continue?"]
        schema = json.loads((SCHEMA_ROOT / "context-capsule.schema.json").read_text(encoding="utf-8"))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(value)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from book_translation_pro.alignment import create_segment_alignment, render_bilingual_markdown
from book_translation_pro.artifacts import BTPError, atomic_write_json, atomic_write_text, hash_data, sha256_file
from book_translation_pro.config import load_resolved_project, translation_config_hash, write_yaml
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
            resolved, _ = load_resolved_project(root)
            migrated = load_state(root)
            self.assertEqual(resolved["schema_version"], 2)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["units"]["unit-0001"]["status"], "completed")
            self.assertEqual(
                migrated["units"]["unit-0001"]["dependency_hashes"]["config"], translation_config_hash(resolved)
            )
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

    def test_a_source_table_and_its_translation_are_kept_apart_for_word(self) -> None:
        from book_translation_pro.alignment import TABLE_GAP

        def item(order: int, source: str, target: str) -> dict:
            return {"id": f"align-{order + 1:04d}", "order": order, "unit_id": f"unit-{order + 1:04d}",
                    "book_node_id": None, "source_segment_ids": ["seg-000001"], "source_text": source,
                    "target_text": target, "source_sha256": "a" * 64, "target_sha256": "b" * 64}

        alignment = {"schema_version": 1, "source_fingerprint": "c" * 64, "target_language": "zh-CN", "items": [
            item(0, "| Cost | 1 |\n|---|---|\n| Labor | 2 |", "| 成本 | 1 |\n|---|---|\n| 人工 | 2 |"),
            item(1, "Prose after the table.", "表格之后的文字。"),
        ]}
        rendered = render_bilingual_markdown(alignment)
        between = rendered[rendered.index("| Labor | 2 |"):rendered.index("| 成本 | 1 |")]
        self.assertIn(f"\n{TABLE_GAP}\n", between)            # Word would otherwise join the two tables
        self.assertEqual(rendered.count(TABLE_GAP), 1)        # and nowhere else

    def test_bilingual_sides_lose_print_residue_and_slivers_but_keep_text_and_pictures(self) -> None:
        from PIL import Image

        from book_translation_pro.publication import clean_bilingual_side
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = root / "assets" / "original" / "images"
            images.mkdir(parents=True)
            Image.new("RGB", (12, 40)).save(images / "000001.png")    # a sliver of a drawing
            Image.new("RGB", (300, 200)).save(images / "000002.jpg")  # a picture
            text = "\n".join([
                'Lean companies make money by flow. <span class="calibre3">ch07_4772.qxd 2/2/07 3:41 PM Page 160</span>',
                "",
                "**160** **Lean Accounting**",
                "",
                '<img src="assets\\original/images/000001.png" class="calibre52" />',
                "",
                '<img src="assets\\original/images/000002.jpg" class="calibre49" />',
                "",
                "**Exhibit 6.10** USCG Budget",
                "",
                "160 units were sold.",
            ])
            cleaned = clean_bilingual_side(root, text)
            self.assertEqual([line for line in cleaned.splitlines() if line.strip()], [
                "Lean companies make money by flow. ", "![](assets/original/images/000002.jpg)",
                "**Exhibit 6.10** USCG Budget", "160 units were sold.",   # a sentence opening with the page number stays
            ])
            # Without a print stamp there is no page to recognise a running head by.
            self.assertIn("**160** **Lean Accounting**", clean_bilingual_side(root, "**160** **Lean Accounting**"))

    def test_bilingual_items_leave_out_comment_only_units_and_stand_in_for_emptied_sources(self) -> None:
        from PIL import Image

        from book_translation_pro.publication import bilingual_items

        class Same:
            def source(self, text: str) -> str:
                return text

            def target(self, text: str) -> str:
                return text

        def item(order: int, source: str, target: str) -> dict:
            return {"id": f"align-{order + 1:04d}", "order": order, "unit_id": f"unit-{order + 1:04d}",
                    "book_node_id": None, "source_segment_ids": ["seg-000001"], "source_text": source,
                    "target_text": target, "source_sha256": "a" * 64, "target_sha256": "b" * 64}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = root / "assets" / "original" / "images"
            images.mkdir(parents=True)
            Image.new("RGB", (10, 10)).save(images / "000001.png")
            Image.new("RGB", (400, 300)).save(images / "exhibit-10-4.png")
            sliver = '<img src="assets/original/images/000001.png" class="calibre52" />'
            items = [item(0, "Text before.", "前文。"),
                     item(1, sliver, "### 图表10.4　将萨班斯风险与精益结合\n\n| 编号 |\n|---|\n| 1 |"),
                     item(2, sliver, "### 图表10.4续　将萨班斯风险与精益结合\n\n| 编号 |\n|---|\n| 2 |"),
                     item(3, sliver + "\n\n1O 2P", "<!-- 图表10.4的视觉切片；内容已在前文重建。 -->"),
                     item(4, "Text after.", "后文。"),
                     item(5, "# OCR Recovered Source Pages", "# OCR 恢复的源页面"),
                     item(6, "## Source Page 1", "## 源页面 1")]
            kept, skipped = bilingual_items(root, items, Same())
            self.assertEqual([i["unit_id"] for i in kept], ["unit-0001", "unit-0002", "unit-0003", "unit-0005"])
            self.assertEqual(kept[1]["source_text"], "![](assets/original/images/exhibit-10-4.png)")  # shown once
            self.assertEqual(kept[2]["source_text"], " ")                                     # then blank
            self.assertEqual(skipped, [{"unit_id": "unit-0004", "reason": "translation holds only a comment"}])

    def test_an_emptied_source_unit_is_skipped_only_when_its_translation_is_a_comment(self) -> None:
        from book_translation_pro.manifest import load_manifest
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = make_project(root)
            entry = load_manifest(root)["units"][0]
            atomic_write_text(root / entry["source_file"], "\n")
            target = root / "target" / "edited" / "unit-0001.md"
            atomic_write_text(target, "<!-- 本单元原文只剩图表碎片，已重建为下一单元中的表格。 -->\n")
            alignment = create_segment_alignment(root, config)
            self.assertEqual(alignment["items"], [])
            self.assertEqual([s["unit_id"] for s in alignment["skipped_units"]], ["unit-0001"])
            # A translation with real text under an empty source would vanish: fail loudly.
            atomic_write_text(target, "<!-- 说明 -->\n\n轴承预紧为 10 mm。\n")
            with self.assertRaisesRegex(BTPError, "source unit is empty but its translation is not"):
                create_segment_alignment(root, config)

    def test_bilingual_pairs_blocks_when_both_sides_have_the_same_shape(self) -> None:
        def item(source: str, target: str) -> dict:
            return {
                "id": "align-0001", "order": 0, "unit_id": "unit-0001", "book_node_id": None,
                "source_segment_ids": ["seg-000001"], "source_text": source, "target_text": target,
                "source_sha256": "a" * 64, "target_sha256": "b" * 64,
            }

        def render(source: str, target: str) -> str:
            return render_bilingual_markdown({
                "schema_version": 1, "source_fingerprint": "a" * 64, "target_language": "zh-Hans",
                "items": [item(source, target)],
            })

        paired = render("# Bearing\n\nFirst paragraph.\n\nSecond paragraph.", "# 轴承\n\n第一段。\n\n第二段。")
        blocks = [line for line in paired.splitlines() if line.startswith(":::") and "{" in line]
        self.assertEqual(len(blocks), 6)
        self.assertLess(paired.index("First paragraph."), paired.index("第一段。"))
        self.assertLess(paired.index("第一段。"), paired.index("Second paragraph."))
        # A merged paragraph changes the shape, so the unit is shown whole.
        whole = render("# Bearing\n\nFirst paragraph.\n\nSecond paragraph.", "# 轴承\n\n第一段。第二段。")
        self.assertEqual(len([line for line in whole.splitlines() if line.startswith(":::") and "{" in line]), 2)
        # A list item's indented continuation stands alone dedented, so its
        # image stays an image; an indented code block keeps its indentation.
        listed = render("1. Item\n\n    ![Flow](assets/flow.png)", "1. 项\n\n    ![流程](assets/flow.png)")
        self.assertIn("\n![Flow](assets/flow.png)\n", listed)
        self.assertIn("\n![流程](assets/flow.png)\n", listed)
        code = render("Run this:\n\n    make all\n    make test", "运行：\n\n    make all\n    make test")
        self.assertIn("\n    make all\n    make test\n", code)

    def test_bilingual_does_not_pair_blocks_that_are_shifted_by_dropped_page_furniture(self) -> None:
        def render(source: str, target: str) -> str:
            return render_bilingual_markdown({
                "schema_version": 1, "source_fingerprint": "a" * 64, "target_language": "zh-Hans",
                "items": [{
                    "id": "align-0001", "order": 0, "unit_id": "unit-0001", "book_node_id": None,
                    "source_segment_ids": ["seg-000001"], "source_text": source, "target_text": target,
                    "source_sha256": "a" * 64, "target_sha256": "b" * 64,
                }],
            })

        first = ("Flow principles and techniques are the key concepts behind designing and executing an "
                 "effective operation for any product or service focused lean enterprise today.")
        second = ("The design of lean manufacturing systems and equipment incorporates the essential principles "
                  "that guide successful lean organizations around the world today.")
        first_zh = "流动原则和技术，是为任何以产品或服务为重点的精益企业设计和执行有效运营的关键概念，在今天仍然如此。"
        second_zh = "精益制造系统和设备的设计包含了指导成功精益组织的基本原则，这些原则在世界各地的精益企业中都适用。"
        # The source carries a running head the translation dropped, so the block
        # counts match only by coincidence and every pair would be one paragraph off.
        shifted = render(f"JIM HUNTZINGER\n\n{first}\n\n{second}", f"{first_zh}\n\n{second_zh}\n\n**17**")
        self.assertEqual(len([line for line in shifted.splitlines() if line.startswith(":::") and "{" in line]), 2)
        self.assertIn("JIM HUNTZINGER", shifted)
        # The same unit with a true one-to-one translation is still paired.
        paired = render(f"{first}\n\n{second}", f"{first_zh}\n\n{second_zh}")
        self.assertEqual(len([line for line in paired.splitlines() if line.startswith(":::") and "{" in line]), 4)
        # A number that vanished means the target paragraph is another one.
        wrong_numbers = render(f"{first} It cost 1,250 dollars and 4,800 hours.\n\n{second}",
                               f"{second_zh}\n\n{first_zh}")
        self.assertEqual(len([line for line in wrong_numbers.splitlines() if line.startswith(":::") and "{" in line]), 2)

    def test_bilingual_keeps_id_anchors_namespaced_per_language_side(self) -> None:
        def item(order: int, source: str, target: str) -> dict:
            return {
                "id": f"align-{order + 1:04d}", "order": order, "unit_id": f"unit-{order + 1:04d}",
                "book_node_id": None, "source_segment_ids": [f"seg-{order + 1:06d}"],
                "source_text": source, "target_text": target,
                "source_sha256": "a" * 64, "target_sha256": "b" * 64,
            }

        rendered = render_bilingual_markdown({
            "schema_version": 1, "source_fingerprint": "a" * 64, "target_language": "zh-Hans",
            "items": [
                item(0, '<div class="topic"><span id="ch01"></span>Opening text.',
                     '<div class="topic"><span id="ch01"></span>开篇正文。'),
                item(1, '<span id="ch01"></span>See <a href="#ch01">the opening</a>.</div>',
                     '<span id="ch01"></span>见<a href="#ch01">开篇</a>。</div>'),
            ],
        })
        # One anchor per side (the repeat is dropped), links follow their side,
        # and the cross-unit wrapper div is gone.
        self.assertEqual(rendered.count('<span id="src-ch01"></span>'), 1)
        self.assertEqual(rendered.count('<span id="tgt-ch01"></span>'), 1)
        self.assertIn('<a href="#src-ch01">the opening</a>', rendered)
        self.assertIn('<a href="#tgt-ch01">开篇</a>', rendered)
        self.assertNotIn('class="topic"', rendered)

    def test_bilingual_namespaces_ids_on_content_anchors_per_side(self) -> None:
        def item(order: int, source: str, target: str) -> dict:
            return {
                "id": f"align-{order + 1:04d}", "order": order, "unit_id": f"unit-{order + 1:04d}",
                "book_node_id": None, "source_segment_ids": [f"seg-{order + 1:06d}"],
                "source_text": source, "target_text": target,
                "source_sha256": "a" * 64, "target_sha256": "b" * 64,
            }

        image = '<a href="img/f1.jpg" id="IMG_1" target="_parent"><img src="assets/f1.jpg" alt="{alt}" /></a>'
        rendered = render_bilingual_markdown({
            "schema_version": 1, "source_fingerprint": "a" * 64, "target_language": "zh-Hans",
            "items": [
                item(0, image.format(alt="Expand") + "\n\nSee [the note](#IMG_1) and <a href=\"#IMG_1\">figure</a>.",
                     image.format(alt="展开") + "\n\n见<a href=\"#IMG_1\">图</a>。"),
                # The same id repeating inside one side keeps only its first copy.
                item(1, image.format(alt="Expand again"), image.format(alt="再次展开")),
                # Extraction ids are not renamed: strip_extraction_links needs them.
                item(2, '<a id="calibre_link-7">Kept text</a>', '<a id="calibre_link-7">保留文本</a>'),
            ],
        })
        self.assertEqual(rendered.count('id="src-IMG_1"'), 1)
        self.assertEqual(rendered.count('id="tgt-IMG_1"'), 1)
        self.assertNotIn('id="IMG_1"', rendered)
        # Both repeated images still render; only the duplicate id is gone.
        self.assertEqual(rendered.count('alt="Expand again"'), 1)
        self.assertEqual(rendered.count('alt="再次展开"'), 1)
        self.assertIn('href="#src-IMG_1"', rendered)
        self.assertIn('href="#tgt-IMG_1"', rendered)
        self.assertIn("Kept text", rendered)
        self.assertNotIn("calibre_link", rendered)

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
            with self.assertRaisesRegex(BTPError, "pending checks: cover, toc"):
                record_publication_review(root, review_input)
            for check in template["checks"]:
                check["status"] = "approved"
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

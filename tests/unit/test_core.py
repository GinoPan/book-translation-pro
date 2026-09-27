from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book_translation_pro.artifacts import (
    BTPError,
    atomic_write_json,
    atomic_write_text,
    external_tool_path,
    hash_data,
    safe_project_path,
)
from book_translation_pro.config import resolve_config
from book_translation_pro.glossary import entry_matches, select_entries, validate_glossary
from book_translation_pro.runtime import doctor_report
from book_translation_pro.structure import (
    draft_book_map,
    expand_long_segments,
    make_work_units,
    parse_blocks,
)


class ArtifactTests(unittest.TestCase):
    def test_canonical_hash_ignores_dict_order(self) -> None:
        self.assertEqual(hash_data({"a": 1, "b": 2}), hash_data({"b": 2, "a": 1}))

    def test_safe_project_path_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(BTPError):
                safe_project_path(root, "../outside.txt")

    def test_atomic_writers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atomic_write_text(root / "nested" / "value.txt", "ok\n")
            atomic_write_json(root / "nested" / "value.json", {"b": 2, "a": 1})
            self.assertEqual((root / "nested" / "value.txt").read_text(encoding="utf-8"), "ok\n")
            self.assertEqual(json.loads((root / "nested" / "value.json").read_text()), {"a": 1, "b": 2})

    def test_external_tool_path_removes_windows_extended_prefix(self) -> None:
        value = external_tool_path(Path(r"\\?\C:\long 中文 path\book.epub"))
        if __import__("os").name == "nt":
            self.assertEqual(value, r"C:\long 中文 path\book.epub")


class RuntimeTests(unittest.TestCase):
    def test_powershell_alone_does_not_imply_word_publication_support(self) -> None:
        def locate(name: str):
            return Path(f"C:/{name}.exe") if name in {"pandoc", "powershell"} else None

        with (
            patch("book_translation_pro.runtime.find_binary", side_effect=locate),
            patch("book_translation_pro.runtime.binary_version", return_value="fixture"),
            patch("book_translation_pro.runtime.word_automation_available", return_value=False),
        ):
            report = doctor_report()
        self.assertTrue(report["binaries"]["powershell"]["found"])
        self.assertFalse(report["binaries"]["word"]["found"])
        self.assertFalse(report["publication_ready"])


class ConfigTests(unittest.TestCase):
    def base_config(self) -> dict:
        return {
            "schema_version": 1,
            "project_id": "sample",
            "source": {"path": "source/original.epub", "language": "en", "format": "epub", "edition": ""},
            "target": {"language": "zh-Hans"},
            "mode": "study",
            "outputs": ["epub"],
            "workspace": ".",
        }

    def test_study_defaults(self) -> None:
        resolved = resolve_config(self.base_config())
        self.assertTrue(resolved["passes"]["edit"])
        self.assertTrue(resolved["terminology"]["require_review_before_translation"])
        self.assertEqual(resolved["execution"]["max_parallel"], 8)

    def test_cli_overlay_wins(self) -> None:
        resolved = resolve_config(self.base_config(), {"mode": "fast", "target": {"language": "ja"}})
        self.assertEqual(resolved["mode"], "fast")
        self.assertEqual(resolved["target"]["language"], "ja")
        self.assertFalse(resolved["passes"]["edit"])


class StructureTests(unittest.TestCase):
    def test_front_matter_does_not_parent_body_chapters(self) -> None:
        segments = parse_blocks("Title page prose\n\n# Chapter One\n\nBody\n\n# Chapter Two\n\nMore")
        book_map, _ = draft_book_map(segments, "a" * 64)
        self.assertEqual(book_map["root_ids"], ["front-0001", "section-0002", "section-0003"])
        self.assertEqual(book_map["nodes"][0]["children"], [])

    def test_book_map_covers_every_segment_once(self) -> None:
        markdown = "Intro\n\n# One\n\nText A.\n\n## Two\n\nText B.\n"
        segments = parse_blocks(markdown)
        book_map, assigned = draft_book_map(segments, "a" * 64)
        mapped = [item for node in book_map["nodes"] for item in node["source_span"]["segment_ids"]]
        self.assertCountEqual(mapped, [item.id for item in assigned])
        self.assertEqual(len(mapped), len(set(mapped)))

    def test_long_paragraph_splits_and_preserves_node(self) -> None:
        segments = parse_blocks("# Heading\n\n" + ("word " * 1000))
        book_map, assigned = draft_book_map(segments, "a" * 64)
        expanded = expand_long_segments(assigned, 700)
        units = make_work_units(expanded, 800, 900)
        self.assertGreater(len(units), 1)
        self.assertTrue(all(unit["book_node_id"] for unit in units))
        self.assertTrue(book_map["nodes"])

    def test_fenced_block_stays_together(self) -> None:
        segments = parse_blocks("# H\n\n```python\na = 1\n\nb = 2\n```\n\nAfter")
        self.assertEqual([item.kind for item in segments], ["heading", "code", "paragraph"])


class GlossaryTests(unittest.TestCase):
    def entry(self) -> dict:
        return {
            "id": "bearing-preload",
            "source": "bearing preload",
            "target": "轴承预紧",
            "aliases": ["preload"],
            "category": "technical_term",
            "domain": "mechanical",
            "grammatical": {},
            "status": "approved",
            "confidence": "high",
            "scopes": [],
            "evidence": [{"source_ref": "seg-000001", "quote": "bearing preload"}],
            "notes": "",
            "frequency": 1,
        }

    def test_ascii_word_boundaries(self) -> None:
        entry = self.entry()
        entry["source"] = "cat"
        entry["aliases"] = []
        self.assertTrue(entry_matches(entry, "a cat waits"))
        self.assertFalse(entry_matches(entry, "category"))

    def test_scope_and_status(self) -> None:
        entry = self.entry()
        entry["scopes"] = ["section-a"]
        glossary = {"schema_version": 1, "entries": [entry]}
        self.assertEqual(len(select_entries(glossary, "preload", "section-a")), 1)
        self.assertEqual(len(select_entries(glossary, "preload", "section-b")), 0)

    def test_duplicate_surface_rejected(self) -> None:
        first = self.entry()
        second = {**self.entry(), "id": "other", "source": "preload", "aliases": []}
        with self.assertRaises(BTPError):
            validate_glossary({"schema_version": 1, "entries": [first, second]})


if __name__ == "__main__":
    unittest.main()

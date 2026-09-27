from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from book_translation_pro.config import load_yaml, resolve_config
from book_translation_pro.project import initialize_project
from book_translation_pro.rights import (
    insert_rights_notice,
    publication_rights_errors,
    rights_notice_markdown,
    rights_warning,
)


def base_config(**rights: object) -> dict:
    value = {
        "schema_version": 2,
        "project_id": "rights-test",
        "source": {
            "path": "source/original.epub",
            "language": "en",
            "format": "epub",
            "edition": "",
        },
        "target": {"language": "zh-Hans"},
        "mode": "study",
        "outputs": ["epub"],
        "workspace": ".",
        "metadata": {
            "title": "示例书",
            "original_title": "Example Book",
            "author": "Example Author",
            "translator": "Example Translator",
        },
    }
    if rights:
        value["rights"] = rights
    return value


class RightsTests(unittest.TestCase):
    def test_project_initialization_persists_rights_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.epub"
            source.write_bytes(b"epub fixture")
            project = root / "project"
            result = initialize_project(
                source,
                project,
                "zh-CN",
                "en",
                "publication",
                ["epub"],
                rights_status="authorized",
                rights_basis="Written permission",
                intended_use="publication",
                redistribution_allowed=True,
                rights_attribution="Original author",
            )
            stored = load_yaml(project / "book-project.yaml")
            self.assertEqual(stored["rights"]["status"], "authorized")
            self.assertTrue(stored["rights"]["redistribution_allowed"])
            self.assertFalse(stored["rights"]["source_upload_allowed"])
            self.assertNotIn("rights_warning", result)

    def test_unconfigured_project_defaults_to_local_non_distribution(self) -> None:
        resolved = resolve_config(base_config())
        self.assertEqual(resolved["rights"]["status"], "unknown")
        self.assertFalse(resolved["rights"]["redistribution_allowed"])
        self.assertFalse(resolved["rights"]["source_upload_allowed"])
        self.assertIsNotNone(rights_warning(resolved))

    def test_publication_requires_authority_redistribution_and_basis(self) -> None:
        unknown = resolve_config(base_config())
        self.assertEqual(len(publication_rights_errors(unknown)), 2)

        authorized = resolve_config(base_config(
            status="authorized",
            basis="Written authorization dated 2026-09-27",
            intended_use="publication",
            redistribution_allowed=True,
            source_upload_allowed=False,
            attribution="Original author and publisher",
            notes="",
            include_notice_in_outputs=True,
        ))
        self.assertEqual(publication_rights_errors(authorized), [])

        missing_basis = {**authorized, "rights": {**authorized["rights"], "basis": ""}}
        self.assertTrue(any("basis" in item for item in publication_rights_errors(missing_basis)))

    def test_notice_records_rights_without_claiming_certification(self) -> None:
        config = resolve_config(base_config(
            status="public-domain",
            basis="First published in 1900",
            intended_use="publication",
            redistribution_allowed=True,
            source_upload_allowed=False,
            attribution="Original author",
            notes="",
            include_notice_in_outputs=True,
        ))
        notice = rights_notice_markdown(config)
        self.assertIn("# 版权与授权", notice)
        self.assertIn("公有领域", notice)
        self.assertIn("Example Book", notice)
        self.assertIn("不构成法律意见或权利认证", notice)

    def test_notice_is_inserted_after_title_once(self) -> None:
        config = resolve_config(base_config())
        original = "# 示例书\n\n*Example Book*\n\n# 第一章\n\n正文。\n"
        rendered = insert_rights_notice(original, config)
        self.assertLess(rendered.index("*Example Book*"), rendered.index("# 版权与授权"))
        self.assertLess(rendered.index("# 版权与授权"), rendered.index("# 第一章"))
        self.assertEqual(insert_rights_notice(rendered, config), rendered)


if __name__ == "__main__":
    unittest.main()

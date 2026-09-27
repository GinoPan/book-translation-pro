"""Copyright, authorization, and distribution safeguards."""

from __future__ import annotations

from typing import Any


RIGHTS_STATUSES = (
    "unknown",
    "public-domain",
    "licensed",
    "authorized",
    "personal-research",
)
INTENDED_USES = (
    "personal-study",
    "internal-research",
    "publication",
    "commercial",
)
PUBLICATION_RIGHTS_STATUSES = frozenset({"public-domain", "licensed", "authorized"})

DEFAULT_RIGHTS: dict[str, Any] = {
    "status": "unknown",
    "basis": "",
    "intended_use": "personal-study",
    "redistribution_allowed": False,
    "source_upload_allowed": False,
    "attribution": "",
    "notes": "",
    "include_notice_in_outputs": True,
}

_STATUS_LABELS_ZH = {
    "unknown": "尚未确认",
    "public-domain": "公有领域",
    "licensed": "已取得许可",
    "authorized": "已取得权利人授权",
    "personal-research": "个人研究用途声明",
}
_STATUS_LABELS_EN = {
    "unknown": "Not confirmed",
    "public-domain": "Public domain",
    "licensed": "Licensed",
    "authorized": "Authorized by the rights holder",
    "personal-research": "Personal research use declared",
}
_USE_LABELS_ZH = {
    "personal-study": "个人学习",
    "internal-research": "内部研究",
    "publication": "公开发布",
    "commercial": "商业使用",
}
_USE_LABELS_EN = {
    "personal-study": "Personal study",
    "internal-research": "Internal research",
    "publication": "Public distribution",
    "commercial": "Commercial use",
}


def rights_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the effective rights declaration, including safe defaults."""
    value = dict(DEFAULT_RIGHTS)
    configured = config.get("rights", {})
    if isinstance(configured, dict):
        value.update(configured)
    return value


def publication_rights_issues(config: dict[str, Any]) -> list[dict[str, str]]:
    """Return stable blocking checks for a distribution-ready build."""
    rights = rights_config(config)
    issues: list[dict[str, str]] = []
    status = rights["status"]
    if status not in PUBLICATION_RIGHTS_STATUSES:
        issues.append({
            "id": "rights.publication_status",
            "message": (
                "Publication requires rights.status to be public-domain, licensed, "
                "or authorized"
            ),
        })
    if not rights["redistribution_allowed"]:
        issues.append({
            "id": "rights.redistribution",
            "message": "Publication requires rights.redistribution_allowed to be true",
        })
    if status in {"licensed", "authorized"} and not str(rights["basis"]).strip():
        issues.append({
            "id": "rights.authorization_basis",
            "message": "Licensed or authorized publication requires a non-empty rights.basis",
        })
    return issues


def publication_rights_errors(config: dict[str, Any]) -> list[str]:
    """Return blocking rights messages for callers that do not need check IDs."""
    return [item["message"] for item in publication_rights_issues(config)]


def rights_warning(config: dict[str, Any]) -> str | None:
    rights = rights_config(config)
    if rights["status"] == "unknown":
        return (
            "Copyright status is unknown. Keep the source and translation local, "
            "and confirm the right to translate or distribute before sharing the output."
        )
    if not rights["redistribution_allowed"]:
        return "The project declaration does not permit redistribution of the translation."
    return None


def rights_metadata_text(config: dict[str, Any]) -> str:
    """Create a compact rights value suitable for EPUB metadata."""
    rights = rights_config(config)
    status = rights["status"]
    basis = " ".join(str(rights["basis"]).split())
    redistribution = "yes" if rights["redistribution_allowed"] else "no"
    statement = f"Rights status: {status}. Redistribution allowed: {redistribution}."
    if basis:
        statement += f" Basis: {basis}."
    return statement


def _clean_inline(value: Any) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def rights_notice_markdown(config: dict[str, Any]) -> str:
    """Render the project rights declaration as a publication front-matter page."""
    rights = rights_config(config)
    metadata = config.get("metadata", {})
    target_language = str(config.get("target", {}).get("language", "")).casefold()
    chinese = target_language.startswith("zh")
    status = rights["status"]
    intended_use = rights["intended_use"]
    original_title = _clean_inline(metadata.get("original_title") or metadata.get("title"))
    author = _clean_inline(metadata.get("author"))
    translator = _clean_inline(metadata.get("translator"))
    attribution = _clean_inline(rights["attribution"])
    basis = _clean_inline(rights["basis"])
    notes = _clean_inline(rights["notes"])

    if chinese:
        lines = [
            "# 版权与授权",
            "",
            f"**权利状态：** {_STATUS_LABELS_ZH[status]}",
            f"**声明用途：** {_USE_LABELS_ZH[intended_use]}",
            f"**允许再分发：** {'是' if rights['redistribution_allowed'] else '否'}",
        ]
        if original_title:
            lines.append(f"**原著：** {original_title}")
        if author:
            lines.append(f"**原著作者：** {author}")
        if translator:
            lines.append(f"**译者：** {translator}")
        if attribution:
            lines.append(f"**署名要求：** {attribution}")
        if basis:
            lines.append(f"**授权依据：** {basis}")
        if notes:
            lines.append(f"**补充说明：** {notes}")
        lines.extend(["", _notice_paragraph_zh(status, rights["redistribution_allowed"]), ""])
        if not rights["source_upload_allowed"]:
            lines.extend([
                "源文件不得上传到未经授权的服务，也不得随本译本公开分发。",
                "",
            ])
        lines.append("本声明记录项目使用者提供的信息，不构成法律意见或权利认证。")
    else:
        lines = [
            "# Copyright and Authorization",
            "",
            f"**Rights status:** {_STATUS_LABELS_EN[status]}",
            f"**Declared use:** {_USE_LABELS_EN[intended_use]}",
            f"**Redistribution allowed:** {'Yes' if rights['redistribution_allowed'] else 'No'}",
        ]
        if original_title:
            lines.append(f"**Original work:** {original_title}")
        if author:
            lines.append(f"**Original author:** {author}")
        if translator:
            lines.append(f"**Translator:** {translator}")
        if attribution:
            lines.append(f"**Required attribution:** {attribution}")
        if basis:
            lines.append(f"**Authorization basis:** {basis}")
        if notes:
            lines.append(f"**Additional notes:** {notes}")
        lines.extend(["", _notice_paragraph_en(status, rights["redistribution_allowed"]), ""])
        if not rights["source_upload_allowed"]:
            lines.extend([
                "The source file must not be uploaded to an unauthorized service "
                "or distributed with this translation.",
                "",
            ])
        lines.append(
            "This notice records information supplied by the project user; "
            "it is not legal advice or rights certification."
        )
    return "\n".join(lines).strip() + "\n"


def _notice_paragraph_zh(status: str, redistribution_allowed: bool) -> str:
    if status == "public-domain":
        return "项目使用者声明原著已进入公有领域；本译本新增的翻译、编辑、排版与注释可能受到独立保护。"
    if status in {"licensed", "authorized"}:
        scope = "允许在所述授权范围内再分发。" if redistribution_allowed else "不得超出所述授权范围进行传播。"
        return "原著版权归原权利人所有。本译本的翻译、复制和分发受所述许可或授权约束；" + scope
    return "原著版权归原权利人所有。当前项目未记录足以支持公开分发的权利依据，本译本仅限声明用途。"


def _notice_paragraph_en(status: str, redistribution_allowed: bool) -> str:
    if status == "public-domain":
        return (
            "The project user declares that the original work is in the public domain. "
            "New translation, editing, layout, and annotations may carry separate rights."
        )
    if status in {"licensed", "authorized"}:
        scope = (
            "Redistribution is permitted only within the declared authorization."
            if redistribution_allowed
            else "Redistribution beyond the declared authorization is prohibited."
        )
        return (
            "Copyright in the original work remains with its rights holder. "
            "Translation, copying, and distribution are governed by the stated license "
            "or authorization. " + scope
        )
    return (
        "Copyright in the original work remains with its rights holder. "
        "This project does not record a sufficient basis for public distribution, "
        "so the translation is limited to the declared use."
    )


def insert_rights_notice(markdown: str, config: dict[str, Any]) -> str:
    """Insert one generated rights page after the translated title block."""
    rights = rights_config(config)
    if not rights["include_notice_in_outputs"]:
        return markdown
    marker = "<!-- btp:rights-notice -->"
    if marker in markdown:
        return markdown
    notice = marker + "\n\n" + rights_notice_markdown(config).rstrip() + "\n\n"
    lines = markdown.splitlines()
    title_index = next((index for index, line in enumerate(lines) if line.startswith("# ")), None)
    if title_index is None:
        return notice + markdown.lstrip()
    insert_at = title_index + 1
    while insert_at < len(lines):
        stripped = lines[insert_at].strip()
        if not stripped or (stripped.startswith("*") and stripped.endswith("*")):
            insert_at += 1
            continue
        break
    prefix = "\n".join(lines[:insert_at]).rstrip() + "\n\n"
    suffix = "\n".join(lines[insert_at:]).lstrip()
    return prefix + notice + suffix

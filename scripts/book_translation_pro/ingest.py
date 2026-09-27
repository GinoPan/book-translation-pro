"""Source conversion for PDF, DOCX, EPUB, and Windows HTML Help (CHM)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .artifacts import BTPError, atomic_write_text, portable_path, safe_project_path
from .runtime import find_binary


SUPPORTED_SUFFIXES = {".pdf": "pdf", ".docx": "docx", ".epub": "epub", ".chm": "chm"}


def source_format(path: Path, configured: str = "auto") -> str:
    if configured != "auto":
        return configured
    try:
        return SUPPORTED_SUFFIXES[path.suffix.lower()]
    except KeyError as exc:
        raise BTPError(f"Unsupported source format: {path.suffix or '<none>'}") from exc


def _run(command: list[str], cwd: Path, log_path: Path, timeout: int = 900) -> None:
    run_cwd = Path(sys.executable).parent if str(cwd).startswith("\\\\?\\") else cwd
    result = subprocess.run(
        command,
        cwd=run_cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    atomic_write_text(
        log_path,
        "$ " + " ".join(command) + "\n\nSTDOUT\n" + result.stdout + "\nSTDERR\n" + result.stderr,
    )
    if result.returncode:
        tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
        raise BTPError(f"Command failed ({result.returncode}): {' | '.join(tail)}")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as handle:
        for member in handle.infolist():
            candidate = (destination / member.filename).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise BTPError(f"Unsafe path in HTMLZ archive: {member.filename}") from exc
        handle.extractall(destination)


def _primary_html(root: Path) -> Path:
    html_files = [path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in {".html", ".xhtml", ".htm"}]
    if not html_files:
        raise BTPError("Extracted source did not contain an HTML document")
    preferred = [path for path in html_files if path.name.casefold() in {"index.html", "index.xhtml", "index.htm", "default.htm", "default.html"}]
    candidates = preferred or html_files
    return max(candidates, key=lambda path: path.stat().st_size)


class _HhcParser(HTMLParser):
    """Read the ordered Local entries from a compiled-help table of contents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._name = ""
        self.entries: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "param":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        param_name = values.get("name", "").casefold()
        value = unescape(values.get("value", "")).strip()
        if param_name == "name":
            self._name = value
        elif param_name == "local" and value:
            self.entries.append((value, self._name))
            self._name = ""


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _chm_topic_paths(root: Path) -> tuple[list[Path], Path | None]:
    """Return CHM topics in HHC order, with unlisted HTML appended deterministically."""
    html_files = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in {".html", ".xhtml", ".htm"}),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    if not html_files:
        raise BTPError("CHM extraction did not contain any HTML topics")
    hhc_files = sorted((path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() == ".hhc"), key=lambda path: path.name.casefold())
    toc_path = hhc_files[0] if hhc_files else None
    ordered: list[Path] = []
    if toc_path:
        parser = _HhcParser()
        try:
            parser.feed(_read_text(toc_path))
        except Exception as exc:
            raise BTPError(f"Could not parse CHM table of contents {toc_path.name}: {exc}") from exc
        root_resolved = root.resolve()
        for local, _title in parser.entries:
            local_path = local.split("#", 1)[0].replace("\\", "/")
            if not local_path:
                continue
            candidate = (root / unquote(local_path.lstrip("/"))).resolve()
            try:
                relative = candidate.relative_to(root_resolved)
            except ValueError:
                continue
            # Rebuild the path from the caller's root spelling. Windows may
            # return an 8.3 alias from resolve(), which would break later
            # relative_to() calls for paths containing spaces or non-ASCII text.
            candidate = root / relative
            if candidate.is_file() and candidate.suffix.casefold() in {".html", ".xhtml", ".htm"} and candidate not in ordered:
                ordered.append(candidate)
    for path in html_files:
        if path not in ordered:
            ordered.append(path)
    return ordered, toc_path


_HTML_URL_RE = re.compile(r"(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>['\"])(?P<value>.*?)(?P=quote)", re.IGNORECASE | re.DOTALL)
_HTML_ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_:][-A-Za-z0-9_.:]*)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_LINKED_IMAGE_RE = re.compile(
    r"(?P<open><a\b(?P<a_attrs>[^>]*)>)\s*"
    r"(?P<img><img\b(?P<img_attrs>[^>]*)>)\s*</a>",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".webp"}


def _rewrite_chm_url(value: str, topic: Path, root: Path) -> str:
    value = unescape(value.strip())
    if not value or value.startswith("#"):
        return value
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return value
    path_part = unquote(parsed.path.replace("\\", "/"))
    if not path_part:
        return value
    candidate = (root / path_part.lstrip("/")) if path_part.startswith("/") else (topic.parent / path_part)
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(root.resolve()).as_posix()
    except ValueError:
        return value
    return urlunsplit(("", "", relative, parsed.query, parsed.fragment))


def _promote_chm_linked_images(html: str, root: Path) -> tuple[str, int]:
    """Use the full-resolution image linked by a CHM thumbnail anchor.

    HTML Help books commonly render a 350px thumbnail in ``<img src=...>``
    and put the real figure in the surrounding ``<a href=...>``.  Pandoc
    extracts the thumbnail unless the image source is promoted before
    conversion.  The promotion is limited to distinct local image files, so
    navigation and ordinary hyperlinks are left untouched.
    """

    promoted = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal promoted
        anchor_attrs = {
            item.group("name").casefold(): item.group("value")
            for item in _HTML_ATTR_RE.finditer(match.group("a_attrs"))
        }
        image_attrs = {
            item.group("name").casefold(): item.group("value")
            for item in _HTML_ATTR_RE.finditer(match.group("img_attrs"))
        }
        href = anchor_attrs.get("href", "").strip()
        src = image_attrs.get("src", "").strip()
        if not href or not src or href.startswith("#") or src.startswith("#"):
            return match.group(0)
        href_parts = urlsplit(href)
        src_parts = urlsplit(src)
        if href_parts.scheme or href_parts.netloc or src_parts.scheme or src_parts.netloc:
            return match.group(0)
        href_path = unquote(href_parts.path.replace("\\", "/")).lstrip("/")
        src_path = unquote(src_parts.path.replace("\\", "/")).lstrip("/")
        if Path(href_path).suffix.casefold() not in _IMAGE_SUFFIXES:
            return match.group(0)
        linked = (root / href_path).resolve()
        thumbnail = (root / src_path).resolve()
        try:
            linked.relative_to(root.resolve())
            thumbnail.relative_to(root.resolve())
        except ValueError:
            return match.group(0)
        if not linked.is_file() or not thumbnail.is_file() or linked.resolve() == thumbnail.resolve():
            return match.group(0)
        image_tag = match.group("img")
        image_tag = re.sub(
            r"(?P<prefix>\bsrc\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
            lambda item: item.group("prefix") + item.group("quote") + href + item.group("quote"),
            image_tag,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        promoted += 1
        return match.group("open") + image_tag + "</a>"

    return _LINKED_IMAGE_RE.sub(replace, html), promoted


def _combine_chm_topics(root: Path, destination: Path) -> tuple[Path, Path | None, int]:
    """Create one HTML working document while retaining every extracted CHM topic."""
    topics, toc_path = _chm_topic_paths(root)
    sections: list[str] = []
    for topic in topics:
        source = _read_text(topic)
        body_match = re.search(r"<body\b[^>]*>(?P<body>.*?)</body\s*>", source, re.IGNORECASE | re.DOTALL)
        body = body_match.group("body") if body_match else source
        body = _HTML_URL_RE.sub(
            lambda match: match.group("prefix") + match.group("quote") + _rewrite_chm_url(match.group("value"), topic, root) + match.group("quote"),
            body,
        )
        relative = topic.relative_to(root).as_posix()
        sections.append(f'<section data-chm-topic="{relative}">\n{body}\n</section>')
    combined = "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>\n" + "\n".join(sections) + "\n</body></html>\n"
    atomic_write_text(destination, combined)
    return destination, toc_path, len(topics)


def convert_to_markdown(project: Path, config: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    source = safe_project_path(project, config["source"]["path"])
    if not source.is_file():
        raise BTPError(f"Configured source file is missing: {config['source']['path']}")
    fmt = source_format(source, config["source"].get("format", "auto"))
    calibre = find_binary("ebook-convert")
    pandoc = find_binary("pandoc")
    if not pandoc or (fmt != "chm" and not calibre):
        required_tools = (("pandoc", pandoc),) if fmt == "chm" else (("ebook-convert", calibre), ("pandoc", pandoc))
        missing = [name for name, value in required_tools if not value]
        raise BTPError(f"Missing required conversion tools: {', '.join(missing)}; run doctor")

    extracted = safe_project_path(project, "source-work/extracted")
    html_root = safe_project_path(project, "source-work/extracted/htmlz")
    htmlz = extracted / "input.htmlz"
    markdown = extracted / "input.md"
    logs = safe_project_path(project, "state/logs")
    extracted.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    if html_root.exists():
        verified = html_root.resolve()
        verified.relative_to(project.resolve())
        shutil.rmtree(verified)
    html_root.mkdir(parents=True)

    extractor_name = "calibre"
    topic_count: int | None = None
    toc_path: Path | None = None
    if fmt == "chm":
        extractor = find_binary("7z") or find_binary("7za")
        fallback = find_binary("hh") or find_binary("hh.exe")
        if not extractor and not fallback:
            raise BTPError("CHM input requires 7-Zip or Windows hh.exe; run doctor")
        if extractor:
            extractor_name = "7z"
            _run([str(extractor), "x", str(source), f"-o{html_root}", "-y"], project, logs / "chm-extract.log")
        else:
            extractor_name = "hh"
            _run([str(fallback), "-decompile", str(html_root), str(source)], project, logs / "chm-extract.log")
        extracted_root = html_root.resolve()
        for member in html_root.rglob("*"):
            try:
                member.resolve().relative_to(extracted_root)
            except ValueError as exc:
                raise BTPError(f"Unsafe path produced while extracting CHM: {member}") from exc
    else:
        _run([str(calibre), str(source), str(htmlz)], project, logs / "calibre-to-htmlz.log")
        _safe_extract(htmlz, html_root)
    if fmt == "chm":
        primary, toc_path, topic_count = _combine_chm_topics(html_root, html_root / "btp-chm-combined.html")
        combined, promoted_images = _promote_chm_linked_images(_read_text(primary), html_root)
        atomic_write_text(primary, combined)
    else:
        primary = _primary_html(html_root)
        promoted_images = 0
    media_dir = safe_project_path(project, "assets/original")
    media_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(pandoc),
            str(primary),
            "--from=html",
            "--to=gfm+raw_html",
            "--wrap=none",
            f"--extract-media={media_dir}",
            f"--resource-path={html_root}",
            "--output",
            str(markdown),
        ],
        project,
        logs / "pandoc-to-markdown.log",
    )
    converted = markdown.read_text(encoding="utf-8")
    roots = {str(project), str(project).replace("\\", "/")}
    if str(project).startswith("\\\\?\\"):
        plain = str(project)[4:]
        roots.update({plain, plain.replace("\\", "/")})
    for root in sorted(roots, key=len, reverse=True):
        converted = converted.replace(root + "\\", "").replace(root + "/", "")
    atomic_write_text(markdown, converted)
    if not markdown.is_file() or not markdown.read_text(encoding="utf-8").strip():
        raise BTPError("Source conversion produced an empty Markdown working draft")
    return markdown, {
        "format": fmt,
        "source": portable_path(source, project),
        "primary_html": portable_path(primary, project),
        "markdown": portable_path(markdown, project),
        "calibre": str(calibre) if fmt != "chm" else None,
        "pandoc": str(pandoc),
        "extractor": extractor_name,
        "topic_count": topic_count,
        "toc": portable_path(toc_path, project) if toc_path else None,
        "image_link_promotions": promoted_images,
    }

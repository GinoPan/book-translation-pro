"""Local capability discovery without provider-specific dependencies."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _candidate_paths(name: str) -> list[Path]:
    values: list[Path] = []
    local_app = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    if os.name == "nt":
        if name == "pandoc" and local_app:
            values.append(Path(local_app) / "Pandoc" / "pandoc.exe")
        if name == "ebook-convert" and program_files:
            values.append(Path(program_files) / "Calibre2" / "ebook-convert.exe")
        if name == "tesseract" and program_files:
            values.append(Path(program_files) / "Tesseract-OCR" / "tesseract.exe")
        if name in {"7z", "7za"} and program_files:
            values.append(Path(program_files) / "7-Zip" / f"{name}.exe")
        if name in {"hh", "hh.exe"}:
            values.append(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "hh.exe")
        if name in {"powershell", "powershell.exe"}:
            values.append(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        if name in {"soffice", "soffice.exe"} and program_files:
            values.append(Path(program_files) / "LibreOffice" / "program" / "soffice.exe")
    return values


def find_binary(name: str) -> Path | None:
    if name == "java":
        configured_java = os.environ.get("BTP_JAVA_PATH")
        if configured_java and Path(configured_java).is_file():
            return Path(configured_java)
    if name == "epubcheck":
        configured = os.environ.get("BTP_EPUBCHECK_PATH")
        if configured and Path(configured).is_file():
            return Path(configured)
    located = shutil.which(name)
    if located:
        return Path(located)
    for candidate in _candidate_paths(name):
        if candidate.is_file():
            return candidate
    return None


def epubcheck_command() -> list[str] | None:
    executable = find_binary("epubcheck")
    if executable:
        return [str(executable)]
    jar = os.environ.get("BTP_EPUBCHECK_JAR")
    java = find_binary("java")
    if jar and Path(jar).is_file() and java:
        return [str(java), "-jar", str(Path(jar))]
    return None


def word_automation_available() -> bool:
    """Detect registered Microsoft Word automation without launching Word."""
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"):
            return True
    except (ImportError, OSError):
        return False


def binary_version(path: Path) -> str:
    # hh.exe is a GUI helper with no stable version flag; probing it can open a
    # window or hang on some Windows installations, so report presence only.
    stem = path.stem.casefold()
    if stem == "hh":
        return "Windows HTML Help (hh.exe)"
    if stem == "soffice":
        return "LibreOffice (soffice)"
    if stem == "powershell":
        command = [str(path), "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"]
    elif stem in {"7z", "7za"}:
        command = [str(path), "i"]
    else:
        version_flag = "-v" if stem in {"pdfinfo", "pdftoppm", "pdftotext"} else "--version"
        command = [str(path), version_flag]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0] if output else f"exit {result.returncode}"


def doctor_report(mode: str = "study") -> dict[str, Any]:
    binaries: dict[str, Any] = {}
    for name in ("pandoc", "ebook-convert", "soffice", "powershell", "tesseract", "pdftoppm", "pdfinfo", "7z", "hh", "java", "epubcheck"):
        path = find_binary(name)
        binaries[name] = {
            "found": path is not None,
            "path": str(path) if path else None,
            "version": binary_version(path) if path else None,
        }
    word_ready = word_automation_available()
    binaries["word"] = {
        "found": word_ready,
        "path": None,
        "version": "Word.Application COM automation registered" if word_ready else None,
    }
    modules = {
        name: importlib.util.find_spec(import_name) is not None
        for name, import_name in {
            "PyYAML": "yaml",
            "jsonschema": "jsonschema",
            "beautifulsoup4": "bs4",
            "PyMuPDF": "fitz",
            "Pillow": "PIL",
            "python-docx": "docx",
        }.items()
    }
    chm_tools_ready = binaries["pandoc"]["found"] and (binaries["7z"]["found"] or binaries["hh"]["found"])
    chm_runtime_ready = chm_tools_ready and all(modules[name] for name in ("PyYAML", "jsonschema"))
    core_ready = all(binaries[name]["found"] for name in ("pandoc", "ebook-convert")) and all(modules.values())
    publication_ready = binaries["pandoc"]["found"] and modules["python-docx"] and (
        word_ready or binaries["soffice"]["found"]
    )
    epubcheck_ready = epubcheck_command() is not None
    if epubcheck_ready and not binaries["epubcheck"]["found"]:
        binaries["epubcheck"] = {
            "found": True,
            "path": os.environ.get("BTP_EPUBCHECK_JAR"),
            "version": "EPUBCheck Java archive",
        }
    mode_readiness = {
        "fast": core_ready,
        "study": core_ready,
        "publication": core_ready and publication_ready and epubcheck_ready,
    }
    return {
        "python": {"path": sys.executable, "version": sys.version.split()[0]},
        "binaries": binaries,
        "python_modules": modules,
        "v1_ready": core_ready and publication_ready and epubcheck_ready,
        "v0_1_ready": core_ready,
        "v0_2_ready": core_ready,
        "publication_ready": publication_ready,
        "publication_final_ready": publication_ready and epubcheck_ready,
        "epubcheck_ready": epubcheck_ready,
        "runtime_conformance": __import__(
            "book_translation_pro.runtime_adapters", fromlist=["conformance_cases"]
        ).conformance_cases(),
        "requested_mode": mode,
        "requested_mode_ready": mode_readiness.get(mode, False),
        "mode_readiness": mode_readiness,
        "ocr_ready": binaries["tesseract"]["found"],
        "chm_ready": chm_runtime_ready,
        "format_readiness": {
            "pdf": binaries["pandoc"]["found"] and binaries["ebook-convert"]["found"],
            "docx": binaries["pandoc"]["found"] and binaries["ebook-convert"]["found"],
            "epub": binaries["pandoc"]["found"] and binaries["ebook-convert"]["found"],
            "chm": chm_runtime_ready,
        },
    }

# Runtime compatibility

Read this when installing the skill or adapting dispatch to a host agent.

## Portable core

The canonical instructions live in `SKILL.md`. All semantic work is expressed as file-backed work items. A host adapter maps its native capabilities to reading files, writing assigned outputs, running deterministic commands, and dispatching one or more work items.

## Capability levels

- **Level A** — files + local commands + parallel workers. Use isolated work-unit workers up to resolved concurrency.
- **Level B** — files + local commands, no parallel workers. Execute the same queue sequentially.
- **Level C** — files only. Planning and manual semantic work are possible; deterministic validation/build steps are blocked and must be reported.

The four V1.0 conformance profiles are `sequential`, `local_parallel`, `host_workers`, and `external_queue`. They share the same file-backed work-item and commit contracts; only queue ownership and dispatch differ. Run `btp runtime-conformance` after installation.

## Dispatch invariants

- One work item is owned by at most one active worker.
- Workers receive absolute host paths but write portable relative paths into JSON.
- Workers cannot edit shared state.
- Coordinator validates and commits batches in stable work-unit order.
- A host interruption is treated as recoverable unless outputs fail validation.
- A platform-specific model or tool name may appear in local logs, never in the portable artifact contract.

## Discovery

At startup, record available commands and versions. On Windows, test explicit configured paths, PATH, and known application locations. Use the active Python interpreter for child scripts. Never execute a Windows Store placeholder as proof that Python is available.

V1.0 visual processing requires PyMuPDF and Pillow. Tesseract is optional for text-based PDFs but required when a scanned page must be recovered; install the requested source-language data or record a blocking OCR issue. A page is treated as scanned when it is mostly image and has little text outside the running-head and folio bands. OCR renders the page at 300 DPI or more and flags a mean word confidence below 60. With `source.language: auto`, Tesseract OSD (the `osd` data) detects each scanned page's script; a CJK verdict reads with every installed CJK language together because OSD confuses those scripts, and a script without installed data is reported rather than read as English. Poppler is a diagnostic/rendering fallback, while the canonical V1.0 Page Map renderer is PyMuPDF. CHM input requires 7-Zip (preferred) or Windows `hh.exe`; the extracted HTML then follows the same Pandoc/Markdown contract as the other text formats. DOCX and EPUB conversion require Calibre (`ebook-convert`) and Pandoc; CHM requires Pandoc. PDF conversion first writes a layout-cleaned copy of the source (`source-work/extracted/layout-clean.pdf`): running heads and folios are removed (including a short chapter's running head that repeats the chapter title without its label), vector figures are rendered as images together with their labels (axis and frame lines tie a chart's parts together, and short tick or value labels set smaller than the body or mostly digits are folded into the image), and ruled tables and footnotes with a matching body marker are replaced by placeholders. The cleaned copy goes through Calibre (with its automatic header/footer scan disabled, since Calibre 9.14 crashes on short PDFs and misses alternating running heads) and Pandoc, after which headings are restored from the PDF's font sizes, including short bold body-size headings; when either tool is missing or Calibre cannot reflow the file, PyMuPDF extracts the cleaned copy directly and `analysis/book-profile.json` records the fallback reason as a warning. Either way the placeholders become pipe tables and Markdown footnotes (`[^fnN]`), and paragraphs split by a page break are rejoined. Scanned pages without a text layer are passed through unchanged for OCR.

PDF-from-DOCX publication requires either a registered Microsoft Word automation server on Windows or LibreOffice `soffice` 7.4 or later (the PDF export passes JSON filter options). The presence of PowerShell alone is not evidence that Word export is available; `doctor` reports Word and LibreOffice separately. Only Word fills the generated TOC field: a LibreOffice-exported PDF of a book with a TOC fails validation until it is exported on a host with Word.

EPUB Publication requires EPUBCheck. Discovery checks PATH first and also accepts `BTP_EPUBCHECK_PATH` for an executable, or `BTP_EPUBCHECK_JAR` together with Java. `BTP_JAVA_PATH` may point to the Java executable. Run `btp doctor --mode publication`; a Study-ready host is not automatically Publication-ready.

## Thin platform metadata

`agents/openai.yaml`, project instruction files, and future host-specific manifests may improve discovery but must point to the canonical skill. They must not fork workflow, schemas, mode definitions, or completion rules.

## Windows installation

The canonical directory may be linked into more than one host's skill directory. The verified shared target is:

```text
D:\Skills\Book Translation Pro\book-translation-pro
```

Use directory links named `book-translation-pro` under `.agents\skills`, `.codex\skills`, and `.claude\skills`. Z Code and OpenClaw versions differ in their discovery conventions; configure their custom-skill or project-instruction feature to read the canonical `SKILL.md` and invoke `scripts\btp.py`. Do not maintain a copied fork.

The Python package can be installed from the release wheel or in editable form from the canonical directory. Both expose a `btp` console entry when the Python Scripts directory is on PATH and bundle the canonical Skill, schemas, templates, licenses, and references under the interpreter's shared-data directory. `btp release-check` verifies that the installed version and all 1.0 contracts are available without a development-repository path. Direct Skill invocation remains the current Python interpreter plus `<skill-root>\scripts\btp.py`.

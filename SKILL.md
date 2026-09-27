---
name: book-translation-pro
description: Translate book-length PDF, DOCX, EPUB, or CHM files with a resumable, structure-aware workflow. Use for multi-chapter translation projects, not short passages or ordinary one-off documents.
---

# Book Translation Pro

Build a faithful, readable target-language edition without losing book structure, figures, tables, notes, formulas, or resumability. The deterministic pipeline supports PDF Page Maps, page rendering, OCR recovery, layout-risk detection, visual evidence packages, review records, Visual QA, and publication-oriented Markdown, DOCX, EPUB, and PDF output.

## Operating principles

- Treat the source bytes and, for PDF, rendered source pages as authority. Extracted Markdown is an editable representation, not proof of layout or completeness.
- Let the active language agent author translation and editorial prose. Deterministic scripts handle extraction, hashing, manifests, validation, image operations, building, and issue localization.
- Keep shared state single-writer. Workers may write only their assigned translation, capsule, and observation artifacts.
- Give every durable object a stable ID and every reusable result a content/dependency hash. Never infer freshness from modification time alone.
- Never merge or publish when required chunks, mapped sections, assets, notes, or QA gates are missing.
- Preserve user instructions and authorization boundaries. Do not obtain, distribute, or upload source books beyond the user's requested local workflow.
- Treat copyright status, translation authority, source-upload permission, and redistribution permission as separate questions. Record the user's declaration without presenting it as legal advice or rights certification.
- Use runtime-neutral language. Discover the host's parallel-worker mechanism when available and fall back to sequential execution when it is not.

## Route the request

1. For a new translation project or a workflow run, read [workflow.md](references/workflow.md).
2. Before authoring a capsule, translation, observation, or edit, read [translation-protocol.md](references/translation-protocol.md).
3. For Fast, Study, or Publication behavior, read [modes.md](references/modes.md).
4. Before creating or changing project artifacts, read [data-contracts.md](references/data-contracts.md) and validate against the schemas in `references/schemas/`.
5. Before declaring a stage or book complete, read [quality-gates.md](references/quality-gates.md).
6. When adapting installation, commands, or parallel work to a host runtime, read [runtime-compatibility.md](references/runtime-compatibility.md).
7. Before generating review or final book files, read [publication-output.md](references/publication-output.md).
8. Before initializing a project, sending source content outside the local workflow, or preparing distribution-ready files, read [copyright-and-rights.md](references/copyright-and-rights.md).

Read only the references required for the current task.

## Required request fields

Infer these from the request and local context when safe:

- Source file path
- Target language and locale
- Mode: `fast`, `study` (default), or `publication`
- Requested output formats
- Project workspace path
- User terminology or style constraints
- Rights status, intended use, redistribution permission, source-upload permission, and a concise authorization basis when applicable

Ask only when a missing value would materially change the result. Unknown rights may proceed only as a warned local Fast/Study workflow. Publication requires a recorded `public-domain`, `licensed`, or `authorized` status plus explicit redistribution permission; licensed or authorized publication also requires a non-empty basis. Before expensive translation, visual processing, or publication work, present the resolved configuration, rights declaration, and any configured review checkpoint.

## Deterministic entry point

Use the active Python interpreter with `{baseDir}/scripts/btp.py`. Start with `doctor --mode <fast|study|publication>`. Use `release-check` to verify the installed 1.0 contracts and `runtime-conformance` to verify the four portable runtime profiles. Run `migrate <project-dir>` before resuming a schema-v1 project; migration preserves verified unit work and invalidates only the QA/build stages required by the new contract. Use the CLI for project creation, conversion, manifests, state, visual evidence, recording, QA, and builds; the language agent authors page reviews, capsules, translation, terminology decisions, and editing.

For PDFs, `prepare` creates the Page Map automatically. Inspect each required page with `show-page`, finalize its generated `qa/visual-reviews/page-####.json`, and commit it with `record --kind visual-review --input <file>`. Use `visualize` to add or refresh visual evidence for an already prepared project.

For CHM inputs, `prepare` decompiles the HTML Help container with 7-Zip (or Windows `hh.exe`), follows the extracted HHC table of contents, promotes a locally linked full-resolution figure over any thumbnail `<img>`, and records the topic count and promotion count in `analysis/book-profile.json`. CHM topics use the text pipeline; PDF Page Maps and page-level source visual review do not apply, but the generated DOCX/EPUB/PDF still requires the final rendered visual pass.

Use `reconstruct-figures <project-dir>` for PDF projects when a source exhibit is vector-only or extraction reduced it to scattered labels. The command creates deterministic crops from the authoritative rendered source pages (clustering page geometry so header rules and body text stay out of the crop), falls back to the page's own embedded scan with border trimming when an exhibit page has no usable vector geometry, and normalizes inverted dark-background line art back to dark-on-white (`normalize-images <project-dir>`; PDF sources only — CHM/EPUB images are as-authored by the publisher and may legitimately be dark photographs). It writes `qa/figure-reconstructions.json` with source page/crop geometry, and—when a target caption already exists—idempotently inserts the asset before that caption and removes only the safe, trailing calibre-wrapped OCR label block. It skips target locations that already contain a visual or a native table reconstruction; a declared structured-reconstruction observation records how extraction degraded and never blocks inserting an authoritative crop. Use `--keys 6.8,6.9` to force-regenerate named exhibits without touching reviewed siblings. Record any changed target units and reconstruction observations, then run `qa`; the source-PDF exhibit inventory must see the target asset or an explicit structured reconstruction before publication can proceed.

Use `typeset <project-dir>` to create a non-final layout candidate under `dist/typeset-preview/`. It cleans print-production residue, replaces copied source-page TOCs with a real Word TOC field, applies book styles and sections, joins clear cross-page sentence fragments when a paragraph ends in a dangling conjunction, emits persistent semantic XHTML, builds EPUB from that XHTML, and derives PDF from DOCX when Word or LibreOffice is available. When bilingual formats are configured, it also creates `target/alignment.json` and builds source/target pairs from those stable segment groups. The publication pass must also normalize raw HTML image tags into portable Markdown asset references, normalize path separators, restore extracted numeric footnote markers as superscripts while preserving quantities, repair extraction cases where a marker is placed before a classifier and protected term (for example, `11个 5S` must become `5S¹¹`), fit inline figures to the usable text width, keep figures and tables off page breaks — a figure never separates from its caption, and a table that fits on one page never splits (rows never break mid-row; oversized tables flow across pages with repeated header rows) — preserve short measurement tables as intact tables, and split only genuinely oversized tables. It must use distinct front-matter/body sections with Roman/Arabic page-numbering where applicable and different odd/even running heads for recto/verso pages. Page geometry follows the configured profile: Publication mode mirrors margins with a gutter for print; fast/study modes default to symmetric margins so consecutive pages stay visually aligned on screen (`publishing.mirror_margins` overrides). The folio follows the source edition's convention: `publishing.folio_position` defaults to `auto`, which samples the source PDF for whether page numbers share the top running-head line or sit in a footer, and composes the running head accordingly (verso: folio at the outer edge plus centered book title; recto: centered chapter title plus outer folio). `typeset` never marks the build gate complete.

For Publication mode, render and inspect the candidate, then use `publication-review <project-dir>` to refresh `qa/publication-review-template.json`. Complete every required check and record it with `record --kind publication-review --input <file>`. Put any accepted warning in the schema-valid `qa/exceptions.json` and record it with `record --kind exceptions --input <file>` before referencing its ID from the review. Finally record `publication_signoff` with `record --kind review --checkpoint publication_signoff --approve --reviewer <name>`. The signoff binds the clean master, candidate report, rendered review, approved exception list, and generated-file hashes. The final `build` command runs only after QA passes and promotes those exact reviewed candidate bytes; it does not silently rebuild a different edition.

For CHM or table-heavy sources, use `check-tables <project-dir>` before a long build to compare raw HTML tables, source pipe tables, cleaned tables, continuations, and unconverted markers. This diagnostic is supplementary; final validation must also compare generated DOCX/EPUB table and image inventories plus sampled content anchors with the clean Markdown master.

## Non-negotiable gates

- Source fingerprint recorded before derived artifacts are reused
- Configuration resolved and frozen for the run
- Book Map present before chunk translation
- Glossary and Style Guide present before parallel translation
- One source unit maps to exactly one current translation status
- Resume planner explains every rerun with dependency-based reason codes
- Completeness QA passes in every mode
- Every PDF page has a rendered image and Page Map entry
- Required reading-order, OCR, table, formula, and scan risks have finalized visual reviews
- Source-page figures reduced to OCR labels are either reconstructed from the authoritative rendered page or explicitly recorded as a missing-asset exception; reconstructed figures are checked in every requested output format
- PDF exhibit captions are inventoried from the source PDF itself, including vector-only drawings with no extracted image reference; an omitted target visual without an explicit reconstruction record is a blocking QA failure
- Visual QA passes before the completeness/build gate
- Publication candidates carry a clean-master hash and format-validation report; later edits invalidate publication signoff
- DOCX/EPUB table and embedded-image inventories do not shrink below the clean master, and sampled content anchors survive every generated format
- Publication mode requires an approved current signoff and an external EPUBCheck pass when EPUB is requested
- The final visual pass checks the cover, TOC, running heads/feet, representative figures, risk tables, and dense tables after rendering—not only the source Markdown
- Known limitations and skipped human reviews are reported explicitly
- Fast/Study projects with unknown or non-redistributable rights remain visibly marked for local use; Publication is blocked without a qualifying rights status and explicit redistribution permission
- Reader-facing outputs include the generated rights notice unless an equivalent reviewed notice is explicitly configured

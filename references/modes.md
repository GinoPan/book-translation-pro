# Modes

Modes are policy bundles. Explicit configuration overrides are allowed, but the resolved configuration must record every override.

The text pipeline supports PDF, DOCX, EPUB, and CHM plus PDF visual evidence/QA and publication-oriented output. CHM is treated as an HTML Help topic collection: its HHC order is preserved, while PDF Page Map and page-level visual QA do not apply. Publication completion remains blocked until format validation, an up-to-date typeset candidate, external EPUBCheck when EPUB is requested, and final signoff are satisfied.

## Shared invariants

Every mode records source fingerprint, resolved configuration, Book Map, Glossary, Style Guide, manifest, Run State, complete source-to-target coverage, and a final limitations report. Fast means fewer semantic/visual passes, never permission to omit content.

In Study mode, a passing automated QA report does not prove that pagination and typesetting are acceptable. Before sharing a reader-facing edition, run `typeset` and visually inspect the title matter, TOC, representative figures, risk tables, dense tables, and chapter openings. A TOC that retains Roman source-page numbers is a symptom that chapter/TOC recognition failed and must be corrected before relying on navigation.

| Policy | Fast | Study (default) | Publication |
|---|---|---|---|
| Primary use | Rapid personal reading | Serious study and internal reference | Distribution-ready candidate |
| Book Map | Required, basic hierarchy | Required, full logical structure | Required, full structure and publishing order |
| Page Map for PDF | All pages; review risk 3 | All pages and regions; review risks 2–3 | All pages and regions; review every page |
| Glossary bootstrap | Small sample; auto-approve low-risk terms | Representative sample; human checkpoint default | Broad sample; human approval required |
| Context Capsule | Previous continuity + core entities | Chapter summary, continuity, entities, references, unresolved issues | Study plus editorial voice and proof notes |
| Initial translation | One pass | One pass | One pass from source evidence |
| Edit Pass | Only flagged issues and joins | Every unit or section | Full editorial pass plus independent proof pass |
| Visual QA | Level 3 and extraction failures | Levels 2–3 plus samples | All figures/tables/formulas/notes and page sampling |
| Completeness QA | Required, blocking | Required, blocking | Required, blocking |
| Terminology QA | Deterministic scan | Scan + semantic review of conflicts | Full review and signed exceptions |
| Human checkpoints | Optional | Terminology checkpoint default | Terminology, visual exceptions, final signoff |
| Output | Reading-oriented | DOCX/EPUB/PDF as requested | Semantic XHTML/EPUB/DOCX/PDF and optional alignment-based bilingual editions |

## Visual risk levels

- **Level 0** — ordinary photograph or decorative asset; preserve and verify presence.
- **Level 1** — simple figure with a caption and little/no embedded source text; translate caption and preserve placement.
- **Level 2** — embedded text, moderate table, sidebar, multi-column interaction, or note-dense layout; visual review required in Study and Publication.
- **Level 3** — complex technical diagram, dense table, formulas mixed with prose, uncertain reading order, low OCR confidence, or meaning dependent on spatial layout; visual review required in every mode.

## Mode resolution

Apply defaults first, then user overrides, then capability-based degradation. A degradation may only reduce an optional feature. If it would violate a shared invariant or a Publication requirement, block and explain the missing capability.

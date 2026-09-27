# Data contracts

Read this before creating, validating, or migrating project artifacts.

## General rules

- Every JSON artifact has `schema_version`.
- IDs are stable, unique within their namespace, and never derived solely from translated text.
- Stored paths are project-relative, use `/`, and must not escape the project root.
- JSON used for hashing is UTF-8 and canonically serialized with sorted object keys.
- Durable writes use a temporary file on the same volume, validation, then atomic replacement.
- Times are ISO 8601 UTC observations, never freshness evidence.
- Unknown or unresolved values are explicit; do not invent certainty to satisfy a schema.

## Configuration

The user-facing file is `book-project.yaml`. V1.0 freezes project configuration and Run State at schema version 2. Resolve mode defaults, environment paths, and explicit overrides into `resolved-config.json`; validate against `schemas/project-config.schema.json`; hash the resolved JSON. Never resume under a different resolved hash without planning invalidation. Use `btp migrate` for version-1 projects; do not rewrite released state ad hoc. Optional `metadata.original_title` is the authoritative source-title line for a translated title page; conservative CIP inference is used only when it is absent.

The resolved `rights` object records status, basis, intended use, redistribution permission, source-upload permission, attribution, notes, and whether generated outputs include the rights notice. Missing legacy declarations resolve to the conservative defaults `unknown`, `personal-study`, no redistribution, no source upload, and notice enabled. This record is a user declaration, not legal advice or rights certification. Changing it changes the resolved configuration hash and invalidates dependent publication evidence.

## Book Map

`analysis/book-map.json` is the canonical whole-book logical order. It contains typed nodes for front matter, parts, chapters, appendices, notes, bibliography, index, and other preserved material. Each node links source pages/segments to target artifacts and visual/note references. Validate with `schemas/book-map.schema.json`.

## Page Map

`analysis/page-map.json` is the V1.0 PDF/page evidence index. Coordinates are normalized `[0,1]` objects with `x`, `y`, `width`, `height` to remain independent of render DPI. Every page records its rendered image/hash, layout, reading-order confidence, mapped units, OCR state, regions, risk, and disposition. CHM/DOCX/EPUB projects do not create Page Maps. Validate with `schemas/page-map.schema.json`.

`analysis/visual-assets.json` records page images, risk crops, dimensions, hashes, and contact sheets. `qa/visual-reviews/page-####.json` stores a page-semantic hash, final review status, and evidence-backed findings; validate reviews with `schemas/visual-review.schema.json`. A stale page hash invalidates the review.

`qa/figure-audit.json` inventories exhibit/figure/table captions found directly in a PDF's text layer and records source pages, vector/image evidence, target units, target assets, reconstruction exceptions, and status. It is required for PDF projects because vector-only figures have no source Markdown image reference to compare.

`qa/figure-reconstructions.json` records deterministic source-visual crops created by `reconstruct-figures`. Each generated item includes the exhibit key, source page, caption and crop bounding boxes, pixel crop, caption orientation, target asset path, and render method. Existing assets are listed as skipped unless `--overwrite` is supplied; exhibits without adjacent vector or embedded-image geometry are listed as unavailable and require an explicit editorial decision.

## Glossary

`terminology/glossary.json` stores approved and proposed source/target pairs, aliases, domain, scope, status, confidence, evidence, and notes. Optional reusable libraries are loaded from the portable paths in `terminology.libraries`; listed libraries overlay in order, the project Glossary wins, and the effective result is snapshotted for reproducibility. Worker observations are separate append-only files until the coordinator merges them. Validate with `schemas/glossary.schema.json`.

## Style Guide

`config/style-guide.md` is human-editable. Machine-sensitive rules use stable markers such as `RULE-ID: terminology.first-use` and optional scope notes. V1.0 hashes the complete Style Guide, so any change conservatively invalidates every unit. Per-rule scoped invalidation remains a future optimization.

## Context Capsules

`context/capsules/<unit-id>.json` summarizes continuity, not source evidence. It links claims to segment/page IDs where possible and records unresolved questions. Validate with `schemas/context-capsule.schema.json`.

## Manifest

`state/manifest.json` indexes source segments, work units, expected outputs, order, and hashes. Completeness means a bijection between required current work units and verified outputs, plus Book Map coverage. Extra orphan outputs are errors until classified.

## Run State

`state/run-state.json` records stage and unit status, attempts, exact dependency hashes, output hashes, reason codes, and optional duration/token/cost metrics. Allowed unit states are `pending`, `running`, `completed`, `failed_retryable`, `failed_terminal`, `invalidated`, and `skipped_approved`. Validate with `schemas/run-state.schema.json`.

## Alignment and bilingual editions

`target/alignment.json` stores stable unit and segment-group mappings, source/target hashes, and publication order. Bilingual Markdown/XHTML/DOCX/EPUB/PDF must be generated from these groups rather than paragraph-position guesses. Validate with `schemas/segment-alignment.schema.json`.

## Publication review and QA exceptions

`qa/exceptions.json` stores explicit check IDs, scope, rationale, approver, status, and evidence. Only approved current IDs can satisfy Publication warnings; validate with `schemas/qa-exceptions.schema.json`.

`qa/publication-review.json` binds seven rendered-output checks to the current `qa/typeset-preview.json` and clean-master hashes. Validate with `schemas/publication-review.schema.json`. The `publication_signoff` Run State review additionally stores hashes for the candidate report, review, exception file, clean master, and every generated candidate. Any mismatch makes it stale.

## Translation observations

Workers may propose terms, aliases, attributes, source corrections, structure corrections, visual issues, and editorial risks. Every proposal includes evidence references and confidence. The coordinator applies approved changes transactionally; rejected or unresolved proposals remain auditable.

## QA reports

Each QA report has checks with stable IDs, `pass/warn/fail`, evidence, affected artifact IDs, whether failure blocks the selected mode, a numeric score, and aggregated attempts/duration/token/cost metrics. Human review and approved exceptions are separate objects; a numeric score cannot convert a blocking failure into pass.
### `qa/figure-reconstructions.json`

For PDF inputs, `reconstruct-figures` writes a deterministic source-visual record. Each generated item records the exhibit key, source page, source caption bounding box, inferred crop bounding box, pixel crop, orientation relative to the caption, target asset path, and render method. Existing assets are reported as `skipped` unless `--overwrite` is supplied; exhibits without adjacent vector or embedded-image geometry are reported as `unavailable` and require an explicit editorial decision. `target_repairs.repaired` records automatic caption placement, safe OCR-fragment removal, or a skip reason such as `existing_target_visual`, `native_table_reconstruction`, or `declared_structured_reconstruction`; `target_repairs.unresolved` records captions that still need editorial mapping.

# Workflow

Read this reference for project setup, planning, implementation, or an eventual translation run.

## Stage graph

1. **Collect** — resolve source, target language, mode, outputs, workspace, user constraints, rights status, intended use, source-upload and redistribution permissions, and review checkpoints.
2. **Capability scan** — detect required and optional local tools; decide run, degrade, or block.
3. **Initialize** — copy the project template, fingerprint the source, resolve configuration, and create the artifact directories.
4. **Profile** — identify format, length, language, extractability, sections, assets, scan/OCR status, columns, tables, formulas, and risk. For CHM, record thumbnail-to-full-resolution image promotions and verify that the publication asset directory contains the promoted files.
5. **Map** — build Book Map in every mode; build Page Map according to source type and mode.
6. **Terminology/style checkpoint** — build Glossary and Style Guide, render a human review view, and pause if configured.
7. **Plan work units** — align segments, choose structure-preserving boundaries, compute dependency hashes, and create Context Capsules.
8. **Translate** — dispatch isolated work units in deterministic batches. Each worker writes only its draft, capsule update, and evidence-backed observations.
9. **Commit batch** — validate worker outputs, record the dependency state used by the batch, then merge observations through the single writer.
10. **Edit** — revise target-language readability and continuity without changing factual or structural invariants.
11. **Visual QA** — inspect the risk set required by the selected mode; correct Page Map or source segments before patching downstream work.
12. **Completeness QA** — validate one-to-one coverage, structure, terminology, numbers, references, notes, and assets.
13. **Build** — merge by Book Map order, clean print-production residue, and generate publication-oriented formats from verified artifacts. DOCX drives fixed-layout PDF; EPUB is generated independently from the clean Markdown master.
14. **Final review/report** — record automatic gates, human review scope, exceptions, limitations, outputs, and resumable state.

## V1.0 command flow

Let `BTP` mean the current Python interpreter followed by `<skill-root>/scripts/btp.py`.

1. Verify the host:

   ```text
   BTP release-check
   BTP runtime-conformance
   BTP doctor --mode <fast|study|publication>
   ```

   For an existing schema-v1 project, run `BTP migrate <project-dir>` once before `prepare`, `plan`, or `qa`. The migration receipt is retained under `state/migrations/`.

2. Create a project when one does not already exist. Record the user's rights declaration; `unknown` is safe for a warned local Study project, while a distribution-ready Publication project requires a qualifying status and explicit redistribution permission:

   ```text
   BTP init <source.pdf|source.docx|source.epub|source.chm> --project <project-dir> --target <language> --mode study --output docx --output epub --output pdf --style-template academic --rights-status personal-research --intended-use personal-study
   ```

   Available Style Guide templates are `general`, `academic`, `technical`, and `business`. Reusable terminology-library paths are declared in `terminology.libraries`; they are applied in listed order and the project Glossary wins conflicts.

3. Convert, map, chunk, and initialize state:

   ```text
   BTP prepare <project-dir>
   ```

4. For PDF, inspect every page listed in `analysis/visual-summary.json.required_review_pages`:

   ```text
   BTP show-page <project-dir> <page-number>
   BTP record <project-dir> --kind visual-review --input <project-dir>/qa/visual-reviews/page-####.json
   ```

   Finalize reading order, OCR, table, figure, formula, and source-extraction findings. Findings that alter translation meaning must set `affects_translation: true` and name affected units. For an existing prepared project, run `BTP visualize <project-dir>` to create or refresh this evidence.

   If a source exhibit is vector-only or its extraction contains only scattered labels, run `BTP reconstruct-figures <project-dir>` after target captions exist. The command reuses or creates the deterministic crop, places it before the mapped target caption, removes only the safe OCR-label tail, and skips existing visuals, native tables, and explicit structured reconstructions. Review the `target_repairs` section, record changed target units and the reconstruction observation, then run QA.

5. Read `terminology/bootstrap-samples.json`, inspect those source work units, author `terminology/glossary.json`, and record it:

   ```text
   BTP record <project-dir> --kind glossary --input <glossary.json>
   ```

   If the resolved configuration contains the terminology checkpoint, present `glossary-review.md` to the user. After approval:

   ```text
   BTP record <project-dir> --kind review --checkpoint terminology --approve --reviewer <name>
   ```

6. Author a source-grounded Context Capsule for every unit. Inspect a package with `show-unit --phase capsule`, write the capsule to its expected path, then record it. Capsules must be complete before parallel translation begins.

7. Run `BTP plan <project-dir>`. Resolve every `blocked` item. Use `translation_unit_ids`, `record_only_unit_ids`, and `edit_unit_ids` exactly; do not rediscover the queue from directory listings.

8. For each translation unit, inspect `show-unit --phase translate`, dispatch one isolated worker, then record its draft and observation. The work package includes mapped source-page images and crops. When available, attach `--duration-seconds`, `--input-tokens`, `--output-tokens`, `--cost`, and `--currency`; these are audit metrics, never semantic dependencies. After each batch, review observations and transactionally update the Glossary through `record --kind glossary`; rerun `plan` because the update may invalidate earlier units.

9. For each edit unit, inspect `show-unit --phase edit`, author a separate edited file, and record it with `record --kind edit`.

10. Run `BTP qa <project-dir>`. This runs Visual QA, Completeness QA, and the source-PDF exhibit inventory together. Fix every blocking issue, including vector-only exhibits that have a target caption but no visual asset or auditable reconstruction record. In Publication mode, warnings must be resolved or entered in an approved `qa/exceptions.json` record.

11. Create and render a candidate with `BTP typeset <project-dir>`. Run `BTP publication-review <project-dir>`, complete all seven checks in the generated template, and record the review:

   ```text
   BTP record <project-dir> --kind publication-review --input <completed-review.json>
   BTP record <project-dir> --kind review --checkpoint publication_signoff --approve --reviewer <name>
   ```

   If a check uses an exception, record the approved exception list first with `BTP record <project-dir> --kind exceptions --input <exceptions.json>`.

12. Rerun `BTP qa <project-dir>`, then `BTP build <project-dir>` and `BTP status <project-dir>`. Publication `build` copies the reviewed candidate files byte-for-byte into `dist/` and verifies their hashes.

For layout iteration before QA is green, run `BTP typeset <project-dir>`. This creates a clearly marked review candidate under `dist/typeset-preview/` and never changes the build stage or bypasses the final build gate. Any later candidate, clean-master, review, exception, or generated-file change invalidates the recorded signoff.

`prepare --rebuild` is required after a prepared source/configuration change. It clears only derived directories inside the translation project and preserves the copied source, user configuration, Style Guide, and Glossary.

## Evidence priority

When representations disagree, use this order:

1. Source file bytes and rendered source page
2. Page Map evidence linked to the source page
3. Extracted source segment
4. Book Map interpretation
5. Context Capsule
6. Translation or edited draft

A lower-priority artifact must not silently override a higher-priority one. Record the correction and invalidate dependent outputs.

## Work-unit contract

Each semantic work unit receives:

- Unit ID, Book Map node, source segment IDs, and source-page references
- Source text and any required visual crops
- Relevant approved Glossary entries and Style Guide rules
- Incoming Context Capsule
- Output path and observation path
- Translation invariants and user instructions

It produces:

- Complete target draft for that unit only
- Outgoing Context Capsule
- Evidence-backed terminology/structure/visual observations
- Used glossary/style IDs
- Unresolved issues rather than invented repairs

## Batch commit order

1. Validate all expected files and nonblank content.
2. Verify output paths stay within the project workspace.
3. Record unit state using the exact Glossary, Style Guide, Book Map, source, and Capsule hashes used.
4. Merge worker observations through the coordinator.
5. Recompute the next plan; do not assume the remaining queue is unchanged after shared-state updates.

## Checkpoints

- `preflight`: before extraction or rendering when dependencies are missing or the source is high risk.
- `terminology`: before bulk translation when configured; default on for Study and Publication.
- `visual-exceptions`: when reading order or OCR confidence is below the mode threshold.
- `publication-signoff`: required before Publication is marked complete.

## Resume

On every restart, load source fingerprint, resolved configuration, manifest, and Run State. Revalidate output hashes before trusting `completed`. Plan only `pending`, `failed-retryable`, or dependency-invalidated units. Never delete a valid completed artifact merely because the prior process ended unexpectedly.

## Stop conditions

Stop and report instead of guessing when:

- Source file identity changed unexpectedly.
- A required dependency is absent and no mode-valid degradation exists.
- Page reading order or OCR uncertainty can materially change meaning.
- A glossary conflict changes a technical definition and evidence is insufficient.
- A configured human checkpoint is pending.
- Completeness has a blocking failure.

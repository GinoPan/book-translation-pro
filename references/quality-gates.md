# Quality gates

Read this before marking a stage or project complete.

## Translation gate

- Every required work unit has a nonblank target draft.
- Output is associated with the source, Book Map, Glossary, Style Guide, and incoming Capsule hashes actually used.
- Markdown/XHTML structure required by the unit is valid.
- Worker observations are present, empty when appropriate, and schema-valid.
- Numbers, formulas, URLs, code, identifiers, note markers, citations, and image paths are preserved unless an approved transformation says otherwise.

## Edit gate

- Edited output exists separately from initial draft or has an auditable patch.
- No unresolved change to facts, definitions, negation, quantity, units, conditions, uncertainty, quotations, or reference anchors.
- Terminology and voice follow approved rules.
- Cross-unit joins do not duplicate or omit content.

## Completeness gate

- For PDF inputs, `reconstruct-figures` output is retained as evidence for deterministic source crops; a crop alone does not satisfy the gate until the target Markdown references it (or the target has an explicit structured reconstruction record).

The following are blocking in every mode:

- Missing or duplicate required work units
- Book Map nodes with no disposition
- Missing chapters, appendices, notes, bibliography, index, figures, tables, formulas, or captions that are in scope
- PDF source exhibits whose target caption has neither a meaningful visual asset nor an explicit reconstruction record in the figure audit. An asset counts only when its file exists and it sits within a few lines of that exhibit's own caption, with no other caption between them; a missing file is named in the failure. Captions are read from whole PDF lines (`Figure 3.2 Title`, `Fig. 4:`, `Chart 5`, `Figure 6-2`, `Plate II`); a line that continues in lower case (`Figure 7 shows ...`) is a citation, not a caption, and a list-of-figures entry with a dot leader is ignored; when a caption also appears on a text-only page, figures are cropped from the page that carries the visual. Figures and tables with the same number are separate exhibits; a target `图表`/`Exhibit` label may stand for either. Target labels are recognized in Chinese and English; for other target languages a missing caption is a warning. A PDF with images or vector drawings but no recognized caption is a warning, never a silent pass
- Source/output hash mismatch or orphan output
- Blank output or unparsed source region
- Broken local resource or note/reference link; URLs are compared in visible text and in raw `<a href>` targets
- Unapproved skipped content
- A unit whose prose block count, or whose number of list items, drops below the source (`content.block_coverage`) unless the observation records `structure_reflowed: <reason>` (with a non-empty reason) or a figure reconstruction. Block counts cannot see a paragraph omitted while another was split; length-based alignment was evaluated and rejected because it cannot separate that case from ordinary length variation without many false alarms
- A unit whose content length is under half of the source (`content.length_ratio`; Latin words and CJK characters are weighted 1 to 0.6, and pipe-table cells count)
- A unit whose target has fewer pipe-table rows than the source (`structure.tables`) unless a reflow or reconstruction is recorded
- A unit where at least half the target is copied verbatim from the source (`content.untranslated`) unless the observation records `untranslated_by_design: <reason>`. Copies are found per sentence and per table cell of six or more words, so a single sentence left in the source language is a warning
- A unit whose recorded dependencies (source, translation-relevant configuration, Book Map node, Style Guide, incoming capsule, glossary selection, page evidence) no longer match, or whose status is not `completed` (`unit.stale`, `unit.status`)
- A changed number or year; numeric differences are downgraded only when the observation records a reflow or reconstruction (other `source_issues` notes do not count), when the values moved across a unit boundary (the neighbour shows the mirror-image imbalance, not merely the same number), or when a small count is spelled out on the other side (`five` / `五` for `5`)

Deterministic checks should also compare heading counts, image/table/formula references, numbers and units, footnote/endnote anchors and placement, untranslated residue, and glossary variants. Footnote markers must remain superscript references attached to the intended term; they must not become quantity words such as “11个 5S”. Differences may be warnings only when a reviewed explanation exists.

## Visual gate

- Page Map covers the pages required by the selected mode.
- Reading order above the mode threshold is verified; low confidence is resolved or explicitly blocked.
- Risk-level review scope matches the mode.
- Crops are from the correct page and region, nonblank, not cut off, and not whole-page substitutes unless a fixed-layout exception is approved.
- Broken OCR remnants are not duplicated beside a retained table/figure crop.
- Captions and alt text are handled according to the Style Guide.

## Build gate

- Reader-facing Markdown and generated DOCX, EPUB, PDF, and bilingual outputs contain the configured copyright and authorization notice; EPUB metadata carries the compact rights declaration.
- Publication is blocked unless `rights.status` is `public-domain`, `licensed`, or `authorized`, `rights.redistribution_allowed` is true, and licensed/authorized projects record a non-empty `rights.basis`.
- Merge order equals Book Map order.
- Requested formats were actually generated and can be parsed/opened.
- EPUB container, manifest, spine, navigation, language, resources, and note links are valid when EPUB is requested.
- Publication EPUB passes external EPUBCheck with no errors; when EPUBCheck is unavailable, Publication remains blocked and the internal topology report is retained as diagnostic evidence.
- Final filenames, sizes, hashes, tool versions, and known limitations are recorded.

## Human review gate

Publication requires explicit review records for opening matter/TOC, chapter openings, all complex visuals, note-dense content, back matter, navigation, and narrow/mobile rendering. The final signoff is tied to the current clean Markdown master hash; any later edit invalidates it. Study reports what was sampled. Fast must never imply human review occurred when it did not.

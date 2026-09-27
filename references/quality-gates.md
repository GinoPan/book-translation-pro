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
- PDF source exhibits whose target caption has neither a meaningful visual asset nor an explicit reconstruction record in the figure audit
- Source/output hash mismatch or orphan output
- Blank output or unparsed source region
- Broken local resource or note/reference link
- Unapproved skipped content

Deterministic checks should also compare heading counts, image/table/formula references, numbers and units, footnote/endnote anchors and placement, untranslated residue, and glossary variants. Footnote markers must remain superscript references attached to the intended term; they must not become quantity words such as “11个 5S”. Differences may be warnings only when a reviewed explanation exists.

## Visual gate

- Page Map covers the pages required by the selected mode.
- Reading order above the mode threshold is verified; low confidence is resolved or explicitly blocked.
- Risk-level review scope matches the mode.
- Crops are from the correct page and region, nonblank, not cut off, and not whole-page substitutes unless a fixed-layout exception is approved.
- Broken OCR remnants are not duplicated beside a retained table/figure crop.
- Captions and alt text are handled according to the Style Guide.

## Build gate

- Merge order equals Book Map order.
- Requested formats were actually generated and can be parsed/opened.
- EPUB container, manifest, spine, navigation, language, resources, and note links are valid when EPUB is requested.
- Publication EPUB passes external EPUBCheck with no errors; when EPUBCheck is unavailable, Publication remains blocked and the internal topology report is retained as diagnostic evidence.
- Final filenames, sizes, hashes, tool versions, and known limitations are recorded.

## Human review gate

Publication requires explicit review records for opening matter/TOC, chapter openings, all complex visuals, note-dense content, back matter, navigation, and narrow/mobile rendering. The final signoff is tied to the current clean Markdown master hash; any later edit invalidates it. Study reports what was sampled. Fast must never imply human review occurred when it did not.
- For PDF inputs, `reconstruct-figures` output is retained as evidence for deterministic source crops; a crop alone does not satisfy the gate until the target Markdown references it (or the target has an explicit structured reconstruction record).

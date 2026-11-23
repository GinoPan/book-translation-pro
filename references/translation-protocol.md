# Translation, capsule, observation, and edit protocol

Read this reference before performing semantic work.

## Context Capsule

Use `show-unit --phase capsule` and read the source unit. Write the expected capsule JSON with:

- A short source-grounded summary of the unit
- Continuity facts needed by later units
- Glossary entity states when they change in this unit
- Cross-references and figure/table continuations
- Unresolved questions with source references

Every `unresolved` item is an object, never a bare string:

```json
{"question": "Does this note continue on the next page?", "source_refs": ["seg-000123"], "confidence": "low"}
```

The capsule is context, not authority. Do not add facts absent from the source, and do not write translated prose into it.

## Translation

Use `show-unit --phase translate`. Read the source unit, Style Guide, approved Glossary entries, incoming Capsule, `chapter_context` (summaries of earlier units in the same chapter), and user instructions. Translate from this package and record the draft before its inputs change; a draft recorded after its inputs changed is rejected.

- Translate every source element in order.
- Preserve heading levels, lists, tables, code blocks, raw markup, links, image paths, note anchors (including Markdown footnote references `[^fn3]` and their definition lines `[^fn3]: ...`; translate the note text, keep the label), citations, formulas, numbers, units, model identifiers, and URLs.
- Translate visible prose, headings, captions, safe alt text, and table language.
- Follow approved Glossary targets within their scopes. Report conflicts instead of silently changing an approved term.
- Use natural target-language syntax while preserving meaning, uncertainty, negation, qualifications, warnings, and authorial tone.
- Output only the translated unit to the expected draft file.

Write an observation JSON beside it:

```json
{
  "schema_version": 1,
  "new_entries": [],
  "conflicts": [],
  "source_issues": [],
  "used_glossary_ids": []
}
```

Every proposed term or conflict must include source evidence and a source segment or unit reference. Empty arrays are valid.

QA blocks omitted paragraphs, truncated units, and text left in the source language. When a difference is intentional, say so in `source_issues` with one of these prefixes, followed by a reason (a bare prefix is ignored; other notes in `source_issues` never excuse a changed number):

- `structure_reflowed: <reason>`: prose blocks were legitimately merged or removed (for example, OCR label fragments)
- `untranslated_by_design: <reason>`: material intentionally kept in the source language (for example, a bibliography)
- `source_error_corrected: <printed> -> <corrected> (exhibit <key>): <reason>`: a misprinted value was corrected (see Exhibits)

## Exhibits

`show-unit` lists the unit's `exhibits`: figure-label blocks (with the source image to look at) and rebuilt tables (with any total that does not add up). The translated figure is drawn from the label block, so its form matters as much as its words.

**Figure labels.** A diagram's image is followed by its labels:

```markdown
<!-- btp:figure-labels exhibit="4.3" -->
1. Customer Needs
2. Strategy Changes
<!-- /btp:figure-labels -->
```

- Keep both marker lines, every number, and the order; one translated label per numbered line, never merged, split, or dropped.
- Translate each label as the words printed in the figure, as short as the source; never describe the figure or add text that is not on it.
- Mark where a label may wrap with `|` between words or word groups (`客户|需求`, `价值流|战略`); never inside a word or term. A label with no `|` stays on one line or shrinks.
- Copy codes, numbers, and symbols that are not words (`FR1`, `PS2`, `M1`, `+`) unchanged.

**Rebuilt tables.** A table between `<!-- btp:exhibit-table exhibit="…" -->` and `<!-- /btp:exhibit-table -->` was rebuilt from the page. Keep the markers, every row and column, and empty cells; translate cell by cell; keep every number exactly as printed.

**Source misprints.** Only when `show-unit` reports a total that does not add up and the rows show which printed value is wrong: put the corrected value in the table, declare it in `source_issues` as `source_error_corrected: 3.4% -> 15.2% (exhibit 7.3): rows sum to 15.2%`, and add a translator's note directly below the table's closing marker giving the printed value, for example `> 译者注：原书此处印作 3.4%，按各行合计应为 15.2%，已改正。` Never change any other number; when the wrong value cannot be identified, leave the numbers and report the mismatch in `source_issues` instead.

## Edit Pass

Use `show-unit --phase edit`. Compare the draft with the source and constraints.

- Remove translationese, awkward source-language syntax, needless repetition, and inconsistent register.
- Preserve every factual and structural invariant from the translation rules.
- Do not add explanations or translator notes unless explicitly requested; a declared source-misprint note (see Exhibits) stays.
- Keep figure-label blocks and rebuilt tables in the form the Exhibits rules require.
- Write the complete edited unit to the expected edited path. Do not overwrite the draft.

## Worker boundary

One worker owns one unit and writes only that unit's expected draft/edit and observation files. Shared Glossary, Book Map, Style Guide, Manifest, and Run State are coordinator-only artifacts.

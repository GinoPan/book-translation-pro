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

QA blocks omitted paragraphs, truncated units, and text left in the source language. When a difference is intentional, say so in `source_issues` with one of these prefixes, followed by a reason:

- `structure_reflowed: <reason>`: prose blocks were legitimately merged or removed (for example, OCR label fragments)
- `untranslated_by_design: <reason>`: material intentionally kept in the source language (for example, a bibliography)

## Edit Pass

Use `show-unit --phase edit`. Compare the draft with the source and constraints.

- Remove translationese, awkward source-language syntax, needless repetition, and inconsistent register.
- Preserve every factual and structural invariant from the translation rules.
- Do not add explanations or translator notes unless explicitly requested.
- Write the complete edited unit to the expected edited path. Do not overwrite the draft.

## Worker boundary

One worker owns one unit and writes only that unit's expected draft/edit and observation files. Shared Glossary, Book Map, Style Guide, Manifest, and Run State are coordinator-only artifacts.

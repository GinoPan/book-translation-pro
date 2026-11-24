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
- Follow `ledger_entries`: the renderings earlier units chose for names and terms the glossary lacks. Use the same rendering; when you believe it is wrong, keep it and report a conflict instead of starting a second one.
- Render people, organizations, and places as the style guide's name policy (`names.policy`) says, the same way in every unit.
- Use natural target-language syntax while preserving meaning, uncertainty, negation, qualifications, warnings, and authorial tone.
- Translate every sentence of a long paragraph; a sentence dropped inside a paragraph is the omission no automatic check can see.
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

Record in `new_entries` every name and every domain term you render that neither the glossary nor `ledger_entries` covers, the first time you render it:

```json
{"source": "box score", "target": "绩效记分卡", "category": "technical_term", "source_ref": "unit-0123", "note": "weekly value stream report, not a statistics term"}
```

`source`, `target`, and `category` (one of the glossary categories: `person`, `place`, `organization`, `title`, `technical_term`, `product`, `standard`, `unit`, `other`) are required; `record --kind observation` rejects an entry without them. Later units receive it in `ledger_entries`, and QA reports a unit that departs from it.

QA blocks omitted paragraphs, truncated units, and text left in the source language. When a difference is intentional, say so in `source_issues` with one of these prefixes, followed by a reason (a bare prefix is ignored; other notes in `source_issues` never excuse a changed number):

- `structure_reflowed: <reason>`: prose blocks were legitimately merged or removed (for example, OCR label fragments). Use it only for that unit's actual reflow: it turns the paragraph, length, and number checks into warnings for the whole unit, so a sentence or paragraph lost there is left to the translation review
- `untranslated_by_design: <reason>`: material intentionally kept in the source language (for example, a bibliography)
- `source_error_corrected: <printed> -> <corrected> (exhibit <key>): <reason>`: a misprinted value was corrected (see Exhibits)
- `term_not_applicable: <glossary entry id> (<reason>)`: an approved term that the source mentions rightly does not appear in this unit's target (for example, its sentence was translated in the neighbouring unit, the source line is exhibit text kept as an image, or the content was omitted by decision). QA reports it as a warning with the reason instead of failing; name one entry per declaration

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
- Where the figure stacks separate items in one label (rows of a list, `Cost` / `Throughput Time` / …), start each item on its own line with `||` (`成本||吞吐时间||机器效率`). A bulleted list already gets one item per line. With exactly one item per source line (a table column inside a form, a legend), each item keeps its source row, so the rows stay in line with the rules and the neighbouring columns; a source line that only continues an item gets its own `||` item.
- A label set at an angle (an axis title reading upward, slanted bar names) is translated like any other. A vertical one is set upright in Chinese, read top to bottom; keep it short, as it has only the source's length.
- Copy codes, numbers, and symbols that are not words (`FR1`, `PS2`, `M1`, `+`, `R8 … Rn`) unchanged.
- QA takes a label of one to four capital letters for a code. When it is a word set in capitals (`MAN`, `LEAN`), translate it and declare it in `source_issues` as `label_is_word: <exhibit>#<label number> (<reason>)`, for example `label_is_word: 8.5#11 (cause category "man", not an abbreviation)`.

**Rebuilt tables.** A table between `<!-- btp:exhibit-table exhibit="…" -->` and `<!-- /btp:exhibit-table -->` was rebuilt from the page. Keep the markers, every row and column, and empty cells; translate cell by cell; keep every number exactly as printed.

**Source misprints.** Only when `show-unit` reports a total that does not add up and the rows show which printed value is wrong: put the corrected value in the table, declare it in `source_issues` as `source_error_corrected: 3.4% -> 15.2% (exhibit 7.3): rows sum to 15.2%`, and add a translator's note directly below the table's closing marker giving the printed value, for example `> 译者注：原书此处印作 3.4%，按各行合计应为 15.2%，已改正。` Never change any other number; when the wrong value cannot be identified, leave the numbers and report the mismatch in `source_issues` instead.

## Edit Pass

Use `show-unit --phase edit`. Compare the draft with the source and constraints.

- Remove translationese, awkward source-language syntax, needless repetition, and inconsistent register.
- Preserve every factual and structural invariant from the translation rules.
- Do not add explanations or translator notes unless explicitly requested; a declared source-misprint note (see Exhibits) stays.
- Keep figure-label blocks and rebuilt tables in the form the Exhibits rules require.
- Write the complete edited unit to the expected edited path. Do not overwrite the draft.

## Translation review

Read [translation-quality.md](translation-quality.md) first. The reviewer must not be the worker who translated or edited the units: use a fresh worker with no part in the translation.

1. Run `translation-review <project>`. It writes `qa/translation-review/review.json` with the selected units, each with `reasons` (why it was chosen) and verdict `pending`.
2. For each unit, run `show-unit <project> <unit> --phase review`. Read the source file beside `target_file`, with the style guide, the glossary entries, and the ledger entries. Read every sentence; compare meaning, not surface.
3. For each error, add a finding to the unit's `findings`:

   ```json
   {"category": "terminology.wrong_term", "severity": "major", "status": "open", "cause": "term-not-in-glossary",
    "source_excerpt": "the weekly box score", "target_excerpt": "每周的箱线分数表",
    "note": "box score is the value stream performance report, not a box plot", "fix": "每周的绩效记分卡"}
   ```

   - `category` and `severity` as defined in translation-quality.md. Mark a meaning change or a lost sentence `major`; reserve `critical` for errors that could cause harm.
   - `cause` is a short slug naming why the error happened, reused across books (`term-not-in-glossary`, `name-policy-not-followed`, `sentence-dropped-in-long-paragraph`, `false-friend`, `unit-join-duplicate`). Rule proposals are grouped by it.
   - `source_excerpt` is the source words involved; `target_excerpt` is the target words (empty for an omission).
4. Set the unit's verdict: `ok` when it has no open finding, otherwise `findings`. Set `reviewer` to your name.
5. Once per review, read `qa/translation-signals.json` and the glossary and ledger: for every name or term rendered more than one way, add a `terminology.inconsistent` finding to one unit where it departs.
6. Record with `record --kind translation-review --input <file>`.

To fix findings, edit the units (a new edit pass), record them, run `translation-review` again (judged units that did not change keep their verdict; changed ones return to `pending` with their earlier findings), and mark each earlier finding `fixed` or still `open`. A finding that turns out not to be an error is marked `dismissed` with the reason in `note`. When a stretch's sample scored above the bar, the next `translation-review` adds every unit of that stretch; review them too. Then the user reads `qa/translation-review/index.html` and records the `translation_review` checkpoint.

## Worker boundary

One worker owns one unit and writes only that unit's expected draft/edit and observation files. Shared Glossary, Book Map, Style Guide, Manifest, and Run State are coordinator-only artifacts.

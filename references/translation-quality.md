# Translation quality

Read this before running or recording a translation review, and before changing a translation check.

The aim is rules that make any book come out right, not fixes to one book. Quality is defined once, measured the same way on every book, and every defect a person finds can become a rule.

## The standard

Errors are classified with the categories of MQM (Multidimensional Quality Metrics), the common industry scheme:

| Category | What it covers |
|---|---|
| `accuracy.mistranslation` | The target says something the source does not (a wrong term sense, a wrong relation, a false friend) |
| `accuracy.omission` | Source content missing from the target: a sentence, a clause, a list item, a qualifier |
| `accuracy.addition` | Target content the source does not have, including a passage translated twice |
| `accuracy.untranslated` | Source text left in the source language without a reason |
| `terminology.inconsistent` | One source term or name rendered more than one way |
| `terminology.wrong_term` | A term rendered with a word that is not the field's term |
| `fluency.grammar` | Grammar or word-order errors in the target |
| `fluency.unnatural` | Correct but stilted target prose (translationese) |
| `style.register` | Tone or register that departs from the author or the style guide |
| `locale.conventions` | Punctuation, number, date, and name conventions of the target locale |
| `structure.markup` | Headings, lists, tables, notes, or links broken or changed |

Each finding has a severity, weighted as in MQM 2.0:

| Severity | Weight | Meaning |
|---|---:|---|
| minor | 1 | Noticeable, but the meaning and the reader's trust are intact |
| major | 5 | The meaning is changed or lost, or a reader would be misled or stopped |
| critical | 25 | The error could cause harm or make the text unusable (a wrong number in an instruction, an inverted warning) |

**Score:** weighted findings per 1,000 source words, prose and table cells (CJK characters count 0.6 of a word). Two scores are kept: *found* counts every finding that was not dismissed (how good the translation was when reviewed); *open* counts the findings not yet fixed (how good it is now).

**The bar** is `quality.max_penalty_per_1000_words`: 10 in Study, 5 in Publication. Fast has no review.

## How it is applied

1. **Before translation.** `prepare` writes `terminology/term-candidates.json`: recurring phrases and names across the whole book, so the glossary is not built from samples alone. The style guide states the name policy (rule `names.policy`).
2. **During translation.** The translator records every rendering of a name or domain term the glossary lacks in the observation's `new_entries`. They form the **ledger** (`terminology/ledger.json`), which `show-unit` gives every later unit (`ledger_entries`), so a book stays consistent as it is written. The ledger is context, not a dependency: a growing ledger never makes a finished unit stale.
3. **Rule checks in QA.** They run on every book and need no reader:

   | Check | Status | Finds |
   |---|---|---|
   | `content.repeated_text` | fail | A sentence the target repeats more often than the source repeats any, within a unit or across the join of two units. A source repeat counts from half the target's weight (a list item of seven English words becomes fourteen Chinese characters), and two source sentences that differ by a slip, with the same numbers, count as a repeat |
   | `style.typography` | warn | Language-profile typography: half-width punctuation between Chinese characters, doubled punctuation, a space between characters, zero-width characters, unpaired quotation marks and brackets |
   | `terminology.name_consistency` | warn | A name printed in English in some units and translated in others (one summary line; details in `qa/translation-signals.json`) |
   | `terminology.ledger` / `terminology.ledger_conflict` | warn | A unit that departs from a recorded rendering; a term recorded with two renderings |
   | `content.excused_units` | warn | A fifth or more of the units carry a reflow or reconstruction note, which turns their paragraph, length, and number checks into warnings |
   | `content.split_sentence` | warn | A translation that ends or starts with `……` the source does not have: a sentence split by a page break left as fragments |
   | `content.note_markers` | warn | Fewer note markers (`^8^`, `<sup>8</sup>`, superscripts, `[^8]`, a digit after full-width punctuation that no measure word follows) in the translation than in the source, counted with the neighbouring units so a marker that moved with its sentence is not reported |
   | `structure.heading_paragraph` | warn | A heading that is a paragraph: about 67 Chinese characters or more, ending in a full stop |
   | `terminology.hyphenated` | warn | An approved term the source prints hyphenated (`Kan-Ban`), which the glossary selection misses, absent from the translation. The selection itself is a dependency and is left unchanged for hyphens; like QA, it reads only the lines a translation renders (no running heads or scrambled sideways text) |

   Typography leaves out diagram labels (spaced to fit a drawing) and counts a bare link as a word. The name check also finds names the source prints in capitals (`DR. C. J. MCNAIR`) and leaves out placeholder units. It still counts a name kept in English in a citation, as the name policy requires, as an English mention; on Lean most of the 12 names it lists after the fixes are of that kind.

   Checks that depend on the target language come from a **language profile** (`quality.PROFILES`; Chinese first). For a target without a profile, QA says the check was not run.
4. **Independent review.** `translation-review` selects units: in Publication every unit; in Study a share (`quality.review_sample_share`, 10%) of each stretch of the book, spread evenly, plus every unit a rule signal points at (a short target, few target sentences, repeated text, typography, missing note markers, a sentence fragment at a join). A unit whose translation holds no reader text (an omitted index, an exhibit rebuilt in another unit) is never selected: there is nothing to read, and QA checks its note. A reviewer who did not translate the units reads each source beside its translation and records findings. Running `translation-review` again keeps the verdicts and findings already entered in the review file, recorded or not; a unit whose translation changed since goes back to pending with its findings kept. QA fails on an open major or critical finding, on open findings above the bar, and on a stretch whose sample scored above the bar until every unit of it is reviewed. The user reads `qa/translation-review/index.html` and approves the `translation_review` checkpoint.
5. **From defects to rules.** Every finding carries a `cause`: a short slug naming why the error happened (`term-not-in-glossary`, `name-policy-not-followed`, `sentence-dropped-in-long-paragraph`, `false-friend`, `unit-join-duplicate`). `quality-rules <project> [<project> ...]` groups findings across books by cause (one cause, such as `term-not-in-glossary`, shows up under several categories) and proposes a rule for a cause found three or more times: widen an existing check (and add the examples to its tests) or add a line to the translation protocol or style guide. A person decides; the tool changes nothing.

## Measuring the checks

`quality-benchmark <project>` plants known errors in copies of the translated units (a dropped paragraph, a truncated unit, a dropped sentence, a repeated sentence, a passage repeated across a join, a sentence left in the source language, a changed number, a half-width comma) and reports what share each check catches, separately for units without an excuse note, and how often each check reports the finished translation. It works on any translated book, so a rule is judged on more than the book it came from. Nothing in the project changes.

Measured on Lean Accounting (280 units, a finished and reviewed translation; no check fails it, and the new checks warn on 4 units for typography):

| Planted error | Recall | Without an excuse note | Note |
|---|---:|---:|---|
| Passage repeated across a join | 98% | 100% | |
| Sentence left in English | 100% | 100% | |
| Half-width comma | 98% | 100% | |
| Repeated sentence | 91% | 96% | |
| Truncated unit | 90% | 70% | |
| Changed number | 50% | 85% | Only a warning in units with a reflow note (157 of 280 on Lean) |
| Dropped paragraph | 25% | 65% | Paragraphs the translator split hide a dropped one from the block count |
| Dropped sentence | 10% | 36% | Only a reader finds it reliably |

On Lean's first translation (v1), with its known defects, the checks found the passage translated twice across the join of units 0091 and 0092, the omission in unit 0077 (7 of 24 paragraphs, 45% of the source length), and Toyota and Deming printed both in English and in Chinese. The wrong terms (箱线分数表 for "box score", Jidoka left in English, mass production rendered two ways) are invisible to every rule: they are what the ledger prevents and the review finds.

### The first real review (Lean Accounting, 2026-11-25)

A reviewer who had not translated the book read 56 of its 280 units (the Study sample and the signalled units) against the source. Found: 35 findings, 3.0 weighted per 1,000 source words (bar 10); one more finding was withdrawn as the reviewer's own error. Six were major:

- "Fortune 50" rendered 五百强 (the number check missed it: the target spells the number);
- "what he meant is lost on most managers" rendered as *forgotten*;
- a paragraph marked as a heading by extraction and kept;
- a literature reference dropped from a note;
- the unit-cost formula losing its divisor *N*, set on its own line by extraction;
- a first half of a sentence split by six pages of exhibits.

With the checks added afterwards (below), the review read 21 more units: the 16 they flagged for missing note markers and 5 sample picks moved off placeholder units. That found two more major omissions (a quotation ending a unit and two sentences across a page break, both in units the signal chose) and 25 more dropped note markers. In all: 77 units, 62 findings, 7 major, 3.1 weighted per 1,000 source words, all fixed.

The minor ones were mostly names rendered two ways (Deming as 戴明 and as Deming, chapter authors transliterated in some chapters), terms missing from the glossary (continuous improvement, performance measurement, activity-based costing, True North), idioms taken literally, dropped note markers, and heading markup that differed by chapter.

What changed because of it: the four checks above (each measured on Lean: they find the 5 split sentences, the 11 paragraphs marked as headings, and the 1 hyphenated term the review found, and nothing in the fixed translation except one inflected term, `target cost-ing model`; the note-marker check flags 19 units of the original translation, every one confirmed by the review, and none after the fixes), typography no longer reporting labels and removed links, the review skipping placeholder units (20 of the 56 first picks; a sample pick that falls on one moves to the nearest readable unit, so the other picks keep their places), recording a review also updating the review file (a rebuild had taken older verdicts from it), the reviewer seeing the edges of the neighbouring units, rule proposals grouped by cause, and the protocol lines on idioms, names in bylines, split sentences, heading markup, note markers, formulas, and added text.

Rejected after measurement (do not reintroduce without new evidence):

- **Pairing paragraphs by position** to find a shortened one: translators split and join paragraphs, so equal counts do not mean aligned paragraphs.
- **Sentence-count or length ratios as failures:** on a finished book they swing from 0 to over 1 with tables, lists, and indexes. They rank units for the review instead.
- **Abbreviations as omission anchors:** headings in capitals look like abbreviations, and MBA or CPA are rightly translated.
- **Treating target-only numbers as unexcusable under a reflow note:** 46 finished Lean units have them (renumbered notes, rebuilt tables).
- **Learning a name's Chinese form from glosses** such as `首席执行官（CEO）`: it mostly finds abbreviations, which are rightly used bare after their first gloss. A name's rendering comes from the glossary or the ledger.
- **Near-identical source sentences at a ratio of 0.9, numbers ignored:** table rows that differ only in their numbers ("Cell 1 produced 250 units", "Cell 2 produced 260 units") counted as one repeated sentence and excused planted repeats (Lean recall 90% to 87%). The numbers must now match and the ratio be 0.97.

### The second book (Practical Lean Accounting, 2026-11-25)

Running the tool on a second book (620 units, from CHM) found two failures that blocked it, both fixed and measured on both books:

- `content.repeated_text` failed 3 faithful units: a list item the source repeats is under the repeat weight in English and over it in Chinese, and one source repeat differs by a typo ("waits" / "awaits"). With the source counted from half the weight and same-numbered near-duplicates: the 3 units pass, and planted-error recall stays at the baseline (Lean: repeated sentence 165 of 185 caught, was 166; across a join 198 of 203, was 199. Practical: 79% and 98%, as before).
- The bilingual EPUB failed its table count: removing a CHM spacer image left 196 empty divider tables, which Pandoc drops. The bilingual side now drops a table its cleaning emptied (278 tables to 82 on Practical, no line with text removed).

The note-marker check now counts `<sup>8</sup>` (Practical's 6 markers were invisible to it) and no longer takes a quantity after punctuation (`其中，10家企业`) for a marker; on Lean it still flags the same 19 units of the original translation and none of the fixed one.

## Changing a check

A check that fails a unit must be measured on at least one finished book with `quality-benchmark` (alarms on the finished translation) and have negative tests for look-alike cases it must leave alone. A warning is enough for a signal that is strong but not proof.

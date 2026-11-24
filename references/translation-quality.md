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
   | `content.repeated_text` | fail | A sentence the target repeats more often than the source repeats any, within a unit or across the join of two units |
   | `style.typography` | warn | Language-profile typography: half-width punctuation between Chinese characters, doubled punctuation, a space between characters, unpaired quotation marks and brackets |
   | `terminology.name_consistency` | warn | A name printed in English in some units and translated in others (one summary line; details in `qa/translation-signals.json`) |
   | `terminology.ledger` / `terminology.ledger_conflict` | warn | A unit that departs from a recorded rendering; a term recorded with two renderings |
   | `content.excused_units` | warn | A fifth or more of the units carry a reflow or reconstruction note, which turns their paragraph, length, and number checks into warnings |

   Checks that depend on the target language come from a **language profile** (`quality.PROFILES`; Chinese first). For a target without a profile, QA says the check was not run.
4. **Independent review.** `translation-review` selects units: in Publication every unit; in Study a share (`quality.review_sample_share`, 10%) of each stretch of the book, spread evenly, plus every unit a rule signal points at (a short target, few target sentences, repeated text, typography). A reviewer who did not translate the units reads each source beside its translation and records findings. Running `translation-review` again keeps the verdicts and findings already entered in the review file, recorded or not; a unit whose translation changed since goes back to pending with its findings kept. QA fails on an open major or critical finding, on open findings above the bar, and on a stretch whose sample scored above the bar until every unit of it is reviewed. The user reads `qa/translation-review/index.html` and approves the `translation_review` checkpoint.
5. **From defects to rules.** Every finding carries a `cause`: a short slug naming why the error happened (`term-not-in-glossary`, `name-policy-not-followed`, `sentence-dropped-in-long-paragraph`, `false-friend`, `unit-join-duplicate`). `quality-rules <project> [<project> ...]` groups findings across books by category and cause and proposes a rule for a cause found three or more times: widen an existing check (and add the examples to its tests) or add a line to the translation protocol or style guide. A person decides; the tool changes nothing.

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

Rejected after measurement (do not reintroduce without new evidence):

- **Pairing paragraphs by position** to find a shortened one: translators split and join paragraphs, so equal counts do not mean aligned paragraphs.
- **Sentence-count or length ratios as failures:** on a finished book they swing from 0 to over 1 with tables, lists, and indexes. They rank units for the review instead.
- **Abbreviations as omission anchors:** headings in capitals look like abbreviations, and MBA or CPA are rightly translated.
- **Treating target-only numbers as unexcusable under a reflow note:** 46 finished Lean units have them (renumbered notes, rebuilt tables).
- **Learning a name's Chinese form from glosses** such as `首席执行官（CEO）`: it mostly finds abbreviations, which are rightly used bare after their first gloss. A name's rendering comes from the glossary or the ledger.

## Changing a check

A check that fails a unit must be measured on at least one finished book with `quality-benchmark` (alarms on the finished translation) and have negative tests for look-alike cases it must leave alone. A warning is enough for a signal that is strong but not proof.

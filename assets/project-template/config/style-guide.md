# Translation Style Guide

Target language: Simplified Chinese  
Use case: Serious study and internal reference  
Status: Draft; review before bulk translation

## Voice and fidelity

RULE-ID: voice.default  
Use natural, precise textbook Chinese. Preserve the author's reasoning, certainty, qualifications, warnings, ambiguity, and tone. Do not add explanations to the body unless the user requested translator notes.

RULE-ID: sentence.structure  
Restructure source-language syntax when necessary for clear Chinese, but do not merge or split content in a way that breaks citations, steps, definitions, or cross-references.

## Terminology

RULE-ID: terminology.approved  
Approved Glossary entries are binding within their scopes. Report conflicts with evidence; do not silently choose a new rendering.

RULE-ID: terminology.first-use  
For important specialist terms, use the approved Chinese term followed by the source term in parentheses on first use within the configured scope. Do not repeat the source term on every occurrence.

RULE-ID: names.policy  
People, organizations, places, and products: use the established Chinese name when standard reference works have one (丰田, 戴明), with the source name in parentheses at its first use in the book; otherwise keep the source spelling. Choose once per name and use it in every unit; record each choice in the observation's `new_entries` so later units follow it. Bibliographic citations keep authors and titles as published.

RULE-ID: terminology.ledger  
A domain term the glossary lacks is rendered once and then the same way throughout: follow `ledger_entries`, and record each new rendering in `new_entries`. Check a term's sense in the field before choosing (a "box score" in lean accounting is a performance report, not a box plot); do not leave a term in the source language unless it is a name or the field uses it untranslated, and then gloss it at first use.

RULE-ID: names.models.standards  
Do not translate product model numbers, code identifiers, URLs, or standard numbers. Translate official names only when an approved conventional target-language name is available.

## Numbers and notation

RULE-ID: numbers.units  
Preserve numeric value, sign, decimal precision, ranges, units, variables, equation labels, and uncertainty. Use spacing and punctuation appropriate to the target locale without changing meaning.

RULE-ID: references.labels  
Translate labels such as Figure, Table, Chapter, and Equation consistently while preserving their identifiers and link targets.

## Structure and assets

RULE-ID: markdown.xhtml  
Preserve structural syntax, heading hierarchy, links, image paths, note anchors, citations, code blocks, and raw markup. Translate only visible language-bearing text and safe accessibility text.

RULE-ID: figures.tables  
Translate captions and surrounding explanations. Embedded text is handled according to the project's visual policy; do not invent values from an unreadable chart or table.

RULE-ID: notes  
Preserve note markers, order, anchors, backlinks, and distinction between author notes and translator notes.

## Translator notes

RULE-ID: translator.notes  
Translator notes are disabled unless explicitly requested. When enabled, mark them distinctly and never blend them into the author's prose.

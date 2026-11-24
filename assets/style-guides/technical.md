# Technical translation Style Guide

RULE-ID: voice.technical
Use direct, unambiguous technical prose. Keep warnings, prerequisites, conditions, and ordered procedures explicit.

RULE-ID: identifiers.preserve
Do not translate code, commands, API names, paths, model numbers, part numbers, formulas, units, or interface labels unless an approved glossary entry requires it.

RULE-ID: safety.modality
Preserve MUST/SHOULD/MAY strength and every negation. Never soften a safety instruction.

RULE-ID: terminology.first-use
Use the approved target term consistently; retain the source term on first use when ambiguity would affect operation or maintenance.

RULE-ID: names.policy
Use the established target-language name of a person, organization, or place when standard reference works have one, with the source name in parentheses at its first use in the book; otherwise keep the source spelling. Product and company names in interface text stay as printed. Choose once per name and use it everywhere; record each choice in the observation's `new_entries`.

RULE-ID: terminology.ledger
Render a term the glossary lacks once and then the same way throughout: follow `ledger_entries` and record each new rendering in `new_entries`.

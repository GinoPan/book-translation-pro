# Copyright and rights protection

Read this before initializing a project, sending source content to another service, or generating reader-facing files.

## Purpose and boundary

Book Translation Pro records the user's rights declaration and enforces conservative publication gates. It does not determine whether a work is protected, validate a license, replace legal advice, or guarantee that a proposed use is lawful in a particular jurisdiction.

Translation is commonly treated as a derivative use of the source work. Owning a lawful copy does not by itself establish the right to translate, reproduce, publish, or commercially distribute either the source or a translation. Personal study, internal research, public distribution, and commercial use may require different permissions.

## Project declaration

Every resolved project has a `rights` object:

```yaml
rights:
  status: unknown
  basis: ""
  intended_use: personal-study
  redistribution_allowed: false
  source_upload_allowed: false
  attribution: ""
  notes: ""
  include_notice_in_outputs: true
```

`status` is one of:

- `unknown` — no sufficient basis has been recorded.
- `public-domain` — the user declares that the source is in the public domain for the intended jurisdiction and use.
- `licensed` — a license covers the intended translation and use.
- `authorized` — the relevant rights holder has granted authorization.
- `personal-research` — the user declares a limited personal research purpose; this is not a publication right.

`basis` records a concise citation, license name, authorization date, or other non-sensitive basis. Do not store secret contract terms, private contact details, access tokens, or full license documents in project configuration. `attribution` records the credit required by the source license or authorization.

`redistribution_allowed` is an explicit user declaration about the translation output. `source_upload_allowed` is separate: permission to redistribute a translation does not imply permission to upload or distribute the source book. `include_notice_in_outputs` defaults to true and should be disabled only when an equivalent, reviewed rights page is already present.

## Runtime behavior

- `btp init` defaults to `status: unknown`, no redistribution, no source upload, and an output notice.
- Fast and Study projects may continue locally when rights are unknown, but `init`, `status`, and QA expose a warning.
- Publication QA blocks unless `status` is `public-domain`, `licensed`, or `authorized` and `redistribution_allowed` is true.
- `licensed` and `authorized` Publication projects also require a non-empty `basis`.
- The final Markdown master and generated DOCX, EPUB, PDF, and bilingual editions include a generated copyright and authorization page by default.
- EPUB metadata includes a compact rights declaration.
- A generated notice reports the user's declaration and explicitly states that it is not legal advice or rights certification.

These checks are intentionally not bypassed by a generic QA exception. If the declaration changes, edit `book-project.yaml`, rerun `prepare --rebuild` as required by the configuration hash, create a new typeset candidate, and repeat the current publication review and signoff.

## Data handling

- Keep the source file inside the user-selected local project by default.
- Do not upload source bytes, page images, OCR output, long excerpts, or a complete translation to an external service unless the user has authorized that service and `source_upload_allowed` is true.
- Give workers only the minimum work unit and visual evidence needed for their assignment.
- Store hashes, paths, IDs, and concise observations in logs instead of copying long source passages.
- Exclude real source books, generated translation projects, test artifacts, credentials, and local caches from public source-control repositories.
- Do not publish or bundle the copied source file with the translation unless the recorded rights expressly allow it.

## Rights page

The generated page identifies the declared status, intended use, redistribution permission, original work, author, translator, attribution requirement, authorization basis, and any project note that the user intentionally supplied. Its wording varies for public-domain, licensed/authorized, and unconfirmed projects.

The original work's copyright remains with its rights holder unless it is in the public domain. A public-domain source does not automatically place new translation, editing, layout, or annotations in the public domain; the publisher of the translation must choose and state the applicable terms separately.

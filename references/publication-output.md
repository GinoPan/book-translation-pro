# Publication output

Read this before generating DOCX, PDF, EPUB, or the reader-facing Markdown edition.

## Output relationship

The verified edited units remain the semantic source of truth. Build a clean reader-facing Markdown master from them, then generate:

```text
edited units -> clean Markdown -> publication DOCX -> fixed-layout PDF
                              -> semantic XHTML -> reflowable EPUB
                              -> alignment -> optional bilingual formats
```

Do not use a PDF produced from unrelated HTML/CSS when DOCX and PDF are expected to share pagination. EPUB is reflowable and must use navigation links rather than printed page numbers.

The clean Markdown master includes the generated copyright and authorization page unless `rights.include_notice_in_outputs` is explicitly false because an equivalent reviewed notice already exists. The page reports the project's rights declaration without claiming legal review. Carry the same statement into DOCX/PDF, EPUB, and bilingual editions, and write a compact rights value into EPUB metadata. Publication remains blocked by the rights gate even when notice generation is disabled.

## Cleanup

Remove print-production residue such as QuarkXPress/InDesign job stamps, copied running heads, folios, empty extraction wrappers, source-page TOC numbers, extraction-only OCR recovery tails, HTML review comments, and leaked patch/observation records from reader-facing outputs. Restore numeric footnote markers that were encoded as extracted superscript spans, but preserve ordinary quantities and list numbering. When extraction places a marker before a classifier and protected term (for example, `11个 5S` from source `5S¹¹`), move the marker after the term before rendering it as a superscript; never let a footnote change the quantity or meaning. Join only clear cross-page sentence fragments whose preceding paragraph ends in a dangling conjunction; preserve ordinary paragraph boundaries. If source-page visual review finds a figure that extraction reduced to scattered OCR labels or omitted entirely, create a deterministic crop from the authoritative rendered source page, insert it at the figure position, and remove duplicated OCR label fragments. The PDF exhibit inventory must confirm that every source exhibit has a target asset or an explicit structured-reconstruction record before publication files are built. Preserve the edited units unchanged so the transformation remains auditable. Limit cleanup to recognized structural patterns; do not delete ordinary numbers, citations, dates, or measurements.

## Word structure

- Use native Title and Heading styles rather than visual-only bold text.
- Replace a copied static source TOC with a Word TOC field based on Heading 1 through the configured depth.
- Set the document to refresh fields when opened. On Windows, prefer Word automation to update fields and export PDF; otherwise use LibreOffice when available, exporting with `IsSkipEmptyPages=false` so the blank pages inserted before odd-page chapter openers survive in the PDF. LibreOffice does not fill the Word TOC field, so a PDF that still shows the TOC placeholder fails validation, and a DOCX whose TOC was not refreshed carries a warning.
- Use a real trim size, front/body section separation, Roman front-matter numbering on recto and verso pages, Arabic body numbering, and distinct odd/even headers. When level 1 holds only the book title (a PDF title page set larger than the chapter openers), every later heading is lifted one level so chapters become level 1; a lone chapter whose level-2 headings are its numbered sections (`6.1 …`) and notes is not lifted. The generated rights page is front matter and follows the whole title page, including author or subtitle lines; when body text follows the level-1 heading directly (a single chapter), the rights page goes before that heading instead, and a numbered chapter heading that is the publication title with no other chapter or part after it opens the body rather than becoming a title page. The body opens at the first Heading 1 that is a chapter or part opener (`第1章`, `第一章`, `1 标题`, `第一部分`, `Chapter One`, `Part II`) or, when none is numbered, at the first Heading 1 that is not a front-matter or contents title; a document without body headings is numbered in Arabic throughout. A Heading 1 directly under a Heading 1 of the same rank (the source and target titles in a bilingual edition) shares its opener; a part title and its first chapter still open separate sections. Margins follow the configured profile: Publication mode mirrors margins with a gutter for print binding; fast/study modes keep symmetric margins so consecutive pages do not visibly shift on screen (`publishing.mirror_margins` overrides the default).
- Write section-specific chapter or appendix titles into odd-page running heads and the book title into even-page running heads. Avoid `STYLEREF` where a section can begin without a discoverable Heading 1, because Word renders a visible field error.
- Match the source edition's folio convention. `publishing.folio_position` defaults to `auto`: sample the source PDF for standalone page numbers on body pages, and when they share the top line, compose the running head as folio at the outer edge plus centered title (verso: folio then book title; recto: chapter title then folio) instead of moving the page number to a footer.
- Begin parts and chapters on a new page; use odd-page section starts when the renderer supports them reliably.
- Put the title leaf, copyright page, and TOC on separate pages. Suppress running heads on section-opening pages; use Roman front-matter numbering and Arabic body numbering.
- Normalize table-cell indentation and alignment. Treat the first row as a header (bold, centered, repeated across pages) only when Pandoc marked it as one; a headerless pipe table's first row is data. Convert retained HTML tables before applying width rules, then split Markdown pipe tables wider than five columns into readable continuation groups only when their columns do not fit the text width; continuation groups are labelled in the target language (`（续表）` for Chinese, `(continued)` otherwise). Repeat both category and row-label columns when the source uses a two-column key, so the same content remains usable in portrait DOCX/PDF and reflowable EPUB. Accept Pandoc's short two-dash alignment separators when detecting tables. Keep tables off page breaks: every row carries `w:cantSplit`, all rows but the last keep with the next, and the header row repeats when a genuinely oversized table must flow across pages. Keep each figure on the same page as its caption (图表/图/表 or Figure/Table/Exhibit labels) via keep-with-next and keep-lines on the figure paragraph.
- For CHM/HTML Help sources, preserve the high-resolution file linked by a thumbnail anchor and keep the `assets/original/` publication prefix when upgrading legacy nested Markdown-image links. Never resample source pixels merely to fit a page; constrain physical display size in the document instead.

### CHM/HTML Help normalization

- Convert complete raw HTML tables with a nesting-aware outer-table scanner before applying Markdown width and split rules. Replace `b24-toctitle` or `span#TOC` layout tables with `[[TOC]]`, discard content-free spacer tables, and emit long single-cell prose/footnote rows as ordinary paragraphs.
- Remove only known platform chrome such as `teamlib.gif`, `next.gif`, `previous.gif`, and verified spacer filenames. Preserve real images inside table cells as Markdown image references.
- Prefer the locally linked full-resolution image over its thumbnail. For a legacy prepared project, publication can backfill from `source-work/extracted/htmlz` without `prepare --rebuild` only when the edited units still contain the relevant markup and the extracted HTML/image tree is still present; otherwise rebuild or restore the missing source evidence.
- Rewrite pipe-table separator widths from maximum rendered cell width, counting East Asian wide/fullwidth glyphs as two columns. Apply this after HTML conversion and wide-table splitting.

Use `btp check-tables <project>` to compare raw HTML tables, existing pipe tables, cleaned pipe tables, continuation tables, and any unconverted table markers before a long publication build.

For python-docx post-processing, set `table.autofit = False` through the public API. Do not append `w:tblLayout` manually because invalid `tblPr` child order can make Word ignore the setting. Setting `first_line_indent = None` inherits the body style; use an explicit zero length for title-page and table-cell paragraphs that must not inherit a body indent.

Size fixed-layout tables from their content, not from Pandoc's source-line grid: measure each column's widest cell (counting East Asian wide/fullwidth glyphs as two), never drop below a readable minimum, and scale so every table spans the text column exactly. Write the computed widths into `w:tblGrid`, every `w:tcW` (as `dxa`), and `w:tblW`, so CJK headers never wrap one character per line and amounts never split mid-number.

## XHTML and EPUB

Persist the publication XHTML under `target/xhtml/`, with each lone image and its adjacent short caption bound into `<figure>`/`<figcaption>` and the caption used as alt text, and validate XML syntax, unique IDs, local links, note targets, and note backlinks before packaging. Generate EPUB from that XHTML with Pandoc navigation and EPUB-specific CSS. Validate container, manifest resources, spine order, nav/TOC targets, configured cover, and semantic note links, then run external EPUBCheck in Publication mode. Remove the Word TOC placeholder. Do not copy fixed paper page numbers into EPUB navigation.

When bilingual formats are configured, generate `target/alignment.json` from source segments and verified target units, then build source/target blocks from stable segment groups. Namespace note IDs by language side so references remain unique. Validate and EPUBCheck the bilingual package independently; success of the monolingual package does not cover it.

## Review candidate versus final build

`btp typeset <project>` creates `dist/typeset-preview/` for layout review and writes `qa/typeset-preview.json`, including the clean-master hash and DOCX/XHTML/EPUB/PDF format validation. It also refreshes `qa/publication-review-template.json`. It does not waive QA, update the build stage, or represent a final publication.

Complete every check in the review template—cover, TOC, chapter openings, running heads/feet, figures, dense tables, and narrow/mobile rendering—and record it with `btp record --kind publication-review`. Record approved exceptions before referencing their IDs. Then approve `publication_signoff`; its evidence hashes bind the exact candidate and review.

`btp build <project>` runs only after Visual and Completeness QA pass. In Publication mode it requires an approved, current `publication-signoff`, a candidate matching the clean-master hash, and an external EPUBCheck pass for every requested EPUB. It promotes the reviewed candidate bytes instead of rebuilding. Verify that DOCX/EPUB packages open, the PDF has pages, a requested TOC is populated, page numbers increment, headers change as intended, and rendered pages have no clipping, overlap, broken tables, or missing glyphs.

Publication validation compares each DOCX/EPUB package with the clean Markdown master: generated table counts must not fall below the master table count, embedded image counts must cover the master's unique local image references, and deterministic front/middle/back content samples must remain present. PDF validation checks the same text samples when extraction is available. These checks supplement visual review; they do not certify layout quality.

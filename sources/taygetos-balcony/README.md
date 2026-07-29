# Το Μπαλκόνι του Ταϋγέτου (1977–1988)

The community newspaper of **Γεωργίτσι**, Laconia — *"Όργανο συνδέσεως των
απανταχού Γεωργιτσιάνων"*, the paper connecting Georgitsi people wherever they
had settled. Issue 1 appeared in January 1977.

The archive is published by the rights holder at
[taygetosbalcony.gr](https://taygetosbalcony.gr/2025/06/03/αρχείο-εφημερίδας/)
as a single 203 MB PDF of 179 scanned sheets, under **CC BY-SA 4.0** with
attribution to **Christine K. Yannakaki**. That licence and attribution carry
over to the text here.

## What the scans look like

The PDF is 300 dpi bilevel scans with no text layer, so every character here
came from OCR. Two things about the source shape the pipeline:

- **122 sheets are single pages; 57 are two-page spreads.** A four-page issue
  was printed on one folded sheet, so a spread holds two *non-adjacent* pages:
  the outer sheet is `[page 4 | page 1]` and the inner sheet is `[page 2 | 3]`.
  `pipeline/prepare_pages.py` splits each spread at the fold, and
  `pipeline/build_issues.py` puts the pages back into reading order.

- **Spreads must be split before OCR.** Submitting a whole spread produces
  unusable output — the service downsamples the image to a fixed budget, and a
  spread spends half of that on the second page, so the glyphs land below
  legibility and the model hallucinates. Measured over 56 spreads, whole-spread
  OCR averaged a 0.61 Greek-letter ratio with 0.67 of lines being exact
  repeats. Split into single pages, the same material scores 0.99–1.00 with 0.00
  repeats.

Resolution beyond that point does not help: OCR accuracy was flat from native
300 dpi down to a 1200 px long edge, which is why pages are rendered at 200 dpi.

## Known problems

Every page is scored in [`ocr/quality.json`](ocr/) on Greek-letter ratio,
duplicate-line ratio and length. Pages that fail are listed there and marked
`ocr_flagged: true` in the article frontmatter.

The main remaining failure is **homoglyph drift**: on some caption and italic
blocks the model transcribes Greek as visually similar Latin letters, so
`Το παραδοσιακό σπίτι` comes out as `To npapadoxiok otnti`. This is not repaired
automatically. The Latin-to-Greek mapping is ambiguous — `c` could be `ς` or
`σ`, `n` could be `η` or `π` — so an automatic fix would inject silent errors
into a corpus whose value depends on being checkable. Those pages are flagged
and left for a human.

Article segmentation follows the OCR's own headings, which is the only
structural signal the scans carry. Expect some articles to be split at a
subheading, and some short items to be merged into the article above them.
Articles that continue on another page are flagged, not stitched.

## Layout

```
manifest.json     provenance, licence, checksum of the source PDF
raw/              the source PDF (gitignored; fetch with pipeline/fetch_source.py)
pages/            rendered single pages, spreads split (gitignored, regenerable)
ocr/              raw OCR text per page + quality.json
issues/           pages regrouped into issues, with dates
articles/         one file per article, with frontmatter
export/wordpress/ backdated REST payloads for taygetosbalcony.gr
```

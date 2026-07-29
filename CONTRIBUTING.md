# Contributing

Two kinds of contribution are especially useful: **correcting OCR text** and
**adding a source**.

## Correcting OCR

The text is machine-transcribed from scans of newsprint and has not been
hand-checked. Corrections are the most valuable thing you can contribute.

1. Find the article under `sources/<source-id>/articles/`.
2. Its frontmatter names the `page_image` it came from. Regenerate that image
   with `pipeline/prepare_pages.py` (or open the source PDF at the matching
   page) and read the text against the scan.
3. Fix the body text. Keep the frontmatter, and set `ocr_corrected: true` once
   the whole article has been checked against the scan.

Please correct what the page actually says, including its spelling and its
punctuation. This is a historical record: the goal is a faithful transcription,
not a modernised or improved one. Where the scan is genuinely unreadable, use
`[…]` rather than guessing.

Do not run the text through a language model to "clean it up". A model will
quietly rewrite names, dates and figures that it thinks look wrong, and there is
no way to tell those edits from real corrections afterwards.

### Pages already known to be bad

`sources/<source-id>/ocr/quality.json` scores every page. Anything with
`"ok": false` failed one of the automatic checks — usually the model drifting
out of Greek into Latin lookalike letters, or collapsing into a repetition loop.
Those pages are the highest-value ones to retype.

## Adding a source

A source needs to be openly licensed, or public domain, or yours to release.
If it is not, it does not go in — no exceptions, and please do not open a PR.

Add `sources/<source-id>/manifest.json` recording the title, language, the
licence, who to attribute, and where the scans came from with a checksum. Then
run the pipeline described in [README.md](README.md).

## Secrets

This repository is public. Never commit an API key, token or password.

Credentials belong in the environment or in a `.env` file **outside** the
repository, passed with `--env-file`. `.env` is gitignored and CI runs gitleaks
over the full history on every push. If you do leak a key, rotate it first and
rewrite history second: once it has been pushed, assume it is compromised.

## Style

Pipeline code is plain Python with `requests`, `Pillow` and `numpy`, and no
framework. Each stage reads a directory and writes a directory, so any stage can
be re-run on its own and re-running it is always safe.

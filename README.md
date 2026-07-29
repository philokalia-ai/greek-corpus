# greek-corpus

An open corpus of Greek text, built from openly-licensed sources that exist only
as scans. Each source is digitised with document OCR, segmented into individual
articles, and published as plain UTF-8 Markdown with structured metadata — so it
can be read, searched, quoted, corrected, and used to train and evaluate models.

The first source is **Το Μπαλκόνι του Ταϋγέτου**, the community newspaper of
Γεωργίτσι in Laconia, covering **1977–1988**.

| Source | Years | Pages | Licence |
| --- | --- | --- | --- |
| [Το Μπαλκόνι του Ταϋγέτου](sources/taygetos-balcony/) | 1977–1988 | 179 | CC BY-SA 4.0 |

## Why

Greek is under-represented in open text corpora, and the gap is widest for
ordinary twentieth-century prose: local news, letters, notices, obituaries,
village correspondence. That material is not in any digital archive. It exists
as paper, and increasingly as PDFs of paper, which are just as unreadable to a
search engine or a language model as the paper was.

This repository turns those PDFs into text and keeps the provenance attached.

## Repository layout

```
sources/<source-id>/
  manifest.json        provenance, licence, checksums of the source scans
  raw/                 the original scans (gitignored, fetched on demand)
  ocr/                 raw OCR output, one file per scanned page
  issues/              pages grouped into issues, with detected dates
  articles/            one Markdown file per article, with YAML frontmatter
  export/              publish-ready payloads
pipeline/              the scripts that produce all of the above
```

Everything under `ocr/`, `issues/`, `articles/` and `export/` is generated. The
pipeline is deterministic and re-runnable; `raw/` is the only input.

## Reproducing the corpus

```bash
pip install requests

# 1. fetch the source scans named in the manifest (203 MB for this source)
python3 pipeline/fetch_source.py sources/taygetos-balcony

# 2. OCR every page (resumable; skips pages that already have output)
export MISTRAL_API_KEY=...
python3 pipeline/ocr_pdf.py \
    sources/taygetos-balcony/raw/archive-1977-1988.pdf \
    sources/taygetos-balcony/ocr

# 3. group pages into issues, then split issues into articles
python3 pipeline/build_issues.py sources/taygetos-balcony
python3 pipeline/segment_articles.py sources/taygetos-balcony

# 4. build publish-ready payloads
python3 pipeline/export_wordpress.py sources/taygetos-balcony
```

## Secrets

**This is a public repository. No key, token or password belongs in it.**

- `.env` and `*.key` / `*.pem` / `credentials.json` are gitignored. `.env.example`
  documents the variable names and nothing else.
- Every script reads credentials from the environment, or from a `.env` file
  outside the repository via `--env-file`. Nothing writes a credential to disk,
  logs one, or embeds one in generated output.
- CI runs [gitleaks](https://github.com/gitleaks/gitleaks) on every push and pull
  request, over the full history, and fails the build if a `.env` is ever tracked.
- Uploaded scans are deleted from the OCR provider after each page is processed.

If you do leak a key, rotate it first and rewrite history second — a key that has
been pushed to GitHub should be assumed compromised even after a force-push.

## OCR quality

The text is **raw, uncorrected OCR** of 1977–1988 newsprint. Mastheads, datelines
and headlines come through reliably; body text on faded or tightly-set columns
does not always. Expect errors in accents, in the ς/σ distinction, and in words
broken across columns.

Nothing here has been silently "cleaned up" by a language model — what is in the
files is what the OCR returned, so that corrections are visible in the diff and
the text can always be checked against the page it came from. Every article
records its source page, so any passage can be verified against the scan.

Corrections are very welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

- **Corpus text** (`sources/`) — [CC BY-SA 4.0](LICENSE), following the licence of
  the source archive. Το Μπαλκόνι του Ταϋγέτου is published under CC BY-SA 4.0
  with attribution to **Christine K. Yannakaki**; that attribution travels with
  the text and with anything derived from it.
- **Pipeline code** (`pipeline/`) — [MIT](LICENSE-CODE).

Adding a source means adding a `manifest.json` that records where it came from
and under what licence. Sources that are not openly licensed do not go in.

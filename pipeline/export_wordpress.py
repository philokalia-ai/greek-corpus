#!/usr/bin/env python3
"""Turn segmented articles into WordPress REST payloads, backdated to publication.

Each article becomes one post carrying the date of the issue it appeared in, so
the archive lands on the site's timeline where it belongs rather than all at
once. Articles the OCR flagged, or whose date could not be read, are exported
too but marked so they can be held back.

    python3 pipeline/export_wordpress.py sources/taygetos-balcony

Writes export/wordpress/posts.json plus a human-readable summary. Publishing is
a separate step: pipeline/publish_wordpress.py.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import unicodedata

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# The nameplate at the top of every front page reads as a headline, so each
# issue contributes one "article" that is just the paper's own name. Anchored on
# both ends, so a real headline that merely contains the village name is kept.
MASTHEAD_TITLE = re.compile(
    r"\A[^Α-Ωα-ω]*(?:ΤΟ\s+)?(?:ΓΕΩΡΓΙΤΣΙ|ΜΠΑΛΚΟΝΙ\s+ΤΟΥ\s+ΤΑ[ΫΥ]ΓΕΤΟΥ)[^Α-Ωα-ω]*\Z"
)

# Issue dates are only accurate to the month, so every article in an issue would
# otherwise carry an identical timestamp and WordPress would order them
# arbitrarily -- discarding the reading order recovered from the folded sheets.
# Spacing them preserves that order within the correct month.
FIRST_POST_HOUR = 8
MINUTES_BETWEEN_POSTS = 2
LAST_MINUTE_OF_DAY = 23 * 60 + 58


def post_time(index: int) -> str:
    minute = min(FIRST_POST_HOUR * 60 + index * MINUTES_BETWEEN_POSTS, LAST_MINUTE_OF_DAY)
    return f"{minute // 60:02d}:{minute % 60:02d}:00"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        meta[key.strip()] = value
    return meta, text[m.end():]


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
    )


def to_html(markdown_body: str) -> str:
    """Render the article body as the small subset of HTML WordPress needs."""
    lines = markdown_body.splitlines()
    # Drop the leading H1; WordPress renders the post title itself.
    while lines and (not lines[0].strip() or lines[0].startswith("# ")):
        if lines[0].startswith("# "):
            lines.pop(0)
            break
        lines.pop(0)

    html: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            text = " ".join(l.strip() for l in paragraph).strip()
            if text:
                html.append(f"<p>{escape(text)}</p>")
            paragraph.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        heading = re.match(r"^(#{2,4})\s+(.*)$", stripped)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 6)
            html.append(f"<h{level}>{escape(heading.group(2))}</h{level}>")
            continue
        paragraph.append(stripped)
    flush()
    return "\n\n".join(html)


def escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def excerpt(body: str, limit: int = 240) -> str:
    plain = re.sub(r"\s+", " ", re.sub(r"[#*_`]", "", body)).strip()
    if len(plain) <= limit:
        return plain
    cut = plain[:limit]
    return cut[: cut.rfind(" ")] + "…"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--status", default="draft", choices=["draft", "publish", "pending", "private"],
                    help="post status to request (default draft, so nothing goes live by accident)")
    ap.add_argument("--category", default="Αρχείο 1977-1988",
                    help="category name to attach to every post")
    args = ap.parse_args()

    src = args.source_dir
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    articles_dir = src / "articles"
    catalogue = json.loads((articles_dir / "articles.json").read_text(encoding="utf-8"))

    out_dir = src / "export" / "wordpress"
    out_dir.mkdir(parents=True, exist_ok=True)

    attribution = manifest["attribution"]
    licence = manifest["license"]
    posts = []

    per_issue: collections.Counter = collections.Counter()
    skipped_mastheads = 0

    for entry in catalogue:
        if MASTHEAD_TITLE.match(entry["title"].strip()):
            skipped_mastheads += 1
            continue
        text = (articles_dir / entry["file"]).read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        html = to_html(body)
        if not html.strip():
            continue
        sequence = per_issue[entry["issue_file"]]
        per_issue[entry["issue_file"]] += 1

        issue_ref = f" — τεύχος {entry['issue']}" if entry.get("issue") else ""
        credit = (
            f'<p><em>Από το αρχείο της εφημερίδας «{manifest["title"]}»{issue_ref}. '
            f'Ψηφιοποίηση με OCR· το κείμενο ενδέχεται να περιέχει λάθη. '
            f'Άδεια {licence}, απόδοση: {attribution}.</em></p>'
        )

        posts.append({
            "slug": pathlib.Path(entry["file"]).stem,
            "title": entry["title"],
            "content": html + "\n\n" + credit,
            "excerpt": excerpt(body),
            "date": f"{entry['date']}T{post_time(sequence)}" if entry.get("date") else None,
            "status": args.status,
            "category": args.category,
            "meta": {
                "corpus_source_id": manifest["id"],
                "corpus_issue": entry.get("issue"),
                "corpus_page_image": entry.get("page_image"),
                "corpus_license": licence,
                "corpus_attribution": attribution,
            },
            "_flags": {
                "ocr_flagged": entry.get("ocr_flagged", False),
                "undated": not entry.get("date"),
                "possible_continuation": entry.get("possible_continuation", False),
                "date_confidence": entry.get("date_confidence"),
            },
        })

    (out_dir / "posts.json").write_text(
        json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    flagged = sum(1 for p in posts if p["_flags"]["ocr_flagged"])
    undated = sum(1 for p in posts if p["_flags"]["undated"])
    dates = sorted(p["date"][:10] for p in posts if p["date"])
    print(f"{len(posts)} posts written to {out_dir / 'posts.json'}")
    print(f"  mastheads skipped: {skipped_mastheads}")
    print(f"  status:       {args.status}")
    if dates:
        print(f"  date range:   {dates[0]} .. {dates[-1]}")
    print(f"  ocr-flagged:  {flagged}")
    print(f"  undated:      {undated}  (these need a date before publishing)")


if __name__ == "__main__":
    main()

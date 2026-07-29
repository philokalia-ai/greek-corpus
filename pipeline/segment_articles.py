#!/usr/bin/env python3
"""Split issue text into individual articles with YAML frontmatter.

The OCR marks headlines as Markdown headings, which is the only structural
signal the scans carry, so headings are the cut points. Fragments too short to
be an article are attached to the preceding one rather than emitted, and
articles that the paper itself marks as continuing from another page are
flagged instead of being silently stitched together.

    python3 pipeline/segment_articles.py sources/taygetos-balcony
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import unicodedata

HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
PAGE_MARKER = re.compile(r"^<!--\s*page image:\s*(\S+?)\.png\s*-->$")
IMAGE_PLACEHOLDER = re.compile(r"!\[[^\]]*\]\([^)]*\)")
CONTINUATION = re.compile(r"συνεχ|απο\s+τη\s*\d?\s*σελ|σελιδα\s*\d", re.IGNORECASE)

MIN_ARTICLE_CHARS = 180


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
    )


def slugify(title: str, fallback: str) -> str:
    base = strip_accents(title).lower()
    base = re.sub(r"[^a-zα-ω0-9]+", "-", base).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    return (base[:60].strip("-") or fallback)


def clean(text: str) -> str:
    text = IMAGE_PLACEHOLDER.sub("", text)
    lines = [l.rstrip() for l in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return "\n".join(out).strip()


def yaml_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def split_issue(text: str) -> list[dict]:
    """Cut an issue's markdown into (title, body, page) records."""
    current_page = None
    articles: list[dict] = []
    buffer: list[str] = []
    title = None
    title_page = None

    def flush() -> None:
        nonlocal buffer, title, title_page
        body = clean("\n".join(buffer))
        if title is None and not body:
            buffer = []
            return
        if title is not None:
            if body and len(body) < MIN_ARTICLE_CHARS and articles:
                # Too small to stand alone: fold back into the previous article.
                articles[-1]["body"] += f"\n\n## {title}\n\n{body}"
            else:
                articles.append({"title": title, "body": body, "page": title_page})
        buffer = []
        title = None

    for line in text.splitlines():
        marker = PAGE_MARKER.match(line.strip())
        if marker:
            current_page = marker.group(1)
            continue
        heading = HEADING.match(line)
        if heading:
            flush()
            title = heading.group(2).strip()
            title_page = current_page
            continue
        buffer.append(line)
    flush()
    return [a for a in articles if a["body"]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    args = ap.parse_args()

    src = args.source_dir
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    issues = json.loads((src / "issues" / "issues.json").read_text(encoding="utf-8"))
    out_dir = src / "articles"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.md"):
        stale.unlink()

    catalogue = []
    total = 0
    for issue in issues:
        issue_text = (src / "issues" / issue["file"]).read_text(encoding="utf-8")
        # Drop the synthetic issue heading added by build_issues.
        issue_text = "\n".join(issue_text.splitlines()[1:])
        label = issue["file"].removesuffix(".md")

        for n, article in enumerate(split_issue(issue_text), start=1):
            slug = slugify(article["title"], f"arthro-{n}")
            name = f"{label}-{n:02d}-{slug}"
            flagged = article["page"] in issue.get("flagged_pages", [])
            continued = bool(CONTINUATION.search(strip_accents(article["title"]).lower()))

            record = {
                "file": f"{name}.md",
                "title": article["title"],
                "issue": issue.get("issue_number"),
                "issue_file": issue["file"],
                "date": issue.get("date"),
                "date_confidence": issue.get("date_confidence"),
                "page_image": article["page"],
                "chars": len(article["body"]),
                "ocr_flagged": flagged,
                "possible_continuation": continued,
            }
            catalogue.append(record)

            front = [
                "---",
                f"title: {yaml_escape(article['title'])}",
                f"date: {issue.get('date') or 'null'}",
                f"date_confidence: {issue.get('date_confidence')}",
                f"issue: {issue.get('issue_number') if issue.get('issue_number') else 'null'}",
                f"source: {yaml_escape(manifest['title'])}",
                f"source_id: {manifest['id']}",
                f"page_image: {article['page']}",
                f"language: {manifest['language']}",
                f"license: {manifest['license']}",
                f"attribution: {yaml_escape(manifest['attribution'])}",
                f"ocr_model: {manifest['ocr']['model']}",
                "ocr_corrected: false",
                f"ocr_flagged: {'true' if flagged else 'false'}",
                f"possible_continuation: {'true' if continued else 'false'}",
                "---",
                "",
                f"# {article['title']}",
                "",
                article["body"],
                "",
            ]
            (out_dir / f"{name}.md").write_text("\n".join(front), encoding="utf-8")
            total += 1

    (out_dir / "articles.json").write_text(
        json.dumps(catalogue, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    flagged = sum(1 for a in catalogue if a["ocr_flagged"])
    undated = sum(1 for a in catalogue if not a["date"])
    words = sum(a["chars"] for a in catalogue)
    print(f"{total} articles from {len(issues)} issues")
    print(f"  {words:,} characters")
    print(f"  {flagged} on pages flagged by the OCR quality gate")
    print(f"  {undated} without a confident date")


if __name__ == "__main__":
    main()

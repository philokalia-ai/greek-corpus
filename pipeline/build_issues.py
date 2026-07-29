#!/usr/bin/env python3
"""Group OCR'd page images into newspaper issues and date them.

A four-page issue was printed on one folded sheet, so a scanned spread carries
two non-adjacent pages: the outer sheet is [back | front] and the inner sheet is
[2 | 3]. Front pages are found by their masthead, which also carries the issue
number and the month; every page after a front page belongs to that issue, and
the back half of the sheet the front came from is moved to the end of the issue.

    python3 pipeline/build_issues.py sources/taygetos-balcony

Writes issues/issues.json and one issues/NNN.md per issue. The inferred ordering
is written out in full so it can be checked against the scans and corrected.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import unicodedata

MONTHS = {
    "ΙΑΝΟΥΑΡ": 1, "ΦΕΒΡΟΥΑΡ": 2, "ΜΑΡΤ": 3, "ΑΠΡΙΛ": 4, "ΜΑ": 5,
    "ΙΟΥΝ": 6, "ΙΟΥΛ": 7, "ΑΥΓΟΥΣΤ": 8, "ΣΕΠΤΕΜΒΡ": 9,
    "ΟΚΤΩΒΡ": 10, "ΝΟΕΜΒΡ": 11, "ΔΕΚΕΜΒΡ": 12,
}
# Longest first so ΙΑΝΟΥΑΡ wins over ΜΑ, and ΜΑΡΤ over ΜΑ.
MONTH_ORDER = sorted(MONTHS, key=len, reverse=True)

MASTHEAD_TOKENS = ("ΜΠΑΛΚΟΝΙ", "ΤΑΥΓΕΤΟΥ", "ΓΕΩΡΓΙΤΣΙΑΝΩΝ", "ΓΕΩΡΓΙΤΣΙ")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    return strip_accents(text).upper().replace("Ϊ", "Ι").replace("Ϋ", "Υ")


def find_month(text: str) -> int | None:
    norm = normalize(text)
    best = None
    for stem in MONTH_ORDER:
        idx = norm.find(stem)
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, MONTHS[stem])
    return best[1] if best else None


def find_year(text: str) -> int | None:
    for m in re.finditer(r"\b(19[6-9]\d)\b", text):
        year = int(m.group(1))
        if 1975 <= year <= 1995:
            return year
    return None


def is_front_page(text: str) -> bool:
    head = normalize("\n".join(text.splitlines()[:25]))
    hits = sum(1 for token in MASTHEAD_TOKENS if token in head)
    has_dateline = "ΦΥΛΛΟ" in head or "ΕΤΟΣ" in head
    return hits >= 2 and has_dateline


def dateline_region(text: str) -> str:
    """The masthead line carrying ΕΤΟΣ/ΦΥΛΛΟ, plus its neighbours.

    Reading the date off the whole page header picks up years mentioned in the
    first article instead of the publication date, so narrow it to the line the
    issue number sits on.
    """
    lines = text.splitlines()[:25]
    for i, line in enumerate(lines):
        norm = normalize(line)
        if "ΦΥΛΛΟ" in norm or "ΕΤΟΣ" in norm:
            return "\n".join(lines[max(0, i - 1): i + 2])
    return "\n".join(lines)


def parse_dateline(text: str) -> dict:
    region = dateline_region(text)
    norm = normalize(region)
    issue_no = None
    m = re.search(r"ΦΥΛΛΟΥ?\s*[:.]?\s*(\d{1,3})", norm)
    if m:
        issue_no = int(m.group(1))
    year_no = None
    m = re.search(r"ΕΤΟΣ\s*(\d{1,2})", norm)
    if m:
        year_no = int(m.group(1))
    return {
        "issue_number": issue_no,
        "volume_year": year_no,
        "month": find_month(region),
        "year": find_year(region),
    }


def ordered_images(index: dict) -> list[str]:
    out = []
    for key in sorted(index, key=int):
        for part in index[key]["parts"]:
            out.append(pathlib.Path(part["file"]).stem)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    args = ap.parse_args()

    src = args.source_dir
    index = json.loads((src / "pages" / "index.json").read_text(encoding="utf-8"))
    ocr_dir = src / "ocr"
    out_dir = src / "issues"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Read the per-page scores rather than the summary, which is only written
    # when a full OCR run finishes and so is missing or stale after a partial one.
    quality = {}
    for score_file in ocr_dir.glob("[0-9]*.json"):
        try:
            quality[score_file.stem] = json.loads(score_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

    texts = {}
    for stem in ordered_images(index):
        path = ocr_dir / f"{stem}.md"
        texts[stem] = path.read_text(encoding="utf-8") if path.exists() else ""

    sequence = [s for s in ordered_images(index) if s in texts]
    fronts = [s for s in sequence if is_front_page(texts[s])]
    print(f"{len(sequence)} page images, {len(fronts)} front pages detected")

    issues = []
    for n, front in enumerate(fronts):
        start = sequence.index(front)
        end = sequence.index(fronts[n + 1]) if n + 1 < len(fronts) else len(sequence)
        pages = sequence[start:end]

        # The front came from the right half of a sheet; its left half is the
        # back page of the same issue, so it belongs at the end, not the start.
        pdf_key = front[:4]
        sibling = f"{pdf_key}a"
        if front.endswith("b") and sibling in texts:
            pages = [p for p in pages if p != sibling] + [sibling]

        meta = parse_dateline(texts[front])
        issues.append({
            "sequence": n + 1,
            "front_page": front,
            "pages": pages,
            "page_count": len(pages),
            "flagged_pages": [p for p in pages if not quality.get(p, {}).get("ok", True)],
            **meta,
        })

    # Fill gaps in the numbering where a masthead did not OCR cleanly.
    for issue in issues:
        if issue["year"] and issue["month"]:
            issue["date"] = f"{issue['year']:04d}-{issue['month']:02d}-01"
            issue["date_confidence"] = "month"
        else:
            issue["date"] = None
            issue["date_confidence"] = "unknown"

    for issue in issues:
        num = issue["issue_number"]
        name = f"{num:03d}" if num else f"seq{issue['sequence']:03d}"
        body = [f"# {name} — {issue.get('date') or 'undated'}", ""]
        for page in issue["pages"]:
            body.append(f"<!-- page image: {page}.png -->")
            body.append(texts[page].strip())
            body.append("")
        (out_dir / f"{name}.md").write_text("\n".join(body), encoding="utf-8")
        issue["file"] = f"{name}.md"

    (out_dir / "issues.json").write_text(
        json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    dated = sum(1 for i in issues if i["date"])
    numbered = sum(1 for i in issues if i["issue_number"])
    print(f"{len(issues)} issues; {numbered} with an issue number, {dated} dated")
    years = sorted({i["year"] for i in issues if i["year"]})
    if years:
        print(f"years covered: {years[0]}-{years[-1]}")
    undated = [i["sequence"] for i in issues if not i["date"]]
    if undated:
        print(f"undated issues (need manual dating): {undated}")


if __name__ == "__main__":
    main()

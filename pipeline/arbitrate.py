#!/usr/bin/env python3
"""Let the second reader settle the pairs a language prior cannot.

build_gazetteer.py leaves two kinds of pair undecided: both spellings are real
words, so frequency would just rewrite the rarer one, or neither is, so the
lexicon has no opinion. Both need evidence from the page rather than from
Greek. Tesseract read the same pixels independently, so for each page carrying
a suspect word we can ask what the other engine saw there.

A pair is resolved when the second reader saw one spelling and not the other.
Where it saw neither, or both, the pair goes to verify_words.py to be re-read
from the image directly.

    python3 pipeline/arbitrate.py sources/taygetos-balcony
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from greek_text import GREEK_WORD, fold  # noqa: E402


def page_tokens(tess_dir: pathlib.Path) -> dict[str, collections.Counter]:
    out: dict[str, collections.Counter] = {}
    for path in sorted(tess_dir.glob("[0-9]*.txt")):
        counter: collections.Counter = collections.Counter()
        for match in GREEK_WORD.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            counter[fold(match.group(0))] += 1
        out[path.stem] = counter
    return out


def pages_containing(text_dir: pathlib.Path) -> dict[str, set[str]]:
    """Which pages each folded token appears on, in the primary transcription."""
    index: dict[str, set[str]] = {}
    for path in sorted(text_dir.glob("[0-9]*.md")):
        for match in GREEK_WORD.finditer(path.read_text(encoding="utf-8")):
            index.setdefault(fold(match.group(0)), set()).add(path.stem)
    return index


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--text-dir", default="corrected")
    args = ap.parse_args()

    src = args.source_dir
    gaz = src / "gazetteer"
    tess = src / "tesseract"
    if not tess.exists():
        sys.exit("no tesseract/ output; run pipeline/ocr_tesseract.py first")

    second = page_tokens(tess)
    located = pages_containing(src / args.text_dir)
    print(f"second reader: {len(second)} pages, "
          f"{sum(sum(c.values()) for c in second.values()):,} words")

    with (gaz / "confusion_pairs.tsv").open(encoding="utf-8") as fh:
        pairs = list(csv.DictReader(fh, delimiter="\t"))

    undecided = [p for p in pairs if p["confidence"] != "high"]
    print(f"{len(undecided)} pairs left undecided by the lexicon")

    results = []
    tally: collections.Counter = collections.Counter()
    for pair in undecided:
        suspect, candidate = pair["suspect"], pair["candidate"]
        pages = located.get(suspect, set())
        saw_suspect = sum(second.get(p, {}).get(suspect, 0) for p in pages)
        saw_candidate = sum(second.get(p, {}).get(candidate, 0) for p in pages)

        if saw_candidate and not saw_suspect:
            verdict = "candidate"
        elif saw_suspect and not saw_candidate:
            verdict = "suspect"
        elif saw_suspect and saw_candidate:
            verdict = "both-seen"
        else:
            verdict = "unseen"
        tally[verdict] += 1
        results.append({
            "suspect": suspect,
            "candidate": candidate,
            "pages": ",".join(sorted(pages)),
            "second_saw_suspect": saw_suspect,
            "second_saw_candidate": saw_candidate,
            "verdict": verdict,
            "prior_confidence": pair["confidence"],
        })

    results.sort(key=lambda r: (r["verdict"] != "candidate", -r["second_saw_candidate"]))
    with (gaz / "arbitration.tsv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=list(results[0]) if results else
                                ["suspect", "candidate", "pages", "second_saw_suspect",
                                 "second_saw_candidate", "verdict", "prior_confidence"])
        writer.writeheader()
        writer.writerows(results)

    print(f"\n{dict(tally)}")
    print("  candidate = second reader backs the correction; actionable")
    print("  suspect   = second reader backs the page as transcribed; leave it")
    print("  both-seen = the word occurs both ways on the page; needs the crop")
    print("  unseen    = second reader read neither; needs the crop")

    (gaz / "arbitration_summary.json").write_text(
        json.dumps(dict(tally), ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()

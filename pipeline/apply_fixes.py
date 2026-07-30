#!/usr/bin/env python3
"""Apply adjudicated single-letter fixes to the corrected text, in place.

Each fix comes from build_gazetteer.py: a rare spelling one confusable letter
away from a spelling the paper uses far more often. Only the differing letter is
rewritten, so accents and capitalisation elsewhere in the word survive, and only
whole words are touched.

    python3 pipeline/apply_fixes.py sources/taygetos-balcony --confidence high

Nothing listed in gazetteer/names.txt is ever changed. Every replacement is
written to gazetteer/applied.tsv, and raw OCR is untouched, so any fix can be
read back out of the changelog or the diff.
"""
from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from greek_text import GREEK_WORD, fold, learn_confusions  # noqa: E402

LEVELS = {"high": {"high"}, "medium": {"high", "medium"}, "all": {"high", "medium", "low"}}


def load_names(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        fold(line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def recase(original: str, suspect: str, candidate: str) -> str | None:
    """Rewrite only the letters that differ, keeping the original's casing.

    fold() removes combining marks without changing length, so positions in the
    folded forms line up with positions in the word as printed.
    """
    if len(original) != len(suspect) or len(suspect) != len(candidate):
        return None
    out = []
    for original_ch, suspect_ch, candidate_ch in zip(original, suspect, candidate):
        if suspect_ch == candidate_ch:
            out.append(original_ch)
        else:
            out.append(candidate_ch if original_ch.isupper() else candidate_ch.lower())
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--confidence", default="high", choices=list(LEVELS))
    ap.add_argument("--text-dir", default="corrected")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = args.source_dir
    gaz = src / "gazetteer"
    accept = LEVELS[args.confidence]
    protected = load_names(gaz / "names.txt")

    fixes: dict[str, str] = {}
    skipped_protected = 0
    with (gaz / "confusion_pairs.tsv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            # A confirmed name vouches for a spelling the lexicon cannot, which is
            # the whole point of the gazetteer: ΛΟΥΤΡΑΚΙΟΥ is a real word here
            # even though no general dictionary declines toponyms.
            vouched = row["candidate"] in protected
            if row["confidence"] not in accept and not vouched:
                continue
            if row["suspect"] in protected:
                skipped_protected += 1
                continue
            if len(row["suspect"]) == len(row["candidate"]):
                fixes[row["suspect"]] = row["candidate"]

    print(f"{len(fixes)} fixes at confidence '{args.confidence}'"
          f"{f', {skipped_protected} held back by names.txt' if skipped_protected else ''}")
    if not fixes:
        return

    applied: list[dict] = []
    counts: collections.Counter = collections.Counter()
    text_dir = src / args.text_dir

    for path in sorted(text_dir.glob("[0-9]*.md")):
        text = path.read_text(encoding="utf-8")

        def replace(match: re.Match) -> str:
            word = match.group(0)
            folded = fold(word)
            if folded in protected or folded not in fixes:
                return word
            rewritten = recase(word, folded, fixes[folded])
            if rewritten is None or rewritten == word:
                return word
            applied.append({
                "page": path.stem,
                "before": word,
                "after": rewritten,
                "folded_from": folded,
                "folded_to": fixes[folded],
            })
            counts[f"{folded}->{fixes[folded]}"] += 1
            return rewritten

        updated = GREEK_WORD.sub(replace, text)
        if updated != text and not args.dry_run:
            path.write_text(updated, encoding="utf-8")

    print(f"{len(applied)} replacements across "
          f"{len({a['page'] for a in applied})} pages"
          f"{' (dry run, nothing written)' if args.dry_run else ''}")

    if args.dry_run:
        for row in applied[:15]:
            print(f"   {row['page']}  {row['before']} -> {row['after']}")
        return

    # The changelog is the audit trail for edits made in place, so successive
    # runs add to it rather than replacing what an earlier run recorded.
    log_path = gaz / "applied.tsv"
    history: list[dict] = []
    if log_path.exists():
        with log_path.open(encoding="utf-8") as fh:
            history = list(csv.DictReader(fh, delimiter="\t"))
    history.extend(applied)

    with log_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, delimiter="\t",
            fieldnames=["page", "before", "after", "folded_from", "folded_to"],
        )
        writer.writeheader()
        writer.writerows(history)

    # The fixes that were actually needed are the best evidence of which letter
    # pairs this scan confuses, which is better than a hand-written table.
    learned = learn_confusions([(a["folded_from"], a["folded_to"]) for a in history])
    lines = ["# Letter confusions observed in applied fixes, most frequent first.",
             "# source -> corrected (count)"]
    for source, targets in sorted(learned.items(), key=lambda kv: -sum(c for _, c in kv[1])):
        rendered = ", ".join(f"{t} ({c})" for t, c in targets)
        lines.append(f"{source} -> {rendered}")
    (gaz / "observed_confusions.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\nmost frequent repairs:")
    for name, count in counts.most_common(10):
        print(f"   {name}  x{count}")


if __name__ == "__main__":
    main()

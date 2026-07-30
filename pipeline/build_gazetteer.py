#!/usr/bin/env python3
"""Find the names this paper uses, and the pairs of spellings that disagree.

A general Greek lexicon is the wrong tool for a village newspaper: a third of
its distinct words are outside one, because they are surnames, toponyms,
abbreviations and 1970s orthography. Correcting against a dictionary turns
ΤΟΥΡΝΑΣ into ΓΟΥΡΝΑΣ and ΑΡΙΘ. into ΑΡΙΟ.

What does work is that the paper repeats its own vocabulary. Two spellings that
differ by exactly one letter the OCR is known to confuse are almost always the
same word read two ways, and one of them is wrong. This lists those pairs for
adjudication, and the frequent unknown words that are worth confirming once as
names, after which they become authoritative.

    python3 pipeline/build_gazetteer.py sources/taygetos-balcony --lexicon el.dic

A Greek Hunspell lexicon is needed for the "is this a known word" column. It is
not vendored here, to keep this repository's licensing clean:

    curl -sSfL -o el.dic \
      https://raw.githubusercontent.com/wooorm/dictionaries/main/dictionaries/el/index.dic
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from greek_text import (  # noqa: E402
    GREEK_WORD,
    confusion_variants,
    fold,
    is_morphological_variant,
    looks_like_abbreviation,
    sounds_same,
)

MIN_LENGTH = 4
FRONTMATTER_FENCE = "---"


def load_lexicon(path: pathlib.Path | None) -> set[str]:
    if not path:
        return set()
    lex = set()
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in lines[1:] if lines and lines[0].strip().isdigit() else lines:
        word = line.split("/")[0].strip()
        if word:
            lex.add(fold(word))
    return lex


def corpus_frequencies(text_dir: pathlib.Path) -> collections.Counter:
    freq: collections.Counter = collections.Counter()
    for path in sorted(text_dir.glob("[0-9]*.md")):
        for match in GREEK_WORD.finditer(path.read_text(encoding="utf-8")):
            token = fold(match.group(0))
            if len(token) >= MIN_LENGTH:
                freq[token] += 1
    return freq


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--lexicon", type=pathlib.Path, default=None)
    ap.add_argument("--text-dir", default="corrected",
                    help="which transcription layer to scan (default corrected)")
    ap.add_argument("--min-count", type=int, default=3,
                    help="minimum occurrences for a name candidate")
    args = ap.parse_args()

    src = args.source_dir
    out_dir = src / "gazetteer"
    out_dir.mkdir(parents=True, exist_ok=True)

    lexicon = load_lexicon(args.lexicon)
    freq = corpus_frequencies(src / args.text_dir)
    print(f"lexicon {len(lexicon):,} forms; corpus {sum(freq.values()):,} tokens, "
          f"{len(freq):,} distinct")

    known = {w for w in freq if w in lexicon}
    unknown = {w for w in freq if w not in lexicon}
    print(f"outside the lexicon: {len(unknown):,} distinct")

    # Pairs of spellings in this corpus one confusable letter apart. Same-sounding
    # pairs are period orthography, not OCR damage, so they are excluded.
    pairs = []
    seen: set[tuple[str, str]] = set()
    for token in freq:
        for variant in confusion_variants(token):
            if variant not in freq:
                continue
            key = tuple(sorted((token, variant)))
            if key in seen:
                continue
            seen.add(key)
            a, b = key
            # Period orthography, period grammar and truncations are all correct
            # as printed; only a misread letter is an error worth listing.
            if sounds_same(a, b) or is_morphological_variant(a, b):
                continue
            if looks_like_abbreviation(a) or looks_like_abbreviation(b):
                continue
            a_known, b_known = a in lexicon, b in lexicon
            if a_known and b_known:
                # Both are real words; a substitution here is a genuine ambiguity
                # the image has to settle, not something to guess at.
                verdict = "both-known"
            elif a_known != b_known:
                verdict = "one-known"
            else:
                verdict = "neither-known"
            # Point at the rarer spelling: the paper's own usage is the best
            # available witness, and it outvotes lexicon membership, which
            # misfires on names the dictionary simply lacks.
            wrong, right = (a, b) if freq[a] <= freq[b] else (b, a)
            ratio = freq[right] / max(1, freq[wrong])
            pairs.append({
                "verdict": verdict,
                "suspect": wrong,
                "suspect_count": freq[wrong],
                "candidate": right,
                "candidate_count": freq[right],
                "freq_ratio": round(ratio, 1),
                "candidate_in_lexicon": right in lexicon,
                # Only a non-word can be safely called an error. Greek is full of
                # real words one confusable letter apart -- χωρίς beside χωριό,
                # είμαι beside είναι, and every -ουμε/-ουνε verb ending, which
                # differ by exactly the Μ/Ν confusion. Ranking those by frequency
                # rewrites grammatical person, so a pair of two real words is
                # never actionable without the image.
                "confidence": (
                    "high" if wrong not in lexicon and right in lexicon and ratio >= 3
                    else "medium" if wrong not in lexicon and (right in lexicon or ratio >= 3)
                    else "low"
                ),
            })

    order = {"high": 0, "medium": 1, "low": 2}
    pairs.sort(key=lambda r: (order[r["confidence"]], -r["freq_ratio"], -r["suspect_count"]))
    with (out_dir / "confusion_pairs.tsv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(pairs[0]) if pairs else
                                ["verdict", "suspect", "suspect_count", "candidate",
                                 "candidate_count", "candidate_in_lexicon"],
                                delimiter="\t")
        writer.writeheader()
        writer.writerows(pairs)

    by_verdict = collections.Counter(p["verdict"] for p in pairs)
    print(f"\nconfusion pairs found: {len(pairs):,}  {dict(by_verdict)}")
    print("  one-known      = the unknown side is very likely the OCR error")
    print("  neither-known  = probably a name; needs the gazetteer or the image")
    print("  both-known     = real ambiguity; only the scan can settle it")

    # Frequent unknown words, grouped by their first letters so the inflected
    # forms of one name sit together for a single decision.
    candidates = sorted(
        (w for w in unknown if freq[w] >= args.min_count),
        key=lambda w: (w[:5], -freq[w]),
    )
    with (out_dir / "name_candidates.tsv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["stem", "token", "count", "confirm_as_name"])
        for word in candidates:
            writer.writerow([word[:5], word, freq[word], ""])
    print(f"name candidates (>={args.min_count} occurrences): {len(candidates):,} "
          f"-> gazetteer/name_candidates.tsv")

    confirmed_path = out_dir / "names.txt"
    if not confirmed_path.exists():
        confirmed_path.write_text(
            "# One confirmed name or place per line, folded uppercase, no accents.\n"
            "# Inflected forms each get their own line: ΛΟΥΤΡΑΚΙ and ΛΟΥΤΡΑΚΙΟΥ.\n"
            "# Anything listed here is treated as correct and never 'corrected'.\n",
            encoding="utf-8",
        )
        print(f"created empty {confirmed_path} for confirmations")

    (out_dir / "summary.json").write_text(
        json.dumps({
            "text_layer": args.text_dir,
            "tokens": sum(freq.values()),
            "distinct": len(freq),
            "in_lexicon": len(known),
            "outside_lexicon": len(unknown),
            "confusion_pairs": by_verdict,
            "name_candidates": len(candidates),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

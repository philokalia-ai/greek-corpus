#!/usr/bin/env python3
"""Read every page a second time with Tesseract, keeping word boxes.

Tesseract is worse than the hosted OCR on this material, which is not the point.
It is a second, independent reader, so where the two disagree about a word the
image itself can settle the question instead of a language prior. It also
reports a bounding box per word, which the hosted OCR was not asked for, and
those boxes are what let a single word be cropped and re-read.

    python3 pipeline/ocr_tesseract.py sources/taygetos-balcony

Writes tesseract/NNNN.tsv (word, confidence, box) and tesseract/NNNN.txt.
Requires: tesseract with the 'ell' language data.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import os
import pathlib
import subprocess
import sys


def read_page(job: tuple[pathlib.Path, pathlib.Path, str, int]) -> tuple[str, int]:
    image, out_dir, lang, psm = job
    stem = image.stem
    prefix = out_dir / stem
    # Tesseract parallelises internally with OpenMP, which deadlocks when many
    # instances run at once. One thread each is both the documented fix and
    # faster here, since the parallelism is already one process per page.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    subprocess.run(
        ["tesseract", str(image), str(prefix), "-l", lang, "--psm", str(psm), "tsv", "txt"],
        check=True,
        capture_output=True,
        env=env,
    )
    tsv = prefix.with_suffix(".tsv")
    words = 0
    if tsv.exists():
        with tsv.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                if (row.get("text") or "").strip():
                    words += 1
    return stem, words


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--lang", default="ell")
    ap.add_argument("--psm", type=int, default=3, help="3 = automatic page segmentation")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not subprocess.run(["which", "tesseract"], capture_output=True).stdout:
        sys.exit("tesseract not found. Install tesseract-ocr and tesseract-ocr-ell.")

    src = args.source_dir
    pages_dir = src / "pages"
    out_dir = src / "tesseract"
    out_dir.mkdir(parents=True, exist_ok=True)

    images = sorted(pages_dir.glob("[0-9]*.png"))
    todo = [p for p in images if args.force or not (out_dir / f"{p.stem}.tsv").exists()]
    print(f"{len(images)} page images, {len(todo)} to read, {args.workers} workers", flush=True)
    if not todo:
        return

    jobs = [(p, out_dir, args.lang, args.psm) for p in todo]
    done = total_words = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for stem, words in pool.map(read_page, jobs):
            done += 1
            total_words += words
            if done % 20 == 0 or done == len(todo):
                print(f"[{done}/{len(todo)}] {stem}  {words} words", flush=True)
    print(f"\n{total_words:,} words read by tesseract")


if __name__ == "__main__":
    main()

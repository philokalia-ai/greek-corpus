#!/usr/bin/env python3
"""Render PDF pages to images, splitting two-page spreads into single pages.

The bound archive was scanned a sheet at a time, so a landscape PDF page holds
two newspaper pages side by side. Feeding a whole spread to an OCR model fails
badly: at ~53 megapixels the service downsamples it below legibility and the
model drifts into hallucinated text and repetition loops. Splitting each spread
at the gutter and cropping to content keeps every page at a legible scale.

    python3 pipeline/prepare_pages.py sources/taygetos-balcony

Writes pages/NNNN[ab].png plus pages/index.json describing the mapping.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import tempfile

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
INK_THRESHOLD = 160  # 8-bit grey below this counts as ink

# Rendering DPI. A measured sweep of OCR accuracy against input size was flat
# from native 300 dpi down to a ~1200 px long edge, so the scans carry far more
# resolution than the OCR consumes. 200 dpi leaves a wide margin over that floor
# while rendering roughly twice as fast.
DEFAULT_DPI = 200


def page_geometry(pdf: pathlib.Path) -> dict[int, tuple[float, float]]:
    out = subprocess.run(
        ["pdfinfo", "-f", "1", "-l", "100000", str(pdf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    geo = {}
    for m in re.finditer(r"Page\s+(\d+)\s+size:\s+([\d.]+) x ([\d.]+)", out):
        geo[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    return geo


def render(pdf: pathlib.Path, page: int, tmp: pathlib.Path, dpi: int) -> Image.Image:
    prefix = tmp / f"r{page}"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-gray", "-png", "-f", str(page), "-l", str(page),
         str(pdf), str(prefix)],
        check=True,
        capture_output=True,
    )
    hits = sorted(tmp.glob(f"r{page}-*.png"))
    if not hits:
        raise RuntimeError(f"pdftoppm produced nothing for page {page}")
    return Image.open(hits[0]).convert("L")


def ink_profile(img: Image.Image) -> np.ndarray:
    """Per-column count of dark pixels."""
    arr = np.asarray(img)
    return (arr < INK_THRESHOLD).sum(axis=0)


def content_box(img: Image.Image, pad: int = 40) -> tuple[int, int, int, int]:
    """Bounding box of inked pixels, padded, so scanner margins are dropped."""
    arr = np.asarray(img) < INK_THRESHOLD
    cols = np.where(arr.sum(axis=0) > arr.shape[0] * 0.002)[0]
    rows = np.where(arr.sum(axis=1) > arr.shape[1] * 0.002)[0]
    if cols.size == 0 or rows.size == 0:
        return (0, 0, img.width, img.height)
    return (
        max(0, int(cols[0]) - pad),
        max(0, int(rows[0]) - pad),
        min(img.width, int(cols[-1]) + pad),
        min(img.height, int(rows[-1]) + pad),
    )


def find_gutter(img: Image.Image, box: tuple[int, int, int, int]) -> int:
    """Absolute x of the blank vertical channel between the two pages.

    Scans the middle of the content for the longest low-ink run; that channel is
    the fold. Falls back to the midpoint when no clear run exists.
    """
    left, _, right, _ = box
    profile = ink_profile(img).astype(float)
    width = right - left
    centre = (left + right) // 2

    # Both halves are the same printed page, so the fold sits close to the middle.
    # Searching wider than this lets a broad white column inside one of the pages
    # win over the real fold, which silently steals text from the other page.
    lo = left + int(width * 0.40)
    hi = left + int(width * 0.60)
    window = profile[lo:hi]
    if window.size == 0:
        return centre

    # A column is "blank" if it carries far less ink than a typical text column.
    inked = profile[left:right][profile[left:right] > 0]
    typical = float(np.median(inked)) if inked.size else 1.0
    blank = window < max(typical * 0.06, img.height * 0.004)

    runs: list[tuple[int, int]] = []
    run_start = None
    for i, is_blank in enumerate(blank):
        if is_blank and run_start is None:
            run_start = i
        elif not is_blank and run_start is not None:
            runs.append((run_start, i - run_start))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(blank) - run_start))

    candidates = [(start, length) for start, length in runs if length >= 8]
    if not candidates:
        return centre
    # Of the plausible blank channels, take the one nearest the middle.
    start, length = min(candidates, key=lambda r: abs(lo + r[0] + r[1] // 2 - centre))
    return lo + start + length // 2


def save(img: Image.Image, box: tuple[int, int, int, int], dest: pathlib.Path) -> dict:
    crop = img.crop(box)
    # These are intermediate artefacts, so favour encode speed over file size.
    crop.save(dest, compress_level=1)
    return {"file": dest.name, "width": crop.width, "height": crop.height,
            "megapixels": round(crop.width * crop.height / 1e6, 1)}


def process_page(job: tuple) -> tuple[str, dict]:
    """Render one PDF page and write its one or two page images."""
    pdf, page, is_spread, out_dir, dpi = job
    key = f"{page:04d}"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        img = render(pdf, page, tmp, dpi)
        box = content_box(img)
        if is_spread:
            gutter = find_gutter(img, box)
            parts = [
                save(img, (box[0], box[1], gutter, box[3]), out_dir / f"{key}a.png"),
                save(img, (gutter, box[1], box[2], box[3]), out_dir / f"{key}b.png"),
            ]
            entry = {"pdf_page": page, "layout": "spread",
                     "gutter_x": int(gutter), "dpi": dpi, "parts": parts}
        else:
            parts = [save(img, box, out_dir / f"{key}a.png")]
            entry = {"pdf_page": page, "layout": "single", "dpi": dpi, "parts": parts}
    return key, entry


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    args = ap.parse_args()

    manifest = json.loads((args.source_dir / "manifest.json").read_text(encoding="utf-8"))
    pdf = pathlib.Path(args.pdf) if args.pdf else (
        args.source_dir / "raw" / manifest["scans"][0]["filename"]
    )
    out_dir = args.source_dir / "pages"
    out_dir.mkdir(parents=True, exist_ok=True)

    geo = page_geometry(pdf)
    last = args.last or max(geo)
    index_path = out_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}

    jobs = [
        (pdf, page, geo[page][0] > geo[page][1], out_dir, args.dpi)
        for page in range(args.first, last + 1)
        if args.force or f"{page:04d}" not in index
    ]
    print(f"{len(jobs)} pages to render at {args.dpi} dpi, {args.workers} workers", flush=True)

    done = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for key, entry in pool.map(process_page, jobs):
            index[key] = entry
            done += 1
            sizes = ", ".join(f"{p['width']}x{p['height']}" for p in entry["parts"])
            print(f"[{done}/{len(jobs)}] page {entry['pdf_page']:4d}  "
                  f"{entry['layout']:6s}  {sizes}", flush=True)
            index_path.write_text(
                json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
            )

    spreads = sum(1 for v in index.values() if v["layout"] == "spread")
    images = sum(len(v["parts"]) for v in index.values())
    print(f"\n{len(index)} PDF pages -> {images} page images ({spreads} spreads split)")


if __name__ == "__main__":
    main()

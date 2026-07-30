#!/usr/bin/env python3
"""Settle a disputed word by cropping it out of the page and reading it alone.

Where neither the lexicon nor the second reader can decide, the only remaining
authority is the scan. Tesseract reports a box per word, so the word can be cut
out and submitted on its own. That is the same effect that fixed the mastheads:
a small image spends the whole of the OCR's pixel budget on the few glyphs in
question instead of sharing it with a full broadsheet.

    MISTRAL_API_KEY=... python3 pipeline/verify_words.py sources/taygetos-balcony

Reads gazetteer/arbitration.tsv, writes gazetteer/verified.tsv. Applies nothing;
apply_fixes.py consumes the verdicts.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import io
import os
import pathlib
import sys
import threading
import time

import requests
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from greek_text import GREEK_WORD, confusion_distance, fold  # noqa: E402

Image.MAX_IMAGE_PIXELS = None
API = "https://api.mistral.ai/v1/ocr"
MODEL = "mistral-ocr-latest"
PAD = 12
UPSCALE = 4

_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


def load_key(env_file: str | None) -> str:
    key = os.environ.get("MISTRAL_API_KEY")
    if key:
        return key.strip()
    candidate = env_file or os.environ.get("MISTRAL_ENV_FILE")
    if candidate:
        path = pathlib.Path(candidate).expanduser()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("MISTRAL_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("No Mistral API key. Set MISTRAL_API_KEY or pass --env-file.")


def word_boxes(tsv: pathlib.Path) -> list[dict]:
    out = []
    with tsv.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            text = (row.get("text") or "").strip()
            if not text or not GREEK_WORD.search(text):
                continue
            try:
                out.append({
                    "text": fold(GREEK_WORD.search(text).group(0)),
                    "left": int(row["left"]), "top": int(row["top"]),
                    "width": int(row["width"]), "height": int(row["height"]),
                    "conf": float(row.get("conf", -1) or -1),
                })
            except (ValueError, KeyError):
                continue
    return out


def locate(boxes: list[dict], suspect: str, candidate: str) -> dict | None:
    """The box most likely to hold the disputed word.

    The second reader may have misread it too, so an exact match is not required;
    the nearest spelling under the confusion metric is close enough to crop.
    """
    best, best_score = None, 3.0
    for box in boxes:
        score = min(
            confusion_distance(box["text"], suspect),
            confusion_distance(box["text"], candidate),
        )
        if score < best_score:
            best, best_score = box, score
    return best


def read_crop(image: Image.Image, box: dict, key: str) -> str:
    left = max(0, box["left"] - PAD)
    top = max(0, box["top"] - PAD)
    right = min(image.width, box["left"] + box["width"] + PAD)
    bottom = min(image.height, box["top"] + box["height"] + PAD)
    crop = image.crop((left, top, right, bottom))
    crop = crop.resize((crop.width * UPSCALE, crop.height * UPSCALE), Image.LANCZOS)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    delay = 4.0
    for attempt in range(5):
        try:
            resp = requests.post(
                API,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "document": {"type": "image_url", "image_url": f"data:image/png;base64,{b64}"},
                    "include_image_base64": False,
                },
                timeout=300,
            )
            if resp.status_code < 300:
                text = "\n".join(p.get("markdown", "") for p in resp.json().get("pages", []))
                match = GREEK_WORD.search(text)
                return fold(match.group(0)) if match else ""
            if resp.status_code not in (429, 500, 502, 503, 504):
                return ""
        except requests.RequestException:
            pass
        if attempt == 4:
            return ""
        time.sleep(delay)
        delay *= 2
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--verdicts", default="both-seen,unseen",
                    help="which arbitration verdicts to re-read from the image")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    key = load_key(args.env_file)
    src = args.source_dir
    gaz = src / "gazetteer"
    wanted = {v.strip() for v in args.verdicts.split(",")}

    with (gaz / "arbitration.tsv").open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh, delimiter="\t") if r["verdict"] in wanted]
    if args.limit:
        rows = rows[: args.limit]

    out_path = gaz / "verified.tsv"
    already = set()
    if out_path.exists():
        with out_path.open(encoding="utf-8") as fh:
            already = {(r["suspect"], r["candidate"]) for r in csv.DictReader(fh, delimiter="\t")}
    rows = [r for r in rows if (r["suspect"], r["candidate"]) not in already]

    log(f"{len(rows)} disputed words to re-read from the image, {args.workers} workers")
    if not rows:
        return

    boxes_cache: dict[str, list[dict]] = {}
    cache_lock = threading.Lock()

    def boxes_for(page: str) -> list[dict]:
        with cache_lock:
            if page not in boxes_cache:
                tsv = src / "tesseract" / f"{page}.tsv"
                boxes_cache[page] = word_boxes(tsv) if tsv.exists() else []
            return boxes_cache[page]

    def work(row: dict) -> dict | None:
        page = row["pages"].split(",")[0]
        if not page:
            return None
        boxes = boxes_for(page)
        box = locate(boxes, row["suspect"], row["candidate"])
        image_path = src / "pages" / f"{page}.png"
        if not box or not image_path.exists():
            return {**row, "page_used": page, "image_read": "", "resolution": "not-located"}
        read = read_crop(Image.open(image_path).convert("L"), box, key)
        if read == row["suspect"]:
            resolution = "suspect"
        elif read == row["candidate"]:
            resolution = "candidate"
        elif not read:
            resolution = "unreadable"
        else:
            resolution = "third-reading"
        return {**row, "page_used": page, "image_read": read, "resolution": resolution}

    results = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for outcome in pool.map(work, rows):
            done += 1
            if outcome is None:
                continue
            results.append(outcome)
            if outcome["resolution"] in ("candidate", "suspect"):
                log(f"[{done}/{len(rows)}] {outcome['suspect']} vs {outcome['candidate']}"
                    f" -> image says {outcome['resolution']} ({outcome['image_read']})")

    fields = ["suspect", "candidate", "pages", "second_saw_suspect", "second_saw_candidate",
              "verdict", "prior_confidence", "page_used", "image_read", "resolution"]
    write_header = not out_path.exists()
    with out_path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fields, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(results)

    counts: dict[str, int] = {}
    for row in results:
        counts[row["resolution"]] = counts.get(row["resolution"], 0) + 1
    log(f"\n{counts}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Re-read every page's masthead band at high resolution.

Whole-page OCR reads the body text well but often garbles the masthead, whose
display type and rules confuse it. That loses two things at once: which pages
start an issue, and the issue number and date printed on them. Cropping the top
band and OCR'ing it alone gives the model a small image, so the dateline lands
at full effective resolution.

    MISTRAL_API_KEY=... python3 pipeline/read_mastheads.py sources/taygetos-balcony

Writes pages/mastheads.json. build_issues.py prefers this over the page text.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

import requests
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
API = "https://api.mistral.ai/v1"
MODEL = "mistral-ocr-latest"
BAND = 0.20  # top fraction of the page that holds the masthead
CROP_DPI = 300

_lock = threading.Lock()

MONTHS = {
    "ΙΑΝΟΥΑΡ": 1, "ΦΕΒΡΟΥΑΡ": 2, "ΜΑΡΤ": 3, "ΑΠΡΙΛ": 4, "ΜΑ": 5,
    "ΙΟΥΝ": 6, "ΙΟΥΛ": 7, "ΑΥΓΟΥΣΤ": 8, "ΣΕΠΤΕΜΒΡ": 9,
    "ΟΚΤΩΒΡ": 10, "ΝΟΕΜΒΡ": 11, "ΔΕΚΕΜΒΡ": 12,
}
MONTH_ORDER = sorted(MONTHS, key=len, reverse=True)
MASTHEAD_TOKENS = ("ΜΠΑΛΚΟΝΙ", "ΤΑΥΓΕΤΟΥ", "ΓΕΩΡΓΙΤΣΙΑΝΩΝ", "ΓΕΩΡΓΙΤΣΙ", "ΣΥΝΔΕΣΕΩΣ")


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


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.upper().replace("Ϊ", "Ι").replace("Ϋ", "Υ")


def parse(text: str) -> dict:
    norm = normalize(text)
    hits = sum(1 for token in MASTHEAD_TOKENS if token in norm)

    issue_no = None
    m = re.search(r"ΦΥΛΛΟΥ?\s*[:.]?\s*(\d{1,3})", norm)
    if m:
        issue_no = int(m.group(1))

    volume = None
    m = re.search(r"ΕΤΟΣ\s*(\d{1,2})", norm)
    if m:
        volume = int(m.group(1))

    month = None
    best = None
    for stem in MONTH_ORDER:
        idx = norm.find(stem)
        if idx != -1 and (best is None or idx < best):
            best, month = idx, MONTHS[stem]

    year = None
    for m in re.finditer(r"\b(19[6-9]\d)\b", norm):
        candidate = int(m.group(1))
        if 1975 <= candidate <= 1995:
            year = candidate
            break

    return {
        "masthead_hits": hits,
        "is_front": hits >= 2 and ("ΦΥΛΛΟ" in norm or "ΕΤΟΣ" in norm),
        "issue_number": issue_no,
        "volume_year": volume,
        "month": month,
        "year": year,
        "text": text.strip()[:400],
    }


def crop_band(source_dir: pathlib.Path, stem: str, entry: dict) -> bytes:
    """Re-render the page's top band from the PDF at full resolution."""
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    pdf = source_dir / "raw" / manifest["scans"][0]["filename"]
    page = entry["pdf_page"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        subprocess.run(
            ["pdftoppm", "-r", str(CROP_DPI), "-gray", "-png",
             "-f", str(page), "-l", str(page), str(pdf), str(tmp / "p")],
            check=True, capture_output=True,
        )
        rendered = sorted(tmp.glob("p-*.png"))[0]
        img = Image.open(rendered).convert("L")

        if entry["layout"] == "spread":
            # index.json records the gutter in the coordinates of its own render.
            scale = CROP_DPI / entry.get("dpi", CROP_DPI)
            gutter = int(entry["gutter_x"] * scale)
            box = (0, 0, gutter, img.height) if stem.endswith("a") else (gutter, 0, img.width, img.height)
            img = img.crop(box)

        band = img.crop((0, 0, img.width, int(img.height * BAND)))

    buf = io.BytesIO()
    band.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def ocr_band(payload: bytes, key: str) -> str:
    b64 = base64.b64encode(payload).decode()
    delay = 5.0
    for attempt in range(6):
        try:
            resp = requests.post(
                f"{API}/ocr",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "document": {"type": "image_url", "image_url": f"data:image/png;base64,{b64}"},
                    "include_image_base64": False,
                },
                timeout=900,
            )
            if resp.status_code < 300:
                return "\n".join(p.get("markdown", "") for p in resp.json().get("pages", []))
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException:
            pass
        if attempt == 5:
            raise RuntimeError("giving up after retries")
        time.sleep(delay)
        delay *= 2
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default=None, help="comma-separated page stems")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    key = load_key(args.env_file)
    src = args.source_dir
    index = json.loads((src / "pages" / "index.json").read_text(encoding="utf-8"))

    out_path = src / "pages" / "mastheads.json"
    results = {}
    if out_path.exists() and not args.force:
        results = json.loads(out_path.read_text(encoding="utf-8"))

    targets = []
    for key_page in sorted(index, key=int):
        for part in index[key_page]["parts"]:
            stem = pathlib.Path(part["file"]).stem
            if args.only and stem not in {s.strip() for s in args.only.split(",")}:
                continue
            if stem in results and not args.force:
                continue
            targets.append((stem, index[key_page]))

    log(f"{len(targets)} masthead bands to read, {args.workers} workers")
    if not targets:
        return

    def work(item):
        stem, entry = item
        payload = crop_band(src, stem, entry)
        return stem, parse(ocr_band(payload, key))

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, item): item[0] for item in targets}
        for fut in concurrent.futures.as_completed(futures):
            stem = futures[fut]
            done += 1
            try:
                stem, parsed = fut.result()
                results[stem] = parsed
                if parsed["is_front"]:
                    log(f"[{done}/{len(targets)}] {stem}  FRONT  "
                        f"issue={parsed['issue_number']} {parsed['year']}-{parsed['month']}")
            except Exception as exc:  # noqa: BLE001
                log(f"[{done}/{len(targets)}] {stem}  failed: {exc}")
            if done % 20 == 0:
                out_path.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )

    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    fronts = [k for k, v in results.items() if v["is_front"]]
    dated = [k for k in fronts if results[k]["year"] and results[k]["month"]]
    log(f"\n{len(fronts)} front pages, {len(dated)} with a full date")


if __name__ == "__main__":
    main()

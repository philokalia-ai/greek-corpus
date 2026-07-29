#!/usr/bin/env python3
"""OCR prepared page images with Mistral, scoring each result.

Every page is scored on two signals that catch the ways a vision OCR model fails
on poor input: drifting out of Greek into another script, and collapsing into a
repetition loop. Pages below threshold are recorded in ocr/quality.json so they
can be re-run or corrected rather than silently entering the corpus.

    MISTRAL_API_KEY=... python3 pipeline/ocr_pages.py sources/taygetos-balcony

The key is read from MISTRAL_API_KEY or --env-file. It is never logged or written.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import json
import os
import pathlib
import sys
import threading
import time
import unicodedata

import requests
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
API = "https://api.mistral.ai/v1"
MODEL = "mistral-ocr-latest"

MIN_GREEK_RATIO = 0.80
MAX_DUPLICATE_RATIO = 0.25
MIN_CHARS = 400

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


def greek_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    greek = sum(1 for c in letters if "GREEK" in unicodedata.name(c, ""))
    return greek / len(letters)


def duplicate_ratio(text: str) -> float:
    """Share of substantial lines that are exact repeats of an earlier line."""
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 25]
    if not lines:
        return 0.0
    return 1 - len(set(lines)) / len(lines)


def score(text: str) -> dict:
    metrics = {
        "chars": len(text),
        "greek_ratio": round(greek_ratio(text), 3),
        "duplicate_ratio": round(duplicate_ratio(text), 3),
    }
    metrics["ok"] = (
        metrics["chars"] >= MIN_CHARS
        and metrics["greek_ratio"] >= MIN_GREEK_RATIO
        and metrics["duplicate_ratio"] <= MAX_DUPLICATE_RATIO
    )
    return metrics


def encode(path: pathlib.Path, max_edge: int | None) -> str:
    img = Image.open(path).convert("L")
    if max_edge and max(img.size) > max_edge:
        scale = max_edge / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def ocr_image(path: pathlib.Path, out_dir: pathlib.Path, key: str, max_edge: int | None) -> dict:
    b64 = encode(path, max_edge)
    delay = 5.0
    last = None
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
                timeout=1800,
            )
            if resp.status_code < 300:
                break
            if resp.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status_code}"
            else:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        except requests.RequestException as exc:
            last = repr(exc)
        if attempt == 5:
            raise RuntimeError(f"giving up: {last}")
        time.sleep(delay)
        delay *= 2

    data = resp.json()
    markdown = "\n\n".join(p.get("markdown", "") for p in data.get("pages", []))
    (out_dir / f"{path.stem}.md").write_text(markdown, encoding="utf-8")
    metrics = score(markdown)
    metrics["image"] = path.name
    metrics["model"] = data.get("model", MODEL)
    (out_dir / f"{path.stem}.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-edge", type=int, default=0,
                    help="downscale long edge before upload; 0 keeps native size")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default=None, help="comma-separated page stems, e.g. 0003a,0004b")
    args = ap.parse_args()

    key = load_key(args.env_file)
    pages_dir = args.source_dir / "pages"
    out_dir = args.source_dir / "ocr"
    out_dir.mkdir(parents=True, exist_ok=True)

    images = sorted(pages_dir.glob("[0-9]*.png"))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        images = [p for p in images if p.stem in wanted]
    todo = [p for p in images if args.force or not (out_dir / f"{p.stem}.md").exists()]

    log(f"{len(images)} page images, {len(todo)} to OCR, {args.workers} workers")
    if not todo:
        return

    results: dict[str, dict] = {}
    failures: list[str] = []
    done = 0
    max_edge = args.max_edge or None

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(ocr_image, p, out_dir, key, max_edge): p for p in todo}
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            done += 1
            try:
                metrics = fut.result()
                results[path.stem] = metrics
                flag = "" if metrics["ok"] else "   <-- LOW QUALITY"
                log(
                    f"[{done}/{len(todo)}] {path.stem}  {metrics['chars']:6d} chars  "
                    f"greek {metrics['greek_ratio']:.2f}  dup {metrics['duplicate_ratio']:.2f}{flag}"
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(path.stem)
                log(f"[{done}/{len(todo)}] {path.stem}  FAILED: {exc}")

    # Merge into the quality report so partial runs accumulate.
    report_path = out_dir / "quality.json"
    report = {}
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(results)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    low = sorted(k for k, v in report.items() if not v.get("ok"))
    log(f"\n{len(report)} pages scored, {len(low)} below quality threshold")
    if low:
        log(f"low quality: {', '.join(low[:30])}{' ...' if len(low) > 30 else ''}")
    if failures:
        log(f"{len(failures)} pages errored: {', '.join(failures)}")
        sys.exit(1)


if __name__ == "__main__":
    main()

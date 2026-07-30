#!/usr/bin/env python3
"""Repair OCR damage with a small chat model, under validation.

The OCR's characteristic failure is mechanical rather than semantic: Greek read
as visually similar Latin letters, words split across column breaks, lost
accents. A small model fixes that cheaply. It will also, left alone, quietly
drop digits and rewrite phrasing, which is worse for an archive than the damage
it repairs.

So every page is checked before its correction is accepted:

  * the Greek-letter ratio must not fall
  * the length must stay within a tolerance
  * multi-digit numbers must survive

A page failing any check keeps its raw text and is recorded as rejected. Raw OCR
is never overwritten, so every correction stays visible as a diff.

    MISTRAL_API_KEY=... python3 pipeline/correct_ocr.py sources/taygetos-balcony
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
import pathlib
import re
import sys
import threading
import time
import unicodedata

import requests

API = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL = "mistral-small-latest"

MAX_LENGTH_DELTA = 0.15
MIN_NUMBER_RETENTION = 0.95

_lock = threading.Lock()

SYSTEM = """Είσαι επιμελητής κειμένου που διορθώνει σφάλματα OCR σε ελληνικές εφημερίδες του 1977-1988.

Το κείμενο προέρχεται από σάρωση εφημερίδας με στήλες. Θα διαβαστεί σε ιστοσελίδα, όχι σε στήλες.

Κανόνες:
1. Διόρθωσε ΜΟΝΟ σφάλματα οπτικής αναγνώρισης: λατινικά γράμματα στη θέση οπτικά όμοιων ελληνικών, χαμένους τόνους, κολλημένες λέξεις.
2. ΕΝΩΣΕ τις λέξεις που έχουν κοπεί με ενωτικό στο τέλος της γραμμής ή της στήλης: «δια-\nκοπή» γίνεται «διακοπή». Αυτά τα ενωτικά είναι τυπογραφικά, δεν ανήκουν στο κείμενο.
3. ΚΡΑΤΗΣΕ τα ενωτικά που ανήκουν πραγματικά στη λέξη ή στη φράση (π.χ. «Ελληνο-Αμερικανικός», παύλες διαλόγου).
4. Ένωσε προτάσεις που έχουν σπάσει σε πολλές γραμμές λόγω της στοίχισης σε στήλες, ώστε κάθε παράγραφος να είναι ενιαία.
5. ΜΗΝ ξαναγράψεις, ΜΗΝ συντομεύσεις, ΜΗΝ βελτιώσεις το ύφος. Δεν είσαι συντάκτης.
6. ΜΗΝ αλλάξεις ονόματα, αριθμούς, ημερομηνίες, ποσά. Αν ένα όνομα φαίνεται λάθος, άφησέ το.
7. ΜΗΝ προσθέσεις και ΜΗΝ αφαιρέσεις προτάσεις. Αν κάτι είναι δυσανάγνωστο, άφησέ το όπως είναι.
8. Κράτησε την ορθογραφία της εποχής όπου φαίνεται σκόπιμη.
9. Διατήρησε τη δομή Markdown (επικεφαλίδες με #).
10. Επίστρεψε ΜΟΝΟ το διορθωμένο κείμενο, χωρίς σχόλια."""


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


HYPHEN_BREAK = re.compile(r"(\w+)-[ \t]*\n[ \t]*(\w+)")


def dehyphenate(text: str) -> str:
    """Rejoin words the typesetter split across a line or column break.

    Done in code rather than left to the model: the rule is mechanical, and
    asking for it in the prompt only got about half of them. A hyphen before a
    capital is kept, since that is a real compound (Ελληνο-Αμερικανικός) that
    happens to fall at a line end rather than a syllable break.
    """
    previous = None
    while previous != text:
        previous = text
        text = HYPHEN_BREAK.sub(
            lambda m: f"{m.group(1)}-{m.group(2)}" if m.group(2)[:1].isupper()
            else m.group(1) + m.group(2),
            text,
        )
    return text


def greek_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "GREEK" in unicodedata.name(c, "")) / len(letters)


def significant_numbers(text: str) -> collections.Counter:
    """Multi-digit runs only; stray single digits are usually OCR noise."""
    return collections.Counter(re.findall(r"\d{2,}", text))


def validate(raw: str, fixed: str) -> tuple[bool, dict]:
    before, after = significant_numbers(raw), significant_numbers(fixed)
    total = sum(before.values())
    kept = sum((before & after).values())
    retention = kept / total if total else 1.0
    delta = (len(fixed) - len(raw)) / max(1, len(raw))
    g_before, g_after = greek_ratio(raw), greek_ratio(fixed)

    checks = {
        "greek_not_worse": g_after >= g_before - 0.01,
        "length_ok": abs(delta) <= MAX_LENGTH_DELTA,
        "numbers_kept": retention >= MIN_NUMBER_RETENTION,
        "not_empty": len(fixed.strip()) > 0,
    }
    metrics = {
        "greek_before": round(g_before, 3),
        "greek_after": round(g_after, 3),
        "length_delta": round(delta, 3),
        "number_retention": round(retention, 3),
        "numbers_total": total,
        "checks": checks,
    }
    return all(checks.values()), metrics


def correct_page(path: pathlib.Path, out_dir: pathlib.Path, key: str, model: str) -> dict:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {"page": path.stem, "status": "empty"}
    # Fix what can be fixed deterministically before spending a model call on it.
    prepared = dehyphenate(raw)

    delay = 5.0
    resp = None
    for attempt in range(5):
        try:
            resp = requests.post(
                API,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0.0,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prepared},
                    ],
                },
                timeout=900,
            )
            if resp.status_code < 300:
                break
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as exc:
            if attempt == 4:
                raise RuntimeError(repr(exc)) from exc
        if attempt == 4:
            raise RuntimeError("giving up after retries")
        time.sleep(delay)
        delay *= 2

    payload = resp.json()
    fixed = payload["choices"][0]["message"]["content"].strip()
    ok, metrics = validate(prepared, fixed)

    record = {
        "page": path.stem,
        "status": "accepted" if ok else "rejected",
        "model": model,
        "usage": payload.get("usage", {}),
        **metrics,
    }
    if ok:
        (out_dir / f"{path.stem}.md").write_text(fixed, encoding="utf-8")
    else:
        # Keep the rejected attempt so the gate can be reviewed against it,
        # somewhere the corpus build will not pick it up.
        quarantine = out_dir / "rejected"
        quarantine.mkdir(exist_ok=True)
        (quarantine / f"{path.stem}.md").write_text(fixed, encoding="utf-8")
        record["lost_numbers"] = sorted(
            (significant_numbers(prepared) - significant_numbers(fixed)).elements()
        )[:40]
    return record


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only-flagged", action="store_true",
                    help="correct only pages the OCR quality gate flagged")
    ap.add_argument("--retry-rejected", action="store_true",
                    help="re-run only the pages whose previous correction was rejected")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    key = load_key(args.env_file)
    src = args.source_dir
    ocr_dir = src / "ocr"
    out_dir = src / "corrected"
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = sorted(ocr_dir.glob("[0-9]*.md"))
    if args.only_flagged:
        flagged = set()
        for score in ocr_dir.glob("[0-9]*.json"):
            try:
                if not json.loads(score.read_text(encoding="utf-8")).get("ok", True):
                    flagged.add(score.stem)
            except json.JSONDecodeError:
                continue
        pages = [p for p in pages if p.stem in flagged]

    report_path = out_dir / "report.json"
    report = {}
    if report_path.exists() and not args.force:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    if args.retry_rejected:
        retry = {k for k, v in report.items() if v.get("status") == "rejected"}
        todo = [p for p in pages if p.stem in retry]
    else:
        todo = [p for p in pages if args.force or p.stem not in report]

    log(f"{len(pages)} pages in scope, {len(todo)} to correct with {args.model}")
    if not todo:
        return

    done = accepted = rejected = 0
    tokens_in = tokens_out = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(correct_page, p, out_dir, key, args.model): p for p in todo}
        for fut in concurrent.futures.as_completed(futures):
            page = futures[fut]
            done += 1
            try:
                record = fut.result()
            except Exception as exc:  # noqa: BLE001
                log(f"[{done}/{len(todo)}] {page.stem}  FAILED: {exc}")
                continue
            report[page.stem] = record
            usage = record.get("usage") or {}
            tokens_in += usage.get("prompt_tokens", 0)
            tokens_out += usage.get("completion_tokens", 0)
            if record["status"] == "accepted":
                accepted += 1
                if record.get("greek_after", 0) - record.get("greek_before", 0) > 0.02:
                    log(f"[{done}/{len(todo)}] {page.stem}  greek "
                        f"{record['greek_before']:.2f} -> {record['greek_after']:.2f}")
            elif record["status"] == "rejected":
                rejected += 1
                failed = [k for k, v in record["checks"].items() if not v]
                log(f"[{done}/{len(todo)}] {page.stem}  REJECTED ({', '.join(failed)})")
            if done % 5 == 0:
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    log(f"\naccepted {accepted}, rejected {rejected}")
    log(f"tokens: {tokens_in:,} in, {tokens_out:,} out")


if __name__ == "__main__":
    main()

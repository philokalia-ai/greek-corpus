#!/usr/bin/env python3
"""Download the source scans listed in a source's manifest.json.

Scans are large binaries and are deliberately not committed. This script
re-fetches them so the pipeline can be reproduced from a clean clone:

    python3 pipeline/fetch_source.py sources/taygetos-balcony
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

import requests

DRIVE = "https://drive.usercontent.google.com/download"
CHUNK = 1 << 20


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=1800) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        seen = 0
        with dest.open("wb") as fh:
            for block in resp.iter_content(CHUNK):
                fh.write(block)
                seen += len(block)
                if total:
                    print(f"\r  {seen / 1e6:7.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    args = ap.parse_args()

    manifest = json.loads((args.source_dir / "manifest.json").read_text(encoding="utf-8"))
    failed = False

    for item in manifest["scans"]:
        dest = args.source_dir / "raw" / item["filename"]
        if dest.exists():
            print(f"{item['filename']}: already present")
        else:
            url = item.get("download_url") or DRIVE
            if "drive_file_id" in item:
                url = f"{DRIVE}?id={item['drive_file_id']}&export=download&confirm=t"
            print(f"{item['filename']}: downloading")
            download(url, dest)

        expected = item.get("sha256")
        if expected:
            actual = sha256(dest)
            if actual != expected:
                print(f"  CHECKSUM MISMATCH\n    expected {expected}\n    actual   {actual}")
                failed = True
            else:
                print(f"  sha256 ok")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

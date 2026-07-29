#!/usr/bin/env python3
"""Publish exported posts to WordPress over the REST API, backdated.

Defaults are deliberately cautious: it does nothing without --run, it skips
articles the OCR flagged and articles with no confident date unless told
otherwise, and it never creates a second copy of a post it has already created.

    # see what would happen
    python3 pipeline/publish_wordpress.py sources/taygetos-balcony

    # do it
    python3 pipeline/publish_wordpress.py sources/taygetos-balcony --run --limit 5

Credentials come from the environment or --env-file, never from the command
line (a password in argv is visible to every process on the machine):

    WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD

WP_APP_PASSWORD must be a WordPress Application Password, not the account
password. Revoke it when the backfill is finished.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import requests


def load_env(env_file: str | None) -> dict:
    values = {k: os.environ.get(k, "") for k in ("WP_SITE_URL", "WP_USERNAME", "WP_APP_PASSWORD")}
    candidate = env_file or os.environ.get("WP_ENV_FILE")
    if candidate:
        path = pathlib.Path(candidate).expanduser()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in values and not values[key]:
                    values[key] = value.strip().strip('"').strip("'")
    missing = [k for k, v in values.items() if not v]
    if missing:
        sys.exit(
            "Missing credentials: " + ", ".join(missing) + "\n"
            "Set them in the environment or pass --env-file pointing outside the repo."
        )
    values["WP_SITE_URL"] = values["WP_SITE_URL"].rstrip("/")
    return values


def api(env: dict, path: str) -> str:
    return f"{env['WP_SITE_URL']}/wp-json/wp/v2/{path}"


def ensure_category(session: requests.Session, env: dict, name: str) -> int | None:
    resp = session.get(api(env, "categories"), params={"search": name, "per_page": 100}, timeout=60)
    resp.raise_for_status()
    for item in resp.json():
        if item["name"].strip().lower() == name.strip().lower():
            return item["id"]
    resp = session.post(api(env, "categories"), json={"name": name}, timeout=60)
    if resp.status_code >= 300:
        print(f"  could not create category {name!r}: {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json()["id"]


def find_existing(session: requests.Session, env: dict, slug: str) -> dict | None:
    resp = session.get(
        api(env, "posts"), params={"slug": slug, "status": "any", "per_page": 1}, timeout=60
    )
    if resp.status_code >= 300:
        return None
    items = resp.json()
    return items[0] if items else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--run", action="store_true", help="actually write to the site")
    ap.add_argument("--limit", type=int, default=0, help="stop after N posts (0 = all)")
    ap.add_argument("--include-flagged", action="store_true",
                    help="also publish articles the OCR quality gate flagged")
    ap.add_argument("--include-undated", action="store_true",
                    help="also publish articles with no confident date")
    ap.add_argument("--status", default=None, help="override the status in posts.json")
    ap.add_argument("--delay", type=float, default=0.4, help="seconds between requests")
    args = ap.parse_args()

    posts_path = args.source_dir / "export" / "wordpress" / "posts.json"
    posts = json.loads(posts_path.read_text(encoding="utf-8"))

    selected = []
    skipped = {"flagged": 0, "undated": 0}
    for post in posts:
        if post["_flags"]["ocr_flagged"] and not args.include_flagged:
            skipped["flagged"] += 1
            continue
        if post["_flags"]["undated"] and not args.include_undated:
            skipped["undated"] += 1
            continue
        selected.append(post)
    if args.limit:
        selected = selected[: args.limit]

    print(f"{len(posts)} exported, {len(selected)} selected for publishing")
    print(f"  skipped {skipped['flagged']} ocr-flagged, {skipped['undated']} undated")

    if not args.run:
        print("\nDry run. Nothing was sent. Re-run with --run to publish.")
        for post in selected[:10]:
            print(f"  {post['date'] or 'undated':<20} {post['title'][:60]}")
        if len(selected) > 10:
            print(f"  ... and {len(selected) - 10} more")
        return

    env = load_env(args.env_file)
    session = requests.Session()
    session.auth = (env["WP_USERNAME"], env["WP_APP_PASSWORD"])
    session.headers["User-Agent"] = "greek-corpus-backfill/1.0"

    me = session.get(api(env, "users/me"), timeout=60)
    if me.status_code >= 300:
        sys.exit(f"Authentication failed: {me.status_code} {me.text[:200]}")
    print(f"authenticated as {me.json().get('name')} on {env['WP_SITE_URL']}")

    category_ids: dict[str, int] = {}
    created = updated = failed = 0

    for n, post in enumerate(selected, start=1):
        name = post["category"]
        if name and name not in category_ids:
            cid = ensure_category(session, env, name)
            if cid:
                category_ids[name] = cid

        payload = {
            "title": post["title"],
            "content": post["content"],
            "excerpt": post["excerpt"],
            "slug": post["slug"],
            "status": args.status or post["status"],
        }
        if post["date"]:
            payload["date"] = post["date"]
        if name in category_ids:
            payload["categories"] = [category_ids[name]]

        existing = find_existing(session, env, post["slug"])
        if existing:
            resp = session.post(api(env, f"posts/{existing['id']}"), json=payload, timeout=120)
            action = "updated"
        else:
            resp = session.post(api(env, "posts"), json=payload, timeout=120)
            action = "created"

        if resp.status_code >= 300:
            failed += 1
            print(f"[{n}/{len(selected)}] FAILED {post['slug']}: "
                  f"{resp.status_code} {resp.text[:160]}")
        else:
            if action == "created":
                created += 1
            else:
                updated += 1
            print(f"[{n}/{len(selected)}] {action} {post['date'][:10] if post['date'] else '????'} "
                  f"{post['title'][:50]}")
        time.sleep(args.delay)

    print(f"\ncreated {created}, updated {updated}, failed {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

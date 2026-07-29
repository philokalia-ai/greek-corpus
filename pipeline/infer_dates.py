#!/usr/bin/env python3
"""Fill in issue dates that the masthead OCR could not read.

The paper numbered its issues consecutively and appeared at a fairly steady
rate, so issue number predicts date well. This fits a monotonic spine through
the issues whose dateline *was* read, discards the ones that contradict it, and
interpolates a date for numbered issues that have none.

Inferred dates are recorded as date_confidence "interpolated" and never
overwrite a date that was actually read off the page. Issues whose masthead date
disagrees with the spine are marked "conflicting" rather than silently corrected.

    python3 pipeline/infer_dates.py sources/taygetos-balcony
"""
from __future__ import annotations

import argparse
import bisect
import json
import pathlib


def to_months(date: str) -> int:
    year, month, _ = date.split("-")
    return int(year) * 12 + (int(month) - 1)


def from_months(value: int) -> str:
    return f"{value // 12:04d}-{value % 12 + 1:02d}-01"


def longest_monotonic(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest subsequence whose dates rise with issue number (patience sort)."""
    if not points:
        return []
    tails: list[int] = []
    tail_idx: list[int] = []
    parent = [-1] * len(points)
    for i, (_, months) in enumerate(points):
        pos = bisect.bisect_right(tails, months)
        if pos == len(tails):
            tails.append(months)
            tail_idx.append(i)
        else:
            tails[pos] = months
            tail_idx[pos] = i
        parent[i] = tail_idx[pos - 1] if pos else -1
    out = []
    node = tail_idx[-1]
    while node != -1:
        out.append(points[node])
        node = parent[node]
    return out[::-1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_dir", type=pathlib.Path)
    args = ap.parse_args()

    path = args.source_dir / "issues" / "issues.json"
    issues = json.loads(path.read_text(encoding="utf-8"))

    # One date per issue number; a number carrying two different dates means one
    # of the two mastheads was misread, so it cannot anchor anything.
    by_number: dict[int, set[int]] = {}
    for issue in issues:
        if issue.get("issue_number") and issue.get("date"):
            by_number.setdefault(issue["issue_number"], set()).add(to_months(issue["date"]))
    anchors = sorted((n, next(iter(v))) for n, v in by_number.items() if len(v) == 1)
    contested = {n for n, v in by_number.items() if len(v) > 1}

    spine = longest_monotonic(anchors)
    spine_numbers = [n for n, _ in spine]
    spine_months = [m for _, m in spine]
    off_spine = {n for n, _ in anchors} - set(spine_numbers)

    print(f"{len(anchors)} issue numbers with a single read date")
    print(f"  {len(spine)} form the monotonic spine")
    if off_spine:
        print(f"  {len(off_spine)} contradict it: {sorted(off_spine)}")
    if contested:
        print(f"  {len(contested)} carry conflicting dates: {sorted(contested)}")

    interpolated = conflicting = 0
    for issue in issues:
        number = issue.get("issue_number")
        if not number:
            continue
        if issue.get("date"):
            if number in contested or number in off_spine:
                issue["date_confidence"] = "conflicting"
                conflicting += 1
            continue
        if len(spine) < 2 or not (spine_numbers[0] <= number <= spine_numbers[-1]):
            continue
        pos = bisect.bisect_left(spine_numbers, number)
        lo_n, lo_m = spine_numbers[pos - 1], spine_months[pos - 1]
        hi_n, hi_m = spine_numbers[pos], spine_months[pos]
        if hi_n == lo_n:
            continue
        months = lo_m + round((hi_m - lo_m) * (number - lo_n) / (hi_n - lo_n))
        issue["date"] = from_months(months)
        issue["date_confidence"] = "interpolated"
        interpolated += 1

    path.write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")

    dated = sum(1 for i in issues if i.get("date"))
    print(f"\n{interpolated} issues dated by interpolation, {conflicting} marked conflicting")
    print(f"{dated}/{len(issues)} issues now have a date")


if __name__ == "__main__":
    main()

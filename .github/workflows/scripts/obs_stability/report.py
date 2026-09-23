#!/usr/bin/env python3
"""Aggregate per-reader result JSONs into a markdown summary."""

import contextlib
import glob
import json
import os
import sys


def pctl(values, q):
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def fmt(values, unit=""):
    if not values:
        return "n/a"
    return (
        f"min {min(values):.1f} / p50 {pctl(values, 0.5):.1f} / "
        f"p95 {pctl(values, 0.95):.1f} / max {max(values):.1f}{unit}"
    )


def main() -> int:
    results_root = sys.argv[1] if len(sys.argv) > 1 else "results"
    object_key = sys.argv[2] if len(sys.argv) > 2 else "n/a"
    upload_mbps = sys.argv[3] if len(sys.argv) > 3 else "n/a"

    regions = {}
    failures = []
    sha_mismatch = []
    for dirpath in sorted(glob.glob(os.path.join(results_root, "result-*"))):
        result_file = os.path.join(dirpath, "result.json")
        if not os.path.exists(result_file):
            continue
        with open(result_file) as f:
            data = json.load(f)
        region = data.get("region", "other")
        if data.get("attempts") and any(a.get("ok") for a in data["attempts"]):
            if not data.get("sha256_verified", True):
                sha_mismatch.append(data.get("reader_id"))
        stats = regions.setdefault(
            region,
            {"readers": 0, "ok": 0, "bad": 0, "mbps": [], "seconds": [], "curl": []},
        )
        stats["readers"] += 1
        for attempt in data.get("attempts", []):
            if attempt.get("ok"):
                stats["ok"] += 1
                stats["mbps"].append(attempt["mbps"])
                stats["seconds"].append(attempt["seconds"])
            else:
                stats["bad"] += 1
                failures.append(
                    {
                        "reader": data.get("reader_id"),
                        "runner": data.get("runner_tag"),
                        "attempt": attempt.get("attempt"),
                        "error": attempt.get("error"),
                    }
                )
        curl_file = os.path.join(dirpath, "curl.txt")
        if os.path.exists(curl_file):
            with open(curl_file) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 6:
                        with contextlib.suppress(ValueError):
                            stats["curl"].append(float(parts[5]))

    print("## OBS HK bucket access stability report")
    print()
    print(f"- bucket object: `{object_key}`")
    print(f"- upload (prepare job): {upload_mbps} Mbps")
    print()
    print("| region | readers | ok / fail | download Mbps | curl total (s) |")
    print("|---|---|---|---|---|")
    total_bad = 0
    for region in sorted(regions):
        stats = regions[region]
        total_bad += stats["bad"]
        print(
            f"| {region} | {stats['readers']} | {stats['ok']} / {stats['bad']} "
            f"| {fmt(stats['mbps'])} | {fmt(stats['curl'], 's')} |"
        )
    print()
    if failures:
        print("### Failed attempts")
        print()
        for item in failures[:80]:
            print(f"- reader `{item['reader']}` on `{item['runner']}` attempt {item['attempt']}: {item['error']}")
        print()
    if sha_mismatch:
        print(f"**sha256 mismatch in reader(s): {sha_mismatch}** — data corruption, investigate.")
        print()
    if total_bad == 0:
        print("All download attempts succeeded.")
    else:
        print(f"**{total_bad} failed attempt(s)** — see details above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

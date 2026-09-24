#!/usr/bin/env python3
"""Aggregate per-writer/per-reader result JSONs into a markdown summary."""

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


def main():
    results_root = sys.argv[1] if len(sys.argv) > 1 else "results"
    snapshot_key = sys.argv[2] if len(sys.argv) > 2 else "n/a"
    setup_mbps = sys.argv[3] if len(sys.argv) > 3 else "n/a"

    readers = {}
    writers = {}
    failures = []
    sha_mismatch = []
    for dirpath in sorted(glob.glob(os.path.join(results_root, "*"))):
        result_file = os.path.join(dirpath, "result.json")
        write_file = os.path.join(dirpath, "write.json")
        path = result_file if os.path.exists(result_file) else write_file
        if not os.path.exists(path):
            continue
        with open(path) as f:
            data = json.load(f)
        region = data.get("region", "other")
        if data.get("kind") == "write":
            w = writers.setdefault(region, {"jobs": 0, "ok": 0, "bad": 0, "mbps": [], "part_errors": 0})
            w["jobs"] += 1
            if data.get("ok"):
                w["ok"] += 1
                w["mbps"].append(data["mbps"])
                w["part_errors"] += data.get("part_errors") or 0
            else:
                w["bad"] += 1
                failures.append(
                    {"job": f"w{data.get('writer_id')}", "runner": data.get("runner_tag"), "error": data.get("error")}
                )
            continue
        stats = readers.setdefault(
            region,
            {"readers": 0, "ok": 0, "bad": 0, "mbps": [], "seconds": [], "curl": []},
        )
        stats["readers"] += 1
        if data.get("attempts") and any(a.get("ok") for a in data["attempts"]):
            if not data.get("sha256_verified", True):
                sha_mismatch.append(data.get("reader_id"))
        for attempt in data.get("attempts", []):
            if attempt.get("ok"):
                stats["ok"] += 1
                stats["mbps"].append(attempt["mbps"])
                stats["seconds"].append(attempt["seconds"])
            else:
                stats["bad"] += 1
                failures.append(
                    {
                        "job": f"r{data.get('reader_id')}",
                        "runner": data.get("runner_tag"),
                        "error": f"attempt {attempt.get('attempt')}: {attempt.get('error')}",
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

    print("## OBS HK load-model report (csrc-cache style, PR #16718 traffic shape)")
    print()
    print(f"- snapshot object: `{snapshot_key}`; setup MPU (hk): {setup_mbps} Mbps")
    print()
    if writers:
        print("### Writers (concurrent multipart snapshot uploads)")
        print()
        print("| region | writers | ok / fail | upload Mbps | part errors |")
        print("|---|---|---|---|---|")
        for region in sorted(writers):
            w = writers[region]
            print(f"| {region} | {w['jobs']} | {w['ok']} / {w['bad']} | {fmt(w['mbps'])} | {w['part_errors']} |")
        print()
    if readers:
        print("### Readers (concurrent snapshot downloads)")
        print()
        print("| region | readers | ok / fail | download Mbps | curl total (s) |")
        print("|---|---|---|---|---|")
        for region in sorted(readers):
            stats = readers[region]
            print(
                f"| {region} | {stats['readers']} | {stats['ok']} / {stats['bad']} "
                f"| {fmt(stats['mbps'])} | {fmt(stats['curl'], 's')} |"
            )
        print()
    bad = sum(w["bad"] for w in writers.values()) + sum(r["bad"] for r in readers.values())
    if sha_mismatch:
        print(f"**sha256 mismatch in reader(s): {sha_mismatch}** — investigate.")
        print()
    if failures:
        print("### Failed jobs/attempts")
        print()
        for item in failures[:80]:
            print(f"- `{item['job']}` on `{item['runner']}`: {item['error']}")
        print()
    print("All writes/reads succeeded." if bad == 0 else f"**{bad} failure(s)** — see details above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

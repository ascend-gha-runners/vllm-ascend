#!/usr/bin/env python3
"""Simulate one runs-on/cache csrc snapshot writer: generate a large payload and
multipart-upload it, recording per-part behavior."""

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3obj import S3, S3Error  # noqa: E402

CHUNK = 4 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--key", required=True)
    parser.add_argument("--size-mb", type=int, default=2048)
    parser.add_argument("--part-mb", type=int, default=32)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--writer-id", default="")
    parser.add_argument("--region-label", default="other")
    parser.add_argument("--runner-tag", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    path = "/tmp/obs-stability-payload.bin"
    digest = hashlib.sha256()
    remaining = args.size_mb * 1024 * 1024
    gen0 = time.perf_counter()
    with open(path, "wb") as f:
        while remaining > 0:
            block = os.urandom(min(CHUNK, remaining))
            digest.update(block)
            f.write(block)
            remaining -= len(block)
    gen_seconds = time.perf_counter() - gen0

    client = S3(args.endpoint, args.bucket, args.region, os.environ["HW_OBS_AK"], os.environ["HW_OBS_SK"])
    result = {
        "kind": "write",
        "writer_id": args.writer_id,
        "region": args.region_label,
        "runner_tag": args.runner_tag,
        "size_mb": args.size_mb,
        "gen_seconds": round(gen_seconds, 1),
        "ok": False,
        "seconds": None,
        "mbps": None,
        "parts": None,
        "part_errors": None,
        "error": None,
    }
    try:
        elapsed, parts, perrors = client.multipart_put_file(args.key, path, part_mb=args.part_mb, workers=args.workers)
        size = os.path.getsize(path)
        result.update(
            {
                "ok": True,
                "seconds": round(elapsed, 2),
                "mbps": round(size * 8 / elapsed / 1e6, 1),
                "parts": parts,
                "part_errors": perrors,
            }
        )
    except S3Error as exc:
        result["error"] = f"HTTP {exc.status}: {exc.detail}"[:300]
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        if os.path.exists(path):
            os.remove(path)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

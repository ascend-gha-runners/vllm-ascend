#!/usr/bin/env python3
"""Upload a random payload to the HK OBS bucket and build the reader matrix."""

import argparse
import hashlib
import json
import os
import sys
import time

from obs import ObsClient

CHUNK = 4 * 1024 * 1024


def region_of(tag: str) -> str:
    if "cn12" in tag:
        return "cn12"
    if "gy0" in tag:
        return "guiyang"
    if "-hk" in tag:
        return "hongkong"
    return "other"


def make_payload(size_mb: int, path: str) -> str:
    digest = hashlib.sha256()
    remaining = size_mb * 1024 * 1024
    with open(path, "wb") as f:
        while remaining > 0:
            block = os.urandom(min(CHUNK, remaining))
            digest.update(block)
            f.write(block)
            remaining -= len(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--size-mb", type=int, required=True)
    parser.add_argument("--readers", type=int, required=True)
    parser.add_argument("--runner-pool", required=True)
    parser.add_argument("--output-file", default="")
    args = parser.parse_args()

    pool = json.loads(args.runner_pool)
    if not pool:
        print("runner pool is empty", file=sys.stderr)
        return 1
    normalized = []
    for entry in pool:
        if isinstance(entry, str):
            normalized.append({"tag": entry, "region": region_of(entry)})
        else:
            tag = entry["tag"]
            normalized.append({"tag": tag, "region": entry.get("region", region_of(tag))})

    payload = "/tmp/obs-stability-payload.bin"
    sha256 = make_payload(args.size_mb, payload)
    size_bytes = os.path.getsize(payload)
    print(f"payload: {args.size_mb} MB sha256={sha256}")

    client = ObsClient(
        access_key_id=os.environ["HW_OBS_AK"],
        secret_access_key=os.environ["HW_OBS_SK"],
        server=args.endpoint,
    )
    start = time.perf_counter()
    resp = client.putFile(args.bucket, args.key, payload)
    elapsed = time.perf_counter() - start
    if resp.status >= 300:
        print(f"upload failed: status={resp.status} error={resp.errorMessage}", file=sys.stderr)
        return 1
    mbps = size_bytes * 8 / elapsed / 1e6
    print(f"uploaded {args.key} in {elapsed:.2f}s ({mbps:.1f} Mbps)")
    client.close()

    matrix = [
        {
            "reader_id": i,
            "runner": normalized[i % len(normalized)]["tag"],
            "region": normalized[i % len(normalized)]["region"],
        }
        for i in range(args.readers)
    ]

    if args.output_file:
        with open(args.output_file, "a") as f:
            f.write(f"object_key={args.key}\n")
            f.write(f"sha256={sha256}\n")
            f.write(f"upload_mbps={mbps:.1f}\n")
            f.write(f"matrix={json.dumps(matrix)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

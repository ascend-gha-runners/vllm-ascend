#!/usr/bin/env python3
"""Upload a random payload to the HK OBS bucket and build the reader matrix.

Uses only the Python stdlib (runner images have python3 but no pip).
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3obj import S3, S3Error, file_sha256  # noqa: E402

CHUNK = 4 * 1024 * 1024


def make_payload(size_mb, path):
    digest = hashlib.sha256()
    remaining = size_mb * 1024 * 1024
    with open(path, "wb") as f:
        while remaining > 0:
            block = os.urandom(min(CHUNK, remaining))
            digest.update(block)
            f.write(block)
            remaining -= len(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="ap-southeast-1")
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
            normalized.append({"tag": entry, "region": entry, "image": ""})
        else:
            normalized.append(
                {
                    "tag": entry["tag"],
                    "region": entry.get("region", entry["tag"]),
                    "image": entry.get("image", ""),
                }
            )

    payload = "/tmp/obs-stability-payload.bin"
    sha256 = make_payload(args.size_mb, payload)
    size_bytes = os.path.getsize(payload)
    print(f"payload: {args.size_mb} MB sha256={sha256}")

    client = S3(args.endpoint, args.bucket, args.region, os.environ["HW_OBS_AK"], os.environ["HW_OBS_SK"])
    try:
        elapsed = client.put_file(args.key, payload)
    except S3Error as exc:
        print(f"upload failed: {exc}", file=sys.stderr)
        return 1
    mbps = size_bytes * 8 / elapsed / 1e6
    print(f"uploaded {args.key} in {elapsed:.2f}s ({mbps:.1f} Mbps)")
    assert file_sha256(payload) == sha256

    matrix = [
        {
            "reader_id": i,
            "runner": normalized[i % len(normalized)]["tag"],
            "region": normalized[i % len(normalized)]["region"],
            "image": normalized[i % len(normalized)]["image"],
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

#!/usr/bin/env python3
"""Generate + multipart-upload the shared snapshot, then emit writer/reader matrices."""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3obj import S3, S3Error  # noqa: E402

CHUNK = 4 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--size-mb", type=int, required=True)
    parser.add_argument("--writers", type=int, required=True)
    parser.add_argument("--readers", type=int, required=True)
    parser.add_argument("--runner-pool", required=True)
    parser.add_argument("--output-file", required=True)
    args = parser.parse_args()

    pool = json.loads(args.runner_pool)
    if not pool:
        print("runner pool is empty", file=sys.stderr)
        return 1
    normalized = [
        {"tag": e, "region": e, "image": ""}
        if isinstance(e, str)
        else {"tag": e["tag"], "region": e.get("region", e["tag"]), "image": e.get("image", "")}
        for e in pool
    ]

    payload = "/tmp/obs-stability-payload.bin"
    digest = hashlib.sha256()
    remaining = args.size_mb * 1024 * 1024
    with open(payload, "wb") as f:
        while remaining > 0:
            block = os.urandom(min(CHUNK, remaining))
            digest.update(block)
            f.write(block)
            remaining -= len(block)
    sha256 = digest.hexdigest()

    snapshot_key = f"{args.prefix}/snapshot.bin"
    client = S3(args.endpoint, args.bucket, args.region, os.environ["HW_OBS_AK"], os.environ["HW_OBS_SK"])
    try:
        elapsed, parts, perr = client.multipart_put_file(snapshot_key, payload)
    except S3Error as exc:
        print(f"snapshot upload failed: {exc}", file=sys.stderr)
        return 1
    mbps = args.size_mb * 1024 * 1024 * 8 / elapsed / 1e6
    print(f"snapshot MPU: {parts} parts, {elapsed:.1f}s, {mbps:.0f} Mbps, part_errors={perr}")

    def matrix(count, kind):
        return [
            {
                ("writer_id" if kind == "write" else "reader_id"): i,
                "runner": normalized[i % len(normalized)]["tag"],
                "region": normalized[i % len(normalized)]["region"],
                "image": normalized[i % len(normalized)]["image"],
                "key": f"{args.prefix}/{kind}-{i}.bin" if kind == "write" else snapshot_key,
            }
            for i in range(count)
        ]

    with open(args.output_file, "a") as f:
        f.write(f"prefix={args.prefix}\n")
        f.write(f"snapshot_key={snapshot_key}\n")
        f.write(f"sha256={sha256}\n")
        f.write(f"setup_mbps={mbps:.0f}\n")
        f.write(f"matrix_writers={json.dumps(matrix(args.writers, 'write'))}\n")
        f.write(f"matrix_readers={json.dumps(matrix(args.readers, 'read'))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

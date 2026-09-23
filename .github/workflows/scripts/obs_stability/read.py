#!/usr/bin/env python3
"""Download the payload from OBS repeatedly and record per-attempt timing.

Uses only the Python stdlib (runner images have python3 but no pip).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3obj import S3, S3Error, file_sha256  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--key", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--downloads", type=int, default=5)
    parser.add_argument("--region-label", default="other")
    parser.add_argument("--reader-id", default="")
    parser.add_argument("--runner-tag", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    attempts = []
    verified = False
    for n in range(args.downloads):
        record = {"attempt": n, "ok": False, "seconds": None, "mbps": None, "error": None}
        client = S3(args.endpoint, args.bucket, args.region, os.environ["HW_OBS_AK"], os.environ["HW_OBS_SK"])
        target = f"/tmp/obs-dl-{n}.bin"
        try:
            elapsed, written = client.get_file(args.key, target)
            record["ok"] = True
            record["seconds"] = round(elapsed, 3)
            record["mbps"] = round(written * 8 / elapsed / 1e6, 1)
            record["bytes"] = written
            if not verified:
                actual = file_sha256(target)
                record["sha256_match"] = actual == args.sha256
                verified = actual == args.sha256
        except S3Error as exc:
            record["error"] = f"HTTP {exc.status}: {exc.detail}"[:300]
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if os.path.exists(target):
                os.remove(target)
        attempts.append(record)
        print(json.dumps(record))

    result = {
        "reader_id": args.reader_id,
        "region": args.region_label,
        "runner_tag": args.runner_tag,
        "sha256_verified": verified,
        "attempts": attempts,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    ok = sum(1 for a in attempts if a["ok"])
    print(f"{ok}/{args.downloads} attempts succeeded, sha256_verified={verified}")
    raise SystemExit(0 if ok > 0 else 1)


if __name__ == "__main__":
    main()

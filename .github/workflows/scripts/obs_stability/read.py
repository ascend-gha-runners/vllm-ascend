#!/usr/bin/env python3
"""Download the payload from OBS repeatedly and record per-attempt timing."""

import argparse
import hashlib
import json
import os
import time

from obs import ObsClient


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--downloads", type=int, default=5)
    parser.add_argument("--region", default="other")
    parser.add_argument("--runner-id", default="")
    parser.add_argument("--runner-tag", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    attempts = []
    verified = False
    for n in range(args.downloads):
        record = {"attempt": n, "ok": False, "seconds": None, "mbps": None, "error": None}
        client = ObsClient(
            access_key_id=os.environ["HW_OBS_AK"],
            secret_access_key=os.environ["HW_OBS_SK"],
            server=args.endpoint,
        )
        target = f"/tmp/obs-dl-{n}.bin"
        start = time.perf_counter()
        try:
            resp = client.getObject(args.bucket, args.key, download=target)
            elapsed = time.perf_counter() - start
            if resp.status >= 300:
                record["error"] = f"status={resp.status} {resp.errorMessage}"[:300]
            else:
                size = os.path.getsize(target)
                record["ok"] = True
                record["seconds"] = round(elapsed, 3)
                record["mbps"] = round(size * 8 / elapsed / 1e6, 1)
                record["bytes"] = size
                if not verified:
                    actual = sha256_file(target)
                    record["sha256_match"] = actual == args.sha256
                    verified = actual == args.sha256
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            client.close()
            if os.path.exists(target):
                os.remove(target)
        attempts.append(record)
        print(json.dumps(record))

    result = {
        "reader_id": args.runner_id,
        "region": args.region,
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

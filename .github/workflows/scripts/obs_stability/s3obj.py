#!/usr/bin/env python3
"""Minimal S3-compatible (Huawei OBS) client using only the Python stdlib.

Implements AWS SigV4 for PUT/GET/HEAD/DELETE against virtual-hosted-style
bucket endpoints, so the runners do not need pip (esdk-obs-python etc).
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK = 1 << 20


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


class S3Error(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class S3:
    def __init__(self, endpoint, bucket, region, access_key, secret_key):
        host = urllib.parse.urlsplit(endpoint).netloc
        self.host = f"{bucket}.{host}"
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.base = f"https://{self.host}"

    @staticmethod
    def _sign(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _open(self, method, key, payload_hash, body=None):
        path = "/" + urllib.parse.quote(key, safe="/")
        now = datetime.datetime.now(datetime.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = amzdate[:8]
        canonical = f"host:{self.host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amzdate}\n"
        creq = "\n".join([method, path, "", canonical, "host;x-amz-content-sha256;x-amz-date", payload_hash])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(creq.encode("utf-8")).hexdigest()])
        key_ = self._sign(("AWS4" + self.secret_key).encode(), datestamp)
        key_ = self._sign(key_, self.region)
        key_ = self._sign(key_, "s3")
        key_ = self._sign(key_, "aws4_request")
        signature = hmac.new(key_, sts.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "x-amz-date": amzdate,
            "x-amz-content-sha256": payload_hash,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                f"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={signature}"
            ),
        }
        req = urllib.request.Request(self.base + path, method=method, data=body, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace").replace("\n", " ")
            raise S3Error(exc.code, detail) from None

    def put_file(self, key, path):
        payload_hash = file_sha256(path)
        with open(path, "rb") as f:
            body = f.read()
        start = time.perf_counter()
        resp = self._open("PUT", key, payload_hash, body=body)
        elapsed = time.perf_counter() - start
        resp.close()
        return elapsed

    def get_file(self, key, path):
        start = time.perf_counter()
        resp = self._open("GET", key, EMPTY_SHA256)
        written = 0
        with open(path, "wb") as f:
            for block in iter(lambda: resp.read(CHUNK), b""):
                f.write(block)
                written += len(block)
        elapsed = time.perf_counter() - start
        resp.close()
        return elapsed, written

    def delete_file(self, key):
        self._open("DELETE", key, EMPTY_SHA256).close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["put", "get", "delete"])
    parser.add_argument("--endpoint", default=os.environ["OBS_ENDPOINT"])
    parser.add_argument("--bucket", default=os.environ["OBS_BUCKET"])
    parser.add_argument("--region", default=os.environ.get("OBS_REGION", "ap-southeast-1"))
    parser.add_argument("--key", required=True)
    parser.add_argument("--file", default="")
    args = parser.parse_args()

    client = S3(args.endpoint, args.bucket, args.region, os.environ["OBS_AK"], os.environ["OBS_SK"])
    if args.action == "put":
        elapsed = client.put_file(args.key, args.file)
        size = os.path.getsize(args.file)
        out = {
            "ok": True,
            "seconds": round(elapsed, 3),
            "mbps": round(size * 8 / elapsed / 1e6, 1),
            "sha256": file_sha256(args.file),
        }
        print(json.dumps(out))
    elif args.action == "get":
        target = args.file or "/tmp/s3-get.bin"
        elapsed, written = client.get_file(args.key, target)
        out = {
            "ok": True,
            "seconds": round(elapsed, 3),
            "bytes": written,
            "sha256": file_sha256(target),
        }
        print(json.dumps(out))
    else:
        client.delete_file(args.key)
        print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()

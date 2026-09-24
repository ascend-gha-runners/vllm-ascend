#!/usr/bin/env python3
"""Minimal S3-compatible (Huawei OBS) client using only the Python stdlib.

Implements AWS SigV4 for PUT/GET/HEAD/DELETE and multipart uploads against
virtual-hosted-style bucket endpoints, so runners do not need pip.
"""

import argparse
import contextlib
import datetime
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK = 1 << 20


def _enc(s):
    return urllib.parse.quote(str(s), safe="-._~")


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

    def _request(self, method, path, payload_hash, params=None, body=None):
        """path: already-quoted URI path. params: dict of raw query params."""
        if params:
            items = sorted(params.items())
            cqs = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in items)
            url = (
                self.base
                + path
                + "?"
                + "&".join(
                    f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}" for k, v in items
                )
            )
        else:
            cqs = ""
            url = self.base + path
        now = datetime.datetime.now(datetime.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = amzdate[:8]
        canonical = f"host:{self.host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amzdate}\n"
        creq = "\n".join([method, path, cqs, canonical, "host;x-amz-content-sha256;x-amz-date", payload_hash])
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
        req = urllib.request.Request(url, method=method, data=body, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace").replace("\n", " ")
            raise S3Error(exc.code, detail) from None

    @staticmethod
    def _path(key):
        return "/" + urllib.parse.quote(key, safe="/")

    def put_file(self, key, path):
        payload_hash = file_sha256(path)
        with open(path, "rb") as f:
            body = f.read()
        start = time.perf_counter()
        self._request("PUT", self._path(key), payload_hash, body=body).close()
        return time.perf_counter() - start

    def get_file(self, key, path):
        start = time.perf_counter()
        resp = self._request("GET", self._path(key), EMPTY_SHA256)
        written = 0
        with open(path, "wb") as f:
            for block in iter(lambda: resp.read(CHUNK), b""):
                f.write(block)
                written += len(block)
        elapsed = time.perf_counter() - start
        resp.close()
        return elapsed, written

    def delete_file(self, key):
        self._request("DELETE", self._path(key), EMPTY_SHA256).close()

    def delete_prefix(self, prefix):
        token = ""
        deleted = 0
        while True:
            params = {"list-type": "2", "max-keys": "1000", "prefix": prefix}
            if token:
                params["continuation-token"] = token
            resp = self._request("GET", "/", EMPTY_SHA256, params=params)
            body = resp.read().decode("utf-8", "replace")
            resp.close()
            for key in re.findall(r"<Key>(.*?)</Key>", body):
                self.delete_file(urllib.parse.unquote(key))
                deleted += 1
            if "<IsTruncated>true</IsTruncated>" in body:
                token = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", body).group(1)
                token = token.replace("&amp;", "&")
            else:
                break
        return deleted

    # ---- multipart upload (runs-on/cache style) ----

    def create_mpu(self, key):
        resp = self._request("POST", self._path(key), EMPTY_SHA256, params={"uploads": ""})
        body = resp.read()
        resp.close()
        m = re.search(r"<UploadId>(.*?)</UploadId>", body.decode("utf-8", "replace"))
        if not m:
            raise S3Error(0, "no UploadId in create response")
        return m.group(1)

    def _part(self, key, upload_id, number, path, part_bytes, retries=3):
        with open(path, "rb") as fh:
            fh.seek((number - 1) * part_bytes)
            chunk = fh.read(part_bytes)
        digest = hashlib.sha256(chunk).hexdigest()
        for attempt in range(retries):
            try:
                resp = self._request(
                    "PUT",
                    self._path(key),
                    digest,
                    params={"partNumber": str(number), "uploadId": upload_id},
                    body=chunk,
                )
                etag = resp.headers.get("ETag", "")
                resp.close()
                if etag:
                    return (number, etag)
                raise S3Error(0, "no etag")
            except S3Error:
                if attempt == retries - 1:
                    raise
                time.sleep(1 + attempt)
        return None

    def multipart_put_file(self, key, path, part_mb=32, workers=4):
        """Returns (seconds, n_parts, n_errors)."""
        size = os.path.getsize(path)
        part_bytes = part_mb * 1024 * 1024
        n_parts = max(1, -(-size // part_bytes))
        upload_id = self.create_mpu(key)
        errors = []

        def one(num):
            try:
                return self._part(key, upload_id, num, path, part_bytes)
            except S3Error as exc:
                errors.append(f"p{num}:{exc.status}")
                return None

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parts = [r for r in pool.map(one, range(1, n_parts + 1)) if r]
        elapsed = time.perf_counter() - start
        if len(parts) != n_parts:
            with contextlib.suppress(S3Error):
                self._request("DELETE", self._path(key), EMPTY_SHA256, params={"uploadId": upload_id}).close()
            raise S3Error(0, f"parts {len(parts)}/{n_parts} failed: {errors[:6]}")
        xml = "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in sorted(parts))
        body = f"<CompleteMultipartUpload>{xml}</CompleteMultipartUpload>".encode()
        digest = hashlib.sha256(body).hexdigest()
        self._request("POST", self._path(key), digest, params={"uploadId": upload_id}, body=body).close()
        return elapsed, n_parts, len(errors)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["put", "get", "delete", "delete-prefix", "mpu"])
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
    elif args.action == "mpu":
        elapsed, n_parts, n_errors = client.multipart_put_file(args.key, args.file)
        size = os.path.getsize(args.file)
        out = {
            "ok": True,
            "seconds": round(elapsed, 3),
            "mbps": round(size * 8 / elapsed / 1e6, 1),
            "parts": n_parts,
            "part_errors": n_errors,
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
    elif args.action == "delete-prefix":
        print(json.dumps({"ok": True, "deleted": client.delete_prefix(args.key)}))
    else:
        client.delete_file(args.key)
        print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()

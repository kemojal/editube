#!/usr/bin/env python3
"""R2 connectivity smoke test.

Reads R2_* from editube/.env, then round-trips an object:
put -> get (S3 API) -> get (public URL) -> delete. Prints a checklist.

Usage:
    cd editube
    .venv/bin/python scripts/r2_smoke_test.py
"""
import os
import sys
import uuid
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

try:
    import boto3
    from botocore.config import Config
except ImportError:
    sys.exit("boto3 not installed. Run: .venv/bin/python -m pip install boto3")

acct = os.getenv("R2_ACCOUNT_ID")
ak = os.getenv("R2_ACCESS_KEY_ID")
sk = os.getenv("R2_SECRET_ACCESS_KEY")
bucket = os.getenv("R2_BUCKET")
pub = os.getenv("R2_PUBLIC_BASE_URL")
endpoint = os.getenv("R2_ENDPOINT_URL") or f"https://{acct}.r2.cloudflarestorage.com"

missing = [k for k, v in {
    "R2_ACCOUNT_ID": acct, "R2_ACCESS_KEY_ID": ak, "R2_SECRET_ACCESS_KEY": sk,
    "R2_BUCKET": bucket, "R2_PUBLIC_BASE_URL": pub,
}.items() if not v]
if missing:
    sys.exit(f"Missing env: {', '.join(missing)}")

print(f"bucket: {bucket} | public_base: {pub}")
print(f"endpoint: {endpoint}")

s3 = boto3.client(
    "s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=sk,
    region_name="auto", config=Config(s3={"addressing_style": "path"}),
)

try:
    s3.head_bucket(Bucket=bucket)
    print("head_bucket: OK")
except Exception as e:  # noqa: BLE001
    sys.exit(f"head_bucket FAIL: {type(e).__name__}: {str(e)[:200]}")

key = f"_smoketest/{uuid.uuid4().hex}.txt"
body = b"editube r2 smoke test"
s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain")
print(f"put_object OK -> {key}")

got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
print(f"get_object roundtrip: {got == body}")

url = f"{pub.rstrip('/')}/{key}"
print(f"public url: {url}")
# The r2.dev public edge has a brief read-after-write lag for brand-new keys, so
# a GET issued <1s after put can 403 before the object propagates. Retry with
# backoff — a persistent 403/404 across all attempts means public access is off
# or R2_PUBLIC_BASE_URL points at the wrong host.
import time  # noqa: E402

ok = False
# Send a browser User-Agent: Cloudflare's bot protection 403s the default
# `Python-urllib/x` UA on r2.dev. Real browsers / <video> players are unaffected.
for attempt in range(1, 6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
            print(f"public GET: {r.status} | match: {data == body} "
                  f"| accept-ranges: {r.headers.get('Accept-Ranges')} "
                  f"(attempt {attempt})")
            ok = True
            break
    except Exception as e:  # noqa: BLE001
        print(f"public GET attempt {attempt}: {type(e).__name__}: {str(e)[:120]}")
        time.sleep(attempt)  # 1s, 2s, 3s, 4s
if not ok:
    print("public GET FAIL after retries — check step 2 (public access enabled) "
          "and that R2_PUBLIC_BASE_URL is the r2.dev / custom-domain host, "
          "not the S3 API endpoint.")

s3.delete_object(Bucket=bucket, Key=key)
print("cleanup OK")

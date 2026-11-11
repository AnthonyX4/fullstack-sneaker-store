"""
Quick script to verify AWS S3 credentials and permissions.

Run from Backend/ with: uv run check_s3.py

Checks that the .env credentials can upload to and delete from the bucket, and that uploaded
files are publicly readable (what the frontend's <img> tags need), without going through the app.
"""

from io import BytesIO

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from config import settings
from image_utils import PROFILE_PICS_PREFIX, _get_s3_client


def check_s3_connection() -> bool:
    s3 = _get_s3_client()

    print(f"Bucket: {settings.s3_bucket_name}")
    print(f"Region: {settings.s3_region}")
    print()

    test_key = f"{PROFILE_PICS_PREFIX}test.txt" # under profile_pics/, the only prefix the IAM policy allows

    # 1. upload (IAM policy: s3:PutObject)
    try:
        s3.upload_fileobj(
            BytesIO(b"test"),
            settings.s3_bucket_name,
            test_key,
            ExtraArgs={"ContentType": "text/plain"},
        )
        print("Upload: SUCCESS")
    except (BotoCoreError, ClientError) as exc:
        print(f"Upload: FAILED - {exc}")
        return False

    # 2. public read (bucket policy: s3:GetObject for everyone), same url shape as User.image_path
    public_url = f"https://{settings.s3_bucket_name}.s3.{settings.s3_region}.amazonaws.com/{test_key}"
    try:
        response = httpx.get(public_url, timeout=10)
        public_ok = response.status_code == 200
        if public_ok:
            print("Public read: SUCCESS")
        else:
            print(f"Public read: FAILED - HTTP {response.status_code} (check Block Public Access and the bucket policy)")
    except httpx.HTTPError as exc:
        public_ok = False
        print(f"Public read: FAILED - {exc}")

    # 3. delete (IAM policy: s3:DeleteObject), runs even if the read failed so the test file doesn't stay behind
    try:
        s3.delete_object(Bucket=settings.s3_bucket_name, Key=test_key)
        print("Delete: SUCCESS")
    except (BotoCoreError, ClientError) as exc:
        print(f"Delete: FAILED - {exc}")
        return False

    print()
    if public_ok:
        print("All tests passed! Your S3 configuration is working.")
    else:
        print("Uploads work, but browsers won't be able to load the pictures yet.")
    return public_ok


if __name__ == "__main__":
    raise SystemExit(0 if check_s3_connection() else 1)

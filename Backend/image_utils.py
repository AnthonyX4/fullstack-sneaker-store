import logging
import uuid
from io import BytesIO

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from config import settings

logger = logging.getLogger(__name__)

# every object key starts with this prefix, the IAM policy only allows put/delete under profile_pics/*
PROFILE_PICS_PREFIX = "profile_pics/"


def _get_s3_client():
    return boto3.client(
        "s3",
        region_name=settings.s3_region,
        aws_access_key_id=(
            settings.s3_access_key_id.get_secret_value()
            if settings.s3_access_key_id
            else None
        ),
        aws_secret_access_key=(
            settings.s3_secret_access_key.get_secret_value()
            if settings.s3_secret_access_key
            else None
        ),
        endpoint_url=settings.s3_endpoint_url,
    )


# CPU bound (decode, resize, encode), so this is a plain sync function
# the upload route calls it through run_in_threadpool so it doesn't block the event loop
def process_profile_image(content: bytes) -> tuple[bytes, str]:
    """Square crop to 300x300 and encode as jpeg. Returns the jpeg bytes and a random filename for them."""
    with Image.open(BytesIO(content)) as original: # raises UnidentifiedImageError if the bytes aren't an image
        img = ImageOps.exif_transpose(original) # phone photos store rotation in exif, apply it so the picture isn't sideways

        img = ImageOps.fit(img, (300, 300), method=Image.Resampling.LANCZOS) # crop to square from the center, then resize

        # jpeg only supports RGB, transparency (RGBA/LA) and palette (P) images have to be converted
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")

        filename = f"{uuid.uuid4().hex}.jpg" # never use the uploaded filename, it's user controlled and may collide

        # encoded into memory instead of a file, the bytes go straight to S3
        output = BytesIO()
        img.save(output, "JPEG", quality=85, optimize=True)

    return output.getvalue(), filename


# boto3 is a sync library (blocking network calls), so these run in a worker thread through the async wrappers below
def _upload_to_s3(file_bytes: bytes, key: str) -> None:
    s3 = _get_s3_client()
    s3.upload_fileobj(
        BytesIO(file_bytes),
        settings.s3_bucket_name,
        key,
        ExtraArgs={"ContentType": "image/jpeg"}, # without it S3 serves the file as binary/octet-stream and browsers download it instead of showing it
    )


def _delete_from_s3(key: str) -> None:
    s3 = _get_s3_client()
    s3.delete_object(Bucket=settings.s3_bucket_name, Key=key)


async def upload_profile_image(file_bytes: bytes, filename: str) -> None:
    """Raises BotoCoreError/ClientError if the upload fails, the caller decides what the user sees."""
    await run_in_threadpool(_upload_to_s3, file_bytes, f"{PROFILE_PICS_PREFIX}{filename}")


async def delete_profile_image(filename: str | None) -> None:
    """Best effort: always called after the db commit, so a failure only leaves an orphan object in the bucket."""
    if filename is None:
        return

    try:
        await run_in_threadpool(_delete_from_s3, f"{PROFILE_PICS_PREFIX}{filename}")
    except (BotoCoreError, ClientError):
        # the db no longer points at the file, so failing the request here would report an error for a change that succeeded
        logger.exception("Failed to delete profile picture %s from S3", filename)

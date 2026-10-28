"""Adapted from pythonProject/file_utils.py: download -> S3 -> CloudFront URL.

Keeps the existing kigu-db bucket and CloudFront distribution, with bounded
downloads, Feishu decryption, verified WebP output, and explicit upload failures.
Credentials come from boto3's normal environment/profile/role credential chain.
"""
import base64
import hashlib
from io import BytesIO
import os
import warnings
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image, ImageOps
import requests

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000


class ImageError(RuntimeError):
    pass


def download_image(source, session=None):
    url = source.get("url", "")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ImageError("invalid_image_url")
    client = session or requests.Session()
    # Never send Feishu session cookies to a third-party image host.
    with client.get(url, timeout=(10, 45), stream=True) as response:
        response.raise_for_status()
        if int(response.headers.get("Content-Length", "0")) > MAX_DOWNLOAD_BYTES:
            raise ImageError("image_too_large")
        content = bytearray()
        for chunk in response.iter_content(64 * 1024):
            content.extend(chunk)
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise ImageError("image_too_large")
    cipher_type = str(source.get("cipherType", "0"))
    if cipher_type in {"1", "AES"}:
        try:
            key = base64.b64decode(source["secret"], validate=True)
            nonce = base64.b64decode(source["nonce"], validate=True)
            return AESGCM(key).decrypt(nonce, bytes(content), None)
        except Exception:
            raise ImageError("image_decryption_failed") from None
    if cipher_type not in {"0", "PLANT_TEXT", "PLAIN_TEXT"}:
        raise ImageError("unsupported_image_cipher")
    return bytes(content)


def normalize_image(content):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ImageError("image_dimensions_too_large")
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                image.thumbnail((1600, 1600))
                image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
                output = BytesIO()
                image.save(output, format="WEBP", quality=85, method=4)
                return output.getvalue()
    except ImageError:
        raise
    except Exception:
        raise ImageError("invalid_image_data") from None


class CoverImageStore:
    def __init__(self, s3_client=None, bucket=None, public_base_url=None):
        self._s3 = s3_client
        self.bucket = bucket or os.environ.get("S3_BUCKET", "kigu-db")
        self.public_base_url = (public_base_url or os.environ.get(
            "S3_PUBLIC_BASE_URL", "https://d1kkex4vltbo88.cloudfront.net")).rstrip("/")
        if urlsplit(self.public_base_url).scheme != "https":
            raise ValueError("S3_PUBLIC_BASE_URL must use HTTPS")

    def prepare(self, source):
        return normalize_image(download_image(source))

    def upload(self, product_id, content):
        if not product_id.isdigit():
            raise ImageError("invalid_product_id")
        if self._s3 is None:
            import boto3
            from botocore.config import Config
            self._s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"), config=Config(
                connect_timeout=10, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"}))
        digest = hashlib.sha256(content).hexdigest()[:24]
        key = f"resell-posts/covers/{product_id}/{digest}.webp"
        # No public ACL: serve via the existing CloudFront origin access policy.
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=content,
                            ContentType="image/webp", CacheControl="public, max-age=31536000, immutable")
        return f"{self.public_base_url}/{key}"

    def import_cover(self, product_id, source):
        return self.upload(product_id, self.prepare(source))

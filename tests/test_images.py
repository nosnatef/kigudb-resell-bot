import base64
from io import BytesIO
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image

from file_utils import CoverImageStore, ImageError, download_image, normalize_image
from test_worker import FakeDatabase, source_row, response
from worker import FIELDS, FeishuFetcher, SourceError, import_rows


def png():
    output = BytesIO()
    Image.new("RGB", (20, 12), color="pink").save(output, format="PNG")
    return output.getvalue()


def download_response(content):
    response = Mock()
    response.headers = {"Content-Length": str(len(content))}
    response.iter_content.return_value = [content]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class ImageTests(unittest.TestCase):
    def test_aes_gcm_download_decodes_original_image(self):
        key, nonce = b"k" * 32, b"n" * 12
        data = png()
        encrypted = AESGCM(key).encrypt(nonce, data, None)
        client = Mock()
        client.get.return_value = download_response(encrypted)
        result = download_image({"url": "https://cdn.example.com/cover", "cipherType": "1",
                                 "secret": base64.b64encode(key).decode(),
                                 "nonce": base64.b64encode(nonce).decode()}, client)
        self.assertEqual(result, data)

    def test_wrong_key_and_unsupported_cipher_fail(self):
        client = Mock()
        client.get.return_value = download_response(b"encrypted")
        for source in ({"cipherType": "1", "secret": "bad", "nonce": "bad"}, {"cipherType": "3"}):
            with self.assertRaises(ImageError):
                download_image({"url": "https://cdn.example.com/cover", **source}, client)

    def test_invalid_images_and_oversized_downloads_rejected(self):
        with self.assertRaises(ImageError):
            normalize_image(b"<html>not an image</html>")
        client = Mock()
        response = download_response(b"")
        response.headers = {"Content-Length": str(11 * 1024 * 1024)}
        client.get.return_value = response
        with self.assertRaises(ImageError):
            download_image({"url": "https://cdn.example.com/cover"}, client)

    def test_verified_webp_uploaded_to_existing_bucket(self):
        client = Mock()
        store = CoverImageStore(s3_client=client)
        data = normalize_image(png())
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, "WEBP")
        first = store.upload("1095153548801", data)
        self.assertEqual(first, store.upload("1095153548801", data))
        request = client.put_object.call_args.kwargs
        self.assertEqual(request["Bucket"], "kigu-db")
        self.assertEqual(request["ContentType"], "image/webp")
        self.assertTrue(request["Key"].startswith("resell-posts/covers/1095153548801/"))
        self.assertTrue(first.startswith("https://d1kkex4vltbo88.cloudfront.net/"))
        self.assertEqual(request["Body"], data)

    def test_upload_failure_never_returns_fake_url(self):
        client = Mock()
        client.put_object.side_effect = RuntimeError("upload failed")
        with self.assertRaises(RuntimeError):
            CoverImageStore(s3_client=client).upload("1095153548801", normalize_image(png()))

    def test_attachment_authorization_uses_public_share(self):
        fetcher = FeishuFetcher()
        fetcher.base_token = "base"
        fetcher.session = Mock()
        fetcher.session.cookies = []
        source = {"url": "https://cdn.example.com/file", "cipherType": "1", "secret": "secret", "nonce": "nonce"}
        fetcher.session.post.return_value = response({"token": source})
        row = source_row()
        row[FIELDS["cover"]] = {"value": ["filename.webp"], "originValue": [{"attachmentToken": "token"}]}
        row["rec_id"] = {"value": "record"}
        self.assertEqual(fetcher.get_cover_source(row), source)
        self.assertEqual(fetcher.session.post.call_args.kwargs["json"]["recordIDs"], ["record"])
        row[FIELDS["cover"]] = {"value": ["filename.webp"]}
        with self.assertRaises(SourceError):
            fetcher.get_cover_source(row)

    def test_upload_happens_only_for_new_posts_in_write_mode(self):
        importer = Mock(return_value="https://cloudfront.example.com/cover.webp")
        conn = FakeDatabase()
        row = source_row()
        import_rows(conn, [row], "worker", {}, "https://source", False, importer)
        importer.assert_not_called()
        import_rows(conn, [row], "worker", {}, "https://source", True, importer)
        self.assertEqual(next(iter(conn.posts.values()))[7], importer.return_value)
        import_rows(conn, [row], "worker", {}, "https://source", True, importer)
        self.assertEqual(importer.call_count, 1)

    def test_image_failure_leaves_listing_unimported_for_retry(self):
        importer = Mock(side_effect=ImageError("private secret must not be reported"))
        conn = FakeDatabase()
        summary, report = import_rows(conn, [source_row()], "worker", {}, "https://source", True, importer)
        self.assertEqual(summary, {"image_failed": 1})
        self.assertNotIn("private secret", str(report))
        self.assertFalse(conn.posts)
        self.assertFalse(conn.links)


if __name__ == "__main__":
    unittest.main()

import io
import os
import tempfile
import unittest
from pathlib import Path

from app.storage import (
    build_key,
    get_storage,
    guess_content_type,
    reset_storage_cache,
    storage_available,
)
from app.storage.base import UploadResult
from app.storage.local import LocalBackend
from app.storage.r2 import R2Backend
from app.storage.cloudinary_backend import CloudinaryBackend


class BuildKeyTests(unittest.TestCase):
    def test_public_id_used_verbatim_with_derived_ext(self):
        key = build_key(folder="repurpose_clips", public_id="42/thumbnail",
                        content_type="image/jpeg")
        self.assertEqual(key, "repurpose_clips/42/thumbnail.jpg")

    def test_public_id_with_existing_ext_not_double_suffixed(self):
        key = build_key(folder="f", public_id="name.png", content_type="image/jpeg")
        self.assertEqual(key, "f/name.png")

    def test_uuid_when_no_public_id(self):
        key = build_key(folder="videos", filename="clip.mp4")
        self.assertTrue(key.startswith("videos/"))
        self.assertTrue(key.endswith(".mp4"))
        self.assertNotIn("clip", key)  # uuid, not the source name

    def test_no_folder(self):
        key = build_key(public_id="avatar", content_type="image/png")
        self.assertEqual(key, "avatar.png")

    def test_slashes_stripped(self):
        key = build_key(folder="/contracts/", public_id="/c1/")
        self.assertEqual(key, "contracts/c1")


class ContentTypeTests(unittest.TestCase):
    def test_from_filename(self):
        self.assertEqual(guess_content_type("a.mp4"), "video/mp4")
        self.assertEqual(guess_content_type("a.png"), "image/png")
        self.assertEqual(guess_content_type("a.pdf"), "application/pdf")

    def test_resource_type_fallback(self):
        self.assertEqual(guess_content_type(None, resource_type="video"), "video/mp4")
        self.assertEqual(guess_content_type(None, resource_type="image"), "image/jpeg")
        self.assertEqual(guess_content_type("noext", resource_type="raw"),
                         "application/octet-stream")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("STORAGE_BACKEND")
        reset_storage_cache()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("STORAGE_BACKEND", None)
        else:
            os.environ["STORAGE_BACKEND"] = self._prev
        reset_storage_cache()

    def test_default_is_cloudinary(self):
        os.environ.pop("STORAGE_BACKEND", None)
        reset_storage_cache()
        self.assertIsInstance(get_storage(), CloudinaryBackend)

    def test_selects_r2(self):
        os.environ["STORAGE_BACKEND"] = "r2"
        reset_storage_cache()
        self.assertIsInstance(get_storage(), R2Backend)

    def test_selects_local(self):
        os.environ["STORAGE_BACKEND"] = "local"
        reset_storage_cache()
        self.assertIsInstance(get_storage(), LocalBackend)

    def test_unknown_raises(self):
        os.environ["STORAGE_BACKEND"] = "bogus"
        reset_storage_cache()
        with self.assertRaises(ValueError):
            get_storage()

    def test_singleton_cached(self):
        os.environ["STORAGE_BACKEND"] = "local"
        reset_storage_cache()
        self.assertIs(get_storage(), get_storage())


class LocalBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["BASE_URL"] = "https://example.test"
        self.backend = LocalBackend()
        self.backend._root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_upload_bytes_roundtrip(self):
        res = self.backend.upload_bytes(b"hello", key="a/b.txt", content_type="text/plain")
        self.assertIsInstance(res, UploadResult)
        self.assertEqual(res.url, "https://example.test/uploads/a/b.txt")
        self.assertEqual(res.bytes, 5)
        self.assertEqual((Path(self.tmp.name) / "a/b.txt").read_bytes(), b"hello")

    def test_upload_stream(self):
        res = self.backend.upload_stream(io.BytesIO(b"1234"), key="s.bin",
                                         content_type="application/octet-stream")
        self.assertEqual(res.bytes, 4)

    def test_upload_path(self):
        src = Path(self.tmp.name) / "src.txt"
        src.write_text("payload")
        res = self.backend.upload_path(src, key="dst/x.txt", content_type="text/plain")
        self.assertEqual((Path(self.tmp.name) / "dst/x.txt").read_text(), "payload")
        self.assertEqual(res.key, "dst/x.txt")

    def test_traversal_blocked(self):
        with self.assertRaises(ValueError):
            self.backend.upload_bytes(b"x", key="../escape.txt", content_type="text/plain")

    def test_available_always_true(self):
        self.assertTrue(self.backend.available())


class R2BackendConfigTests(unittest.TestCase):
    def test_available_requires_all_env(self):
        backend = R2Backend()
        backend._account_id = "acct"
        backend._access_key = "ak"
        backend._secret_key = "sk"
        backend._bucket = "b"
        backend._public_base = "https://pub-x.r2.dev"
        self.assertTrue(backend.available())
        backend._public_base = ""
        self.assertFalse(backend.available())

    def test_public_url_format(self):
        backend = R2Backend()
        backend._public_base = "https://pub-x.r2.dev"
        self.assertEqual(backend.public_url("videos/a.mp4"),
                         "https://pub-x.r2.dev/videos/a.mp4")
        self.assertEqual(backend.public_url("/leading.mp4"),
                         "https://pub-x.r2.dev/leading.mp4")

    def test_presigned_upload_binds_key_and_content_type(self):
        class FakeS3:
            def generate_presigned_url(self, operation, *, Params, ExpiresIn):
                self.call = (operation, Params, ExpiresIn)
                return "https://signed.example/upload"

        backend = R2Backend()
        backend._bucket = "media"
        backend._public_base = "https://cdn.example"
        fake = FakeS3()
        backend._client = fake

        target = backend.create_presigned_upload(
            key="videos/a.mp4", content_type="video/mp4", expires_in=60
        )

        self.assertEqual(target.upload_url, "https://signed.example/upload")
        self.assertEqual(target.public_url, "https://cdn.example/videos/a.mp4")
        self.assertEqual(target.headers, {"Content-Type": "video/mp4"})
        self.assertEqual(
            fake.call,
            (
                "put_object",
                {"Bucket": "media", "Key": "videos/a.mp4", "ContentType": "video/mp4"},
                60,
            ),
        )
    def test_s3_raises_when_unconfigured(self):
        backend = R2Backend()
        backend._account_id = None
        backend._access_key = None
        backend._bucket = None
        backend._public_base = ""
        with self.assertRaises(RuntimeError):
            backend._s3()


class CloudinaryBackendTests(unittest.TestCase):
    def test_split_key(self):
        from app.storage.cloudinary_backend import _split_key, _resource_type
        self.assertEqual(_split_key("delivery/pkg.zip"), ("delivery", "pkg"))
        self.assertEqual(_split_key("name"), ("", "name"))
        self.assertEqual(_resource_type("image/png"), "image")
        self.assertEqual(_resource_type("video/mp4"), "video")
        self.assertEqual(_resource_type("application/zip"), "raw")

    def test_public_url_not_supported(self):
        with self.assertRaises(NotImplementedError):
            CloudinaryBackend().public_url("x")


if __name__ == "__main__":
    unittest.main()

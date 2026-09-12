import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from openlist import OpenListConfig, OpenListError, OpenListUploader, upload_enabled


class OpenListUploaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.folder = Path(self.tempdir.name) / "中文帖子"
        self.folder.mkdir()
        self.file = self.folder / "video.mp4"
        self.file.write_bytes(b"complete video data")
        self.remote_files = {}
        self.put_count = 0
        self.config = OpenListConfig(
            url="http://openlist.test",
            username="user",
            password="secret",
            target_path="/移动6611-加密",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _hash_info(self, content):
        return {
            "md5": hashlib.md5(content).hexdigest(),
            "sha1": hashlib.sha1(content).hexdigest(),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _transport(self, include_hashes=True, fail_put=False):
        async def handler(request):
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"code": 200, "data": {"token": "token"}})
            if request.url.path == "/api/fs/get":
                body = __import__("json").loads(request.content)
                content = self.remote_files.get(body["path"])
                if content is None:
                    return httpx.Response(200, json={"code": 500, "message": "object not found"})
                data = {"name": Path(body["path"]).name, "size": len(content), "is_dir": False}
                data["hash_info"] = self._hash_info(content) if include_hashes else {}
                return httpx.Response(200, json={"code": 200, "data": data})
            if request.method == "MKCOL":
                return httpx.Response(201)
            if request.method == "PUT":
                self.put_count += 1
                if fail_put:
                    return httpx.Response(503)
                self.remote_files[request.url.path.removeprefix("/dav")] = request.content
                return httpx.Response(201)
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    async def _uploader(self, **transport_options):
        return OpenListUploader(self.config, self._transport(**transport_options))

    async def test_uploads_folder_and_removes_local_copy(self):
        uploader = await self._uploader()
        try:
            result = await uploader.upload_folder(self.folder)
        finally:
            await uploader.close()
        self.assertEqual(result, {"uploaded": 1, "skipped": 0, "total": 1})
        self.assertFalse(self.folder.exists())
        self.assertIn("/移动6611-加密/中文帖子/video.mp4", self.remote_files)

    async def test_matching_hash_skips_existing_file(self):
        content = self.file.read_bytes()
        self.remote_files["/移动6611-加密/中文帖子/video.mp4"] = content
        uploader = await self._uploader()
        try:
            result = await uploader.upload_folder(self.folder)
        finally:
            await uploader.close()
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.put_count, 0)
        self.assertFalse(self.folder.exists())

    async def test_partial_remote_file_is_overwritten(self):
        self.remote_files["/移动6611-加密/中文帖子/video.mp4"] = b"partial"
        uploader = await self._uploader()
        try:
            result = await uploader.upload_folder(self.folder)
        finally:
            await uploader.close()
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(self.put_count, 1)
        self.assertEqual(
            self.remote_files["/移动6611-加密/中文帖子/video.mp4"],
            b"complete video data",
        )

    async def test_same_size_wrong_hash_is_overwritten(self):
        self.remote_files["/移动6611-加密/中文帖子/video.mp4"] = b"x" * len(self.file.read_bytes())
        uploader = await self._uploader()
        try:
            result = await uploader.upload_folder(self.folder)
        finally:
            await uploader.close()
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(self.put_count, 1)

    async def test_size_fallback_skips_when_storage_has_no_hash(self):
        content = self.file.read_bytes()
        self.remote_files["/移动6611-加密/中文帖子/video.mp4"] = content
        uploader = await self._uploader(include_hashes=False)
        try:
            result = await uploader.upload_folder(self.folder)
        finally:
            await uploader.close()
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.put_count, 0)

    async def test_failure_preserves_local_folder(self):
        uploader = await self._uploader(fail_put=True)
        try:
            with self.assertRaises(OpenListError):
                await uploader.upload_folder(self.folder)
        finally:
            await uploader.close()
        self.assertTrue(self.folder.exists())
        self.assertTrue(self.file.exists())

    def test_upload_switch(self):
        with patch.dict(os.environ, {"OPENLIST_UPLOAD_ENABLED": "true"}, clear=False):
            self.assertTrue(upload_enabled())
        with patch.dict(os.environ, {"OPENLIST_UPLOAD_ENABLED": "false"}, clear=False):
            self.assertFalse(upload_enabled())


if __name__ == "__main__":
    unittest.main()

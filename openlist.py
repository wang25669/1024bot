import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx


class OpenListError(Exception):
    pass


def upload_enabled() -> bool:
    return os.environ.get("OPENLIST_UPLOAD_ENABLED", "").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OpenListConfig:
    url: str
    username: str
    password: str
    target_path: str

    @classmethod
    def from_env(cls):
        values = {
            "url": os.environ.get("OPENLIST_URL", "").rstrip("/"),
            "username": os.environ.get("OPENLIST_USERNAME", ""),
            "password": os.environ.get("OPENLIST_PASSWORD", ""),
            "target_path": os.environ.get("OPENLIST_TARGET_PATH", "/移动6611-加密"),
        }
        missing = [key for key in ("url", "username", "password") if not values[key]]
        if missing:
            raise OpenListError(f"OpenList 配置缺失: {', '.join(missing)}")
        values["target_path"] = "/" + values["target_path"].strip("/")
        return cls(**values)


async def _file_hashes(path: Path) -> dict[str, str]:
    def calculate():
        hashes = {name: factory() for name, factory in (
            ("md5", hashlib.md5), ("sha1", hashlib.sha1), ("sha256", hashlib.sha256)
        )}
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                for digest in hashes.values():
                    digest.update(chunk)
        return {name: digest.hexdigest() for name, digest in hashes.items()}

    return await asyncio.to_thread(calculate)


async def _file_chunks(path: Path):
    with path.open("rb") as file:
        while chunk := await asyncio.to_thread(file.read, 1024 * 1024):
            yield chunk


class OpenListUploader:
    def __init__(self, config: OpenListConfig | None = None, transport=None):
        self.config = config or OpenListConfig.from_env()
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=20),
            follow_redirects=True,
            transport=transport,
        )
        self.token = ""

    async def close(self):
        await self.client.aclose()

    async def _login(self):
        response = await self.client.post(
            f"{self.config.url}/api/auth/login",
            json={"username": self.config.username, "password": self.config.password},
        )
        payload = response.json()
        if payload.get("code") != 200 or not payload.get("data", {}).get("token"):
            raise OpenListError(f"OpenList 登录失败: {payload.get('message', response.status_code)}")
        self.token = payload["data"]["token"]

    async def _metadata(self, remote_path: str):
        if not self.token:
            await self._login()
        response = await self.client.post(
            f"{self.config.url}/api/fs/get",
            headers={"Authorization": self.token},
            json={"path": remote_path},
        )
        payload = response.json()
        if payload.get("code") == 200:
            return payload.get("data") or {}
        message = str(payload.get("message", "")).lower()
        if any(text in message for text in ("not found", "no such file", "object not found")):
            return None
        if payload.get("code") == 401:
            self.token = ""
        raise OpenListError(f"读取远端文件信息失败: {payload.get('message', response.status_code)}")

    def _dav_url(self, remote_path: str) -> str:
        return f"{self.config.url}/dav{quote(remote_path, safe='/')}"

    async def _make_dir(self, remote_path: str):
        response = await self.client.request(
            "MKCOL", self._dav_url(remote_path),
            auth=(self.config.username, self.config.password),
        )
        if response.status_code not in (200, 201, 204, 405):
            raise OpenListError(f"创建远端目录失败: HTTP {response.status_code}")

    async def _ensure_dirs(self, remote_path: str):
        current = PurePosixPath("/")
        for part in PurePosixPath(remote_path).parts[1:]:
            current /= part
            await self._make_dir(str(current))

    @staticmethod
    def _matches(metadata: dict | None, size: int, hashes: dict[str, str]):
        if not metadata or metadata.get("is_dir") or metadata.get("size") != size:
            return False, "大小不一致"
        remote_hashes = {str(key).lower(): str(value).lower()
                         for key, value in (metadata.get("hash_info") or {}).items() if value}
        common = [name for name in ("sha256", "sha1", "md5") if name in remote_hashes]
        if common:
            matched = all(remote_hashes[name] == hashes[name] for name in common)
            return matched, "哈希一致" if matched else "哈希不一致"
        return True, "大小一致（远端存储未提供哈希）"

    async def _upload_file(self, local_path: Path, remote_path: str):
        size = local_path.stat().st_size
        hashes = await _file_hashes(local_path)
        matched, detail = self._matches(await self._metadata(remote_path), size, hashes)
        if matched:
            return "skipped", detail
        response = await self.client.put(
            self._dav_url(remote_path),
            auth=(self.config.username, self.config.password),
            headers={"Content-Length": str(size)},
            content=_file_chunks(local_path),
        )
        if response.status_code not in (200, 201, 204):
            raise OpenListError(f"上传 {local_path.name} 失败: HTTP {response.status_code}")
        matched, detail = self._matches(await self._metadata(remote_path), size, hashes)
        if not matched:
            raise OpenListError(f"上传 {local_path.name} 后校验失败: {detail}")
        return "uploaded", detail

    async def upload_folder(self, local_dir: Path):
        local_dir = Path(local_dir)
        files = sorted(path for path in local_dir.rglob("*") if path.is_file())
        if not local_dir.is_dir() or not files:
            raise OpenListError(f"本地上传目录不存在或为空: {local_dir.name}")
        remote_root = f"{self.config.target_path.rstrip('/')}/{local_dir.name}"
        await self._ensure_dirs(remote_root)
        uploaded = skipped = 0
        for path in files:
            relative = path.relative_to(local_dir).as_posix()
            parent = str(PurePosixPath(remote_root, relative).parent)
            await self._ensure_dirs(parent)
            result, _ = await self._upload_file(path, f"{remote_root}/{relative}")
            uploaded += result == "uploaded"
            skipped += result == "skipped"
        await asyncio.to_thread(shutil.rmtree, local_dir)
        return {"uploaded": uploaded, "skipped": skipped, "total": len(files)}

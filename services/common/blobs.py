"""写真などのバイナリの保管先（Cloud Storage／テストはメモリ）。

置き場所は devices/{device_id}/{blob_id}.{jpg|png}。メタデータ（大きさ・sha256・撮った時刻）は Firestore 側（Store）。
バケットにはライフサイクルのルールで90日後に消す設定を付ける（deploy/cloud/01_setup.sh）。
"""
from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod

EXT = {"image/jpeg": "jpg", "image/png": "png"}


def blob_path(device_id: str, blob_id: str, content_type: str) -> str:
    return f"devices/{device_id}/{blob_id}.{EXT.get(content_type, 'bin')}"


class BlobStore(ABC):
    @abstractmethod
    async def put(self, path: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def get(self, path: str) -> bytes | None: ...


class MemoryBlobStore(BlobStore):
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, path, data, content_type):
        self.objects[path] = (data, content_type)

    async def get(self, path):
        o = self.objects.get(path)
        return o[0] if o else None


class GcsBlobStore(BlobStore):
    def __init__(self, bucket: str):
        from google.cloud import storage

        self.bucket = storage.Client().bucket(bucket)

    async def put(self, path, data, content_type):
        b = self.bucket.blob(path)
        await asyncio.to_thread(b.upload_from_string, data, content_type=content_type)

    async def get(self, path):
        from google.api_core.exceptions import NotFound

        try:
            return await asyncio.to_thread(self.bucket.blob(path).download_as_bytes)
        except NotFound:
            return None


def make_blob_store() -> BlobStore:
    bucket = os.environ.get("MEDIA_BUCKET")
    if not bucket or os.environ.get("MUGEMUGE_STORE") == "memory":
        return MemoryBlobStore()
    return GcsBlobStore(bucket)

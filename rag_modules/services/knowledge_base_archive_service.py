from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import PurePath
from typing import BinaryIO
from uuid import uuid4

import anyio

from rag_modules.api.dto.knowledge_base.settings import RetrievalConfig


class ArchiveValidationError(ValueError):
    pass


class KnowledgeBaseArchiveService:
    MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
    MAX_FILE_BYTES = 128 * 1024 * 1024
    MAX_FILES = 1000

    def __init__(self, repository, storage, *, temp_dir: str | os.PathLike | None = None):
        self.repository = repository
        self.storage = storage
        self.temp_dir = temp_dir

    async def export_dataset(self, dataset_id: str):
        dataset = await self.repository.get_active(dataset_id)
        if dataset is None:
            raise ArchiveValidationError("knowledge base not found")
        documents = await self.repository.list_all_documents(dataset_id)
        fd, filename = tempfile.mkstemp(prefix="graph-rag-export-", suffix=".zip", dir=self.temp_dir)
        os.close(fd)
        manifest_files = []
        try:
            with zipfile.ZipFile(filename, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                dataset_payload = {
                    "id": dataset.id,
                    "name": dataset.name,
                    "description": dataset.description or "",
                    "permission": dataset.permission,
                    "category": dataset.dataset_type or "通用知识",
                    "indexing_technique": dataset.indexing_technique,
                    "embedding_model": dataset.embedding_model,
                    "embedding_model_provider": dataset.embedding_model_provider,
                    "partial_user_config": dataset.partial_user_config or {},
                }
                retrieval_payload = RetrievalConfig.model_validate(dataset.retrieval_model_config or {}).model_dump()
                document_payload = []
                for document in documents:
                    info = dict(document.data_source_info or {})
                    object_key = info.get("object_key")
                    safe_name = self._safe_filename(document.name)
                    archive_path = f"files/{document.id}/{safe_name}"
                    metadata = {key: value for key, value in info.items() if key != "object_key"}
                    metadata.update({"imported_document_id": document.id})
                    document_payload.append({
                        "id": document.id, "name": document.name, "position": document.position,
                        "data_source_type": document.data_source_type, "data_source_info": metadata,
                        "created_from": document.created_from, "created_by": document.created_by,
                        "enabled": document.enabled, "archived": document.archived,
                        "archive_path": archive_path,
                    })
                    if object_key:
                        digest, size = await self._write_object(archive, archive_path, object_key)
                        manifest_files.append({"path": archive_path, "sha256": digest, "size": size})
                self._write_json(archive, "dataset.json", dataset_payload)
                self._write_json(archive, "retrieval.json", retrieval_payload)
                self._write_json(archive, "documents.json", document_payload)
                self._write_json(archive, "manifest.json", {
                    "format_version": 1,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "source_dataset_id": dataset.id,
                    "document_count": len(document_payload),
                    "files": manifest_files,
                })
            return filename
        except BaseException:
            try:
                os.unlink(filename)
            except OSError:
                pass
            raise

    async def import_dataset(self, fileobj: BinaryIO, *, actor_id: str = "current-user") -> dict:
        fileobj.seek(0, os.SEEK_END)
        archive_size = fileobj.tell()
        fileobj.seek(0)
        if archive_size > self.MAX_ARCHIVE_BYTES:
            raise ArchiveValidationError("archive exceeds size limit")
        try:
            archive = zipfile.ZipFile(fileobj)
        except zipfile.BadZipFile as exc:
            raise ArchiveValidationError("invalid archive") from exc
        with archive:
            names = archive.namelist()
            if len(names) > self.MAX_FILES + 4 or len(set(names)) != len(names):
                raise ArchiveValidationError("archive contains invalid members")
            for name in names:
                path = PurePath(name)
                if name.startswith("/") or ".." in path.parts or str(path) != name or "\\" in name:
                    raise ArchiveValidationError("archive contains an unsafe path")
            required = {"manifest.json", "dataset.json", "retrieval.json", "documents.json"}
            if not required.issubset(names):
                raise ArchiveValidationError("archive is missing required metadata")
            try:
                manifest = json.loads(archive.read("manifest.json"))
                dataset_payload = json.loads(archive.read("dataset.json"))
                retrieval_payload = RetrievalConfig.model_validate(json.loads(archive.read("retrieval.json"))).model_dump()
                document_payload = json.loads(archive.read("documents.json"))
            except (ValueError, KeyError, TypeError) as exc:
                raise ArchiveValidationError("archive metadata is invalid") from exc
            entries = {item.get("path"): item for item in manifest.get("files", [])}
            if len(document_payload) > self.MAX_FILES or manifest.get("document_count") != len(document_payload):
                raise ArchiveValidationError("archive document count is invalid")
            if len(entries) > self.MAX_FILES:
                raise ArchiveValidationError("archive contains too many files")
            dataset_id = uuid4().hex
            objects: list[str] = []
            imported_documents = []
            try:
                for item in document_payload:
                    source_path = item.get("archive_path")
                    entry = entries.get(source_path)
                    if not source_path or entry is None or source_path not in names:
                        raise ArchiveValidationError("document file is missing from archive")
                    temp = tempfile.NamedTemporaryFile(prefix="graph-rag-import-", dir=self.temp_dir, delete=False)
                    digest = hashlib.sha256()
                    size = 0
                    try:
                        with archive.open(source_path) as source:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                size += len(chunk)
                                if size > self.MAX_FILE_BYTES:
                                    raise ArchiveValidationError("document exceeds archive file size limit")
                                digest.update(chunk)
                                temp.write(chunk)
                        temp.close()
                        if size != entry.get("size") or digest.hexdigest() != entry.get("sha256"):
                            raise ArchiveValidationError("document hash does not match manifest")
                        new_document_id = uuid4().hex
                        object_key = f"datasets/{dataset_id}/documents/{new_document_id}/source"
                        with open(temp.name, "rb") as source_file:
                            await self.storage.put_stream(
                                object_key, source_file, size,
                                (item.get("data_source_info") or {}).get("content_type") or "application/octet-stream",
                            )
                        objects.append(object_key)
                        info = dict(item.get("data_source_info") or {})
                        info.update({
                            "object_key": object_key, "size": size,
                            "sha256": digest.hexdigest(),
                            "imported_document_id": item.get("id"),
                        })
                        imported_documents.append({
                            "id": new_document_id, "name": item.get("name") or "document",
                            "position": item.get("position", len(imported_documents) + 1),
                            "data_source_type": item.get("data_source_type") or "upload_file",
                            "data_source_info": info, "created_from": "import",
                            "created_by": actor_id, "enabled": bool(item.get("enabled", True)),
                            "archived": bool(item.get("archived", False)), "status": "waiting",
                        })
                    finally:
                        try:
                            os.unlink(temp.name)
                        except OSError:
                            pass
                await self.repository.create_import_records(
                    dataset_id=dataset_id, dataset=dataset_payload,
                    retrieval=retrieval_payload, documents=imported_documents, actor_id=actor_id,
                )
            except BaseException:
                for object_key in objects:
                    try:
                        await self.storage.remove_object(object_key)
                    except Exception:
                        pass
                raise
            return {"dataset_id": dataset_id, "document_count": len(imported_documents)}

    async def _write_object(self, archive: zipfile.ZipFile, archive_path: str, object_key: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with archive.open(archive_path, "w") as target:
            async with self.storage.get_stream(object_key) as source:
                while True:
                    chunk = await anyio.to_thread.run_sync(source.read, 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.MAX_FILE_BYTES:
                        raise ArchiveValidationError("document exceeds archive file size limit")
                    digest.update(chunk)
                    await anyio.to_thread.run_sync(target.write, chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _write_json(archive: zipfile.ZipFile, path: str, payload: object) -> None:
        archive.writestr(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode())

    @staticmethod
    def _safe_filename(name: str) -> str:
        candidate = PurePath(name or "document").name
        candidate = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff ]", "_", candidate).strip(" .")
        return candidate[:180] or "document"

import asyncio
import hashlib
import re
from pathlib import Path

import chromadb
from pypdf import PdfReader


class ChromaKnowledgeBase:
    def __init__(self, settings):
        self.settings = settings
        if settings.chroma_server_host:
            self.client = chromadb.HttpClient(
                host=settings.chroma_server_host, port=settings.chroma_server_port
            )
        else:
            self.client = chromadb.PersistentClient(path=settings.chroma_path)
        self.collection_name = "agriculture_documents"

    def _collection(self):
        return self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={
                "hnsw:space": "cosine",
                "description": "Uploaded agricultural and government-scheme source documents",
            },
        )

    @staticmethod
    def _chunks(text: str, size: int = 1100, overlap: int = 160) -> list[str]:
        normalized = re.sub(r"\s+", " ", text).strip()
        result = []
        cursor = 0
        while cursor < len(normalized):
            end = min(cursor + size, len(normalized))
            if end < len(normalized):
                boundary = normalized.rfind(" ", cursor + size // 2, end)
                if boundary > cursor:
                    end = boundary
            chunk = normalized[cursor:end].strip()
            if chunk:
                result.append(chunk)
            if end == len(normalized):
                break
            cursor = max(cursor + 1, end - overlap)
        return result

    @staticmethod
    def _extract(path: Path, file_type: str) -> list[tuple[int, str]]:
        if file_type == "txt":
            return [(1, path.read_text(encoding="utf-8-sig"))]
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise ValueError("Password-protected PDFs are not supported")
        return [
            (page_number, page.extract_text() or "")
            for page_number, page in enumerate(reader.pages, 1)
        ]

    async def ingest(
        self,
        path: Path,
        *,
        file_type: str,
        document_id: str,
        title: str,
        source_name: str,
        source_url: str | None,
        uploader_id: str,
    ) -> int:
        return await asyncio.to_thread(
            self._ingest, path, file_type, document_id, title, source_name, source_url, uploader_id
        )

    def _ingest(
        self,
        path: Path,
        file_type: str,
        document_id: str,
        title: str,
        source_name: str,
        source_url: str | None,
        uploader_id: str,
    ) -> int:
        page_text = self._extract(path, file_type)
        ids, docs, metadata = [], [], []
        for page_number, page in page_text:
            for chunk_number, chunk in enumerate(self._chunks(page)):
                digest = hashlib.sha256(
                    f"{document_id}:{page_number}:{chunk_number}".encode()
                ).hexdigest()[:24]
                ids.append(digest)
                docs.append(chunk)
                metadata.append(
                    {
                        "document_id": document_id,
                        "title": title,
                        "source_name": source_name,
                        "source_url": source_url or "",
                        "uploader_id": uploader_id,
                        "page": page_number,
                        "chunk": chunk_number,
                    }
                )
        if not docs:
            raise ValueError("No readable text was found in this document")
        collection = self._collection()
        collection.upsert(ids=ids, documents=docs, metadatas=metadata)
        return len(docs)

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        return await asyncio.to_thread(self._search, query, limit)

    def _search(self, query: str, limit: int) -> list[dict]:
        collection = self._collection()
        if collection.count() == 0:
            return []
        result = collection.query(
            query_texts=[query],
            n_results=min(limit, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        matches = []
        for doc, metadata, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            matches.append(
                {"text": doc, "metadata": metadata, "similarity": max(0.0, 1.0 - float(distance))}
            )
        return matches

    async def delete_document(self, document_id: str) -> None:
        await asyncio.to_thread(self._collection().delete, where={"document_id": document_id})

    async def count(self) -> int:
        return await asyncio.to_thread(self._collection().count)

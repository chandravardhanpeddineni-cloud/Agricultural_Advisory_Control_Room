import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import HttpUrl

from app.api.dependencies import admin_user
from app.db.mongodb import get_database
from app.services.blackboard import utcnow

router = APIRouter(prefix="/knowledge", tags=["knowledge base"])
logger = logging.getLogger(__name__)
ALLOWED_TYPES = {"application/pdf": "pdf", "text/plain": "txt"}


@router.get("")
async def list_documents(user=Depends(admin_user), database=Depends(get_database)):
    return (
        await database.knowledge_documents.find({}, {"_id": 0})
        .sort("created_at", -1)
        .limit(200)
        .to_list(length=200)
    )


@router.post("/upload", status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(..., min_length=2, max_length=200),
    source_name: str = Form(..., min_length=2, max_length=200),
    source_url: str | None = Form(default=None, max_length=1000),
    user=Depends(admin_user),
    database=Depends(get_database),
):
    file_type = ALLOWED_TYPES.get(file.content_type or "")
    if file_type is None and (file.filename or "").lower().endswith(".pdf"):
        file_type = "pdf"
    if file_type is None and (file.filename or "").lower().endswith(".txt"):
        file_type = "txt"
    if file_type is None:
        raise HTTPException(status_code=415, detail="Upload a PDF or plain text file")
    if source_url:
        try:
            source_url = str(HttpUrl(source_url))
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail="Source URL must be a valid HTTP(S) URL"
            ) from exc
    settings = request.app.state.settings
    data = await file.read(settings.max_upload_bytes + 1)
    await file.close()
    if not data or len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File must be non-empty and no larger than {settings.max_upload_bytes // (1024 * 1024)} MB",
        )
    if file_type == "pdf" and not data.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=415, detail="The uploaded file does not have a valid PDF signature"
        )
    if file_type == "txt":
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=415, detail="Text files must use UTF-8 encoding"
            ) from exc
    document_id = str(uuid.uuid4())
    upload_dir = Path(settings.upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{document_id}.{file_type}"
    path.write_bytes(data)
    doc = {
        "document_id": document_id,
        "title": title.strip(),
        "source_name": source_name.strip(),
        "source_url": source_url,
        "original_filename": Path(file.filename or "document").name[:200],
        "file_type": file_type,
        "size_bytes": len(data),
        "uploaded_by": str(user["_id"]),
        "status": "processing",
        "created_at": utcnow(),
        "chunk_count": 0,
        "error": None,
    }
    await database.knowledge_documents.insert_one(doc)
    try:
        count = await request.app.state.knowledge_base.ingest(
            path,
            file_type=file_type,
            document_id=document_id,
            title=doc["title"],
            source_name=doc["source_name"],
            source_url=source_url,
            uploader_id=doc["uploaded_by"],
        )
        await database.knowledge_documents.update_one(
            {"document_id": document_id}, {"$set": {"status": "ready", "chunk_count": count}}
        )
        doc.update(status="ready", chunk_count=count)
    except Exception as exc:
        logger.exception("Knowledge document indexing failed for %s", document_id)
        await database.knowledge_documents.update_one(
            {"document_id": document_id},
            {"$set": {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}},
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Document indexing failed ({type(exc).__name__}). Check that the PDF "
                "contains selectable text; server logs contain the detailed cause."
            ),
        ) from exc
    finally:
        path.unlink(missing_ok=True)
    return {key: value for key, value in doc.items() if key != "uploaded_by"}


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: str, request: Request, user=Depends(admin_user), database=Depends(get_database)
):
    result = await database.knowledge_documents.delete_one({"document_id": document_id})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    await request.app.state.knowledge_base.delete_document(document_id)

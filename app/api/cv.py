"""CV : import, consultation et téléchargement."""
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..config import settings
from ..db import kv_get, kv_set, now_iso
from .deps import MAX_CV_BYTES, cv_info, safe_filename

router = APIRouter(prefix="/api/cv", tags=["cv"])


@router.get("")
def get_cv():
    return cv_info()


@router.post("")
def upload_cv(file: UploadFile = File(...)):
    data = file.file.read(MAX_CV_BYTES + 1)
    if len(data) > MAX_CV_BYTES:
        raise HTTPException(413, "Le CV dépasse 10 Mo.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Le CV doit être un fichier PDF.")
    tmp = settings.cv_path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(settings.cv_path)  # le nouveau CV écrase l'ancien (cv_active.pdf), pas de versionnage
    name = safe_filename(file.filename or "CV.pdf")
    kv_set("cv_filename", name if name.lower().endswith(".pdf") else name + ".pdf")
    kv_set("cv_updated_at", now_iso())
    return cv_info()


@router.get("/download")
def download_cv():
    if not settings.cv_path.exists():
        raise HTTPException(404, "Aucun CV importé.")
    return FileResponse(settings.cv_path, media_type="application/pdf", filename=kv_get("cv_filename", "CV.pdf"),
                        content_disposition_type="inline")

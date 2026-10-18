"""Modèles de requête et helpers partagés par les routes."""
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .. import services
from ..config import settings
from ..db import get_db, kv_get

MAX_CV_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_BYTES = 20 * 1024 * 1024
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


# --------------------------------------------------------------------------- modèles
class CompanyIn(BaseModel):
    name: str
    website: str | None = None
    email: str | None = None
    sector: str | None = None


class CompanyPatch(BaseModel):
    name: str | None = None
    website: str | None = None
    email: str | None = None
    sector: str | None = None
    status: str | None = None


class GenerateIn(BaseModel):
    kind: str


class DraftIn(BaseModel):
    kind: str
    to: str = ""
    subject: str = ""
    body_html: str = ""


class CorrectIn(BaseModel):
    text: str = ""


class ParagraphIn(BaseModel):
    kind: str = ""
    paragraphs: list[str] = Field(min_length=1)
    index: int
    instruction: str = ""


class ReadIn(BaseModel):
    ids: list[int] | None = None


# --------------------------------------------------------------------------- helpers
def valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))


def safe_filename(name: str, default: str = "CV.pdf") -> str:
    name = Path(name or default).name
    name = re.sub(r"[^\w.\- ()àâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ]", "_", name).strip() or default
    return name


def reply_subject(subject: str | None) -> str:
    subject = (subject or "Candidature de stage").strip()
    return subject if re.match(r"^(re|réf|ref)\s*:", subject, re.I) else f"Re: {subject}"


def require_company(company_id: int) -> dict:
    company = services.get_company_dict(company_id)
    if company is None:
        raise HTTPException(404, "Entreprise introuvable.")
    return company


def last_message(company_id: int, direction: str | None = None):
    sql = "SELECT * FROM message WHERE company_id = ?"
    params: list = [company_id]
    if direction:
        sql += " AND direction = ?"
        params.append(direction)
    sql += " ORDER BY sent_at DESC LIMIT 1"
    with get_db() as db:
        return db.execute(sql, params).fetchone()


def cv_info() -> dict:
    path = settings.cv_path
    if not path.exists():
        return {"exists": False, "filename": None, "updated_at": None, "size": 0}
    updated = kv_get("cv_updated_at") or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    return {"exists": True, "filename": kv_get("cv_filename", "CV.pdf"), "updated_at": updated,
            "size": path.stat().st_size}

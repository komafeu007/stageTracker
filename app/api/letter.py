"""Lettre de motivation : rédaction (Gemini), sauvegarde du texte et export PDF à joindre au mail."""
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .. import gemini_client as ai, letter
from ..db import kv_get, kv_set
from .deps import require_company

router = APIRouter(prefix="/api/companies", tags=["letter"])


class LetterIn(BaseModel):
    subject: str = ""
    body: str = ""


def _key(company_id: int) -> str:
    return f"letter:{company_id}"


def _load(company_id: int) -> dict | None:
    raw = kv_get(_key(company_id))
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _store(company_id: int, subject: str, body: str) -> dict:
    data = {"subject": subject.strip(), "body": body.strip()}
    kv_set(_key(company_id), json.dumps(data, ensure_ascii=False))
    return data


@router.get("/{company_id}/letter")
def get_letter(company_id: int):
    require_company(company_id)
    return {"letter": _load(company_id), "sender": letter.sender_lines()}


@router.post("/{company_id}/letter")
def generate_letter(company_id: int):
    company = require_company(company_id)
    context = ai.fetch_company_context(company["website"])
    try:
        subject, body = ai.draft_cover_letter(company, context)
    except ai.AIQuotaError as exc:
        raise HTTPException(429, str(exc)) from exc
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"letter": _store(company_id, subject, body), "sender": letter.sender_lines()}


@router.put("/{company_id}/letter")
def save_letter(company_id: int, body: LetterIn):
    require_company(company_id)
    return {"letter": _store(company_id, body.subject, body.body)}


@router.post("/{company_id}/letter/pdf")
def letter_pdf(company_id: int, body: LetterIn):
    """Enregistre le texte fourni puis renvoie la lettre en PDF (à joindre au mail ou télécharger)."""
    company = require_company(company_id)
    if not body.body.strip():
        raise HTTPException(400, "La lettre est vide : génère-la ou écris-la d'abord.")
    _store(company_id, body.subject, body.body)
    pdf = letter.build_pdf(company, body.subject or ai.DEFAULT_LETTER_SUBJECT, body.body)
    name = letter.filename(company["name"])
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})

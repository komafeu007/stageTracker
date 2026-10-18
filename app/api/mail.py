"""Brouillons (rédaction Gemini) et envoi manuel des e-mails."""
import logging
import mimetypes

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from googleapiclient.errors import HttpError

from .. import gemini_client as ai, gmail_client as gm, services, sync, textutils as tu
from ..config import settings
from ..constants import APPLICATION, DRAFT_KINDS, FOLLOWUP, REPLY
from ..db import get_db, kv_get, now_iso, parse_iso, utcnow
from .deps import (
    MAX_ATTACHMENTS_BYTES,
    CorrectIn,
    DraftIn,
    GenerateIn,
    ParagraphIn,
    last_message,
    reply_subject,
    require_company,
    safe_filename,
    valid_email,
)

log = logging.getLogger("stage-tracker")
router = APIRouter(prefix="/api/companies", tags=["mail"])


def _raise_ai_error(exc: ai.AIError) -> None:
    """Traduit une erreur Gemini en réponse HTTP adaptée."""
    if isinstance(exc, ai.AIQuotaError):
        raise HTTPException(429, str(exc)) from exc
    raise HTTPException(502, str(exc)) from exc


@router.post("/{company_id}/draft")
def generate_draft(company_id: int, body: GenerateIn):
    company = require_company(company_id)
    kind = body.kind
    if kind not in DRAFT_KINDS:
        raise HTTPException(400, "Type de brouillon inconnu.")

    if kind == APPLICATION:
        context = ai.fetch_company_context(company["website"])
        try:
            subject, text = ai.draft_application(company, context)
        except ai.AIError as exc:
            _raise_ai_error(exc)
        to = company["email"] or ""

    else:  # followup | reply
        to = services.counterpart_address(company_id, company["email"])
        if kind == FOLLOWUP:
            last = last_message(company_id)
            if last is None:
                raise HTTPException(400, "Aucun échange avec cette entreprise : rien à relancer.")
            last_sent = last_message(company_id, "out")
            if last_sent is None:
                raise HTTPException(400, "Aucun e-mail envoyé à cette entreprise : rien à relancer.")
            subject = reply_subject(last["subject"])
            with get_db() as db:
                anchor = db.execute("SELECT anchor_at FROM company WHERE id = ?", (company_id,)).fetchone()["anchor_at"]
            anchor_dt = parse_iso(anchor) or parse_iso(last_sent["sent_at"])
            days = max(0, (utcnow() - anchor_dt).days)
            number = company["followup_count"] + 1
            label = services.followup_label(company["followup_count"]) or "relance supplémentaire"
            try:
                text = ai.draft_followup(company, dict(last_sent), number, label, days)
            except ai.AIError as exc:
                _raise_ai_error(exc)
        else:  # REPLY : on répond au dernier message reçu
            last_in = last_message(company_id, "in")
            if last_in is None:
                raise HTTPException(400, "Aucun message reçu de cette entreprise : rien auquel répondre.")
            subject = reply_subject(last_in["subject"])
            try:
                text = ai.draft_reply(company, dict(last_in))
            except ai.AIError as exc:
                _raise_ai_error(exc)

    body_html = tu.text_to_html(text)
    if not body_html:
        raise HTTPException(502, "L'IA n'a produit aucun texte. Réessaie.")
    services.save_draft(company_id, kind, to, subject, body_html)
    return {"kind": kind, "to": to, "subject": subject, "body_html": body_html}


@router.put("/{company_id}/draft")
def save_draft(company_id: int, body: DraftIn):
    require_company(company_id)
    if body.kind not in DRAFT_KINDS:
        raise HTTPException(400, "Type de brouillon inconnu.")
    services.save_draft(company_id, body.kind, body.to, body.subject, body.body_html)
    return {"ok": True}


@router.post("/{company_id}/draft/correct")
def correct_draft(company_id: int, body: CorrectIn):
    """Corrige uniquement l'orthographe du texte fourni, sans le reformuler ni en changer le sens."""
    require_company(company_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Le message est vide : rien à corriger.")
    try:
        corrected = ai.correct_spelling(text)
    except ai.AIError as exc:
        _raise_ai_error(exc)
    return {"body_html": tu.text_to_html(corrected)}


@router.post("/{company_id}/draft/paragraph")
def regenerate_paragraph(company_id: int, body: ParagraphIn):
    """Réécrit un seul paragraphe du brouillon (les autres ne bougent pas)."""
    company = require_company(company_id)
    if not 0 <= body.index < len(body.paragraphs):
        raise HTTPException(400, "Paragraphe introuvable.")
    context = ai.fetch_company_context(company["website"]) if body.kind == APPLICATION else ""
    try:
        text = ai.draft_paragraph(company, context, body.paragraphs, body.index, body.instruction)
    except ai.AIError as exc:
        _raise_ai_error(exc)
    return {"paragraph_html": tu.text_to_html(text)}


@router.delete("/{company_id}/draft")
def delete_draft(company_id: int):
    services.delete_draft(company_id)
    return {"ok": True}


@router.post("/{company_id}/send")
def send_email(
    company_id: int,
    to: str = Form(...),
    subject: str = Form(...),
    body_html: str = Form(...),
    kind: str = Form(APPLICATION),
    include_cv: bool = Form(True),
    files: list[UploadFile] = File(default=[]),
):
    company = require_company(company_id)
    to = to.strip()
    subject = " ".join(subject.split())
    if not valid_email(to):
        raise HTTPException(400, "Adresse du destinataire invalide.")
    if not subject:
        raise HTTPException(400, "L'objet est obligatoire.")
    if not tu.html_to_text(body_html, drop_quotes=False):
        raise HTTPException(400, "Le message est vide.")
    if kind not in DRAFT_KINDS:
        raise HTTPException(400, "Type d'envoi inconnu.")

    attachments: list[tuple[str, bytes, str]] = []
    if include_cv:
        if not settings.cv_path.exists():
            raise HTTPException(400, "Aucun CV importé : importe ton CV (bandeau du bas) ou décoche « Joindre le CV ».")
        attachments.append((kv_get("cv_filename", "CV.pdf"), settings.cv_path.read_bytes(), "application/pdf"))
    total = sum(len(a[1]) for a in attachments)
    for upload in files:
        if not upload.filename:
            continue
        # Lecture bornée : on ne charge pas 2 Go en mémoire avant de refuser la pièce jointe.
        data = upload.file.read(MAX_ATTACHMENTS_BYTES + 1)
        if len(data) > MAX_ATTACHMENTS_BYTES or total + len(data) > MAX_ATTACHMENTS_BYTES:
            raise HTTPException(413, "Pièces jointes trop lourdes (20 Mo maximum au total).")
        total += len(data)
        ctype = upload.content_type or mimetypes.guess_type(upload.filename)[0] or "application/octet-stream"
        attachments.append((safe_filename(upload.filename, "piece-jointe"), data, ctype))

    thread_id = in_reply_to = None
    if kind in (FOLLOWUP, REPLY):
        last = last_message(company_id)
        if last is not None:
            thread_id, in_reply_to = last["thread_id"], last["rfc_id"] or None

    try:
        service = gm.get_service()
        raw = gm.build_raw(to, subject, tu.build_email_html(body_html), attachments, in_reply_to)
        sent = gm.send_raw(service, raw, thread_id)
    except gm.NotConnectedError as exc:
        raise HTTPException(401, str(exc)) from exc
    except HttpError as exc:
        raise HTTPException(502, f"Gmail a refusé l'envoi : {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, f"En-tête invalide : {exc}") from exc

    # Le mail est parti : on enregistre tout de suite le fil, puis on le relit pour mettre à jour le statut.
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO thread(id, company_id, subject, created_at) VALUES (?, ?, ?, ?)",
                   (sent["threadId"], company_id, subject, now_iso()))
        if not company["email"]:
            db.execute("UPDATE company SET email = ? WHERE id = ?", (to.lower(), company_id))
    services.delete_draft(company_id)
    try:
        me = kv_get("my_email") or gm.get_profile(service)["emailAddress"].lower()
        sync.process_thread(service, sent["threadId"], me, notify=False, force_company_id=company_id)
    except Exception:  # noqa: BLE001 - la synchro périodique rattrapera
        log.exception("Mail envoyé, mais mise à jour immédiate du fil impossible")
    return {"ok": True, "company": services.get_company_dict(company_id)}

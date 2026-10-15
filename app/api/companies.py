"""Entreprises : liste, création, édition, corbeille et fil de discussion."""
from fastapi import APIRouter, HTTPException

from .. import services
from ..constants import FORCEABLE_STATUSES, IGNORED, PROSPECT
from ..db import get_db
from .deps import CompanyIn, CompanyPatch, require_company, valid_email

router = APIRouter(prefix="/api/companies", tags=["companies"])


@router.get("")
def list_companies():
    return services.list_companies()


@router.post("")
def add_company(body: CompanyIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Le nom de l'entreprise est obligatoire.")
    email = (body.email or "").strip()
    if email and not valid_email(email):
        raise HTTPException(400, "Adresse e-mail invalide.")
    with get_db() as db:
        dup = db.execute("SELECT status FROM company WHERE lower(name) = lower(?)", (name,)).fetchone()
        if dup:
            where = " (elle est dans la corbeille : restaure-la)" if dup["status"] == IGNORED else ""
            raise HTTPException(409, f"Cette entreprise existe déjà{where}.")
        company_id = services.create_company(db, name, body.website, email)
    return services.get_company_dict(company_id)


@router.patch("/{company_id}")
def patch_company(company_id: int, body: CompanyPatch):
    fields = body.model_dump(exclude_unset=True)
    with get_db() as db:
        row = db.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Entreprise introuvable.")
        name, website, email = row["name"], row["website"], row["email"]
        status, manual = row["status"], row["status_manual"]

        if "name" in fields:
            name = (fields["name"] or "").strip()
            if not name:
                raise HTTPException(400, "Le nom ne peut pas être vide.")
        if "website" in fields:
            website = (fields["website"] or "").strip() or None
        if "email" in fields:
            email = (fields["email"] or "").strip().lower() or None
            if email and not valid_email(email):
                raise HTTPException(400, "Adresse e-mail invalide.")
        if "status" in fields:
            if fields["status"] not in FORCEABLE_STATUSES:
                raise HTTPException(400, "Statut inconnu.")
            status, manual = fields["status"], 1  # contrôle manuel : forcé jusqu'au prochain message

        db.execute(
            "UPDATE company SET name = ?, website = ?, email = ?, domain = ?, status = ?, status_manual = ? "
            "WHERE id = ?",
            (name, website, email, services.compute_domain(website, email), status, manual, company_id),
        )
    return services.get_company_dict(company_id)


@router.post("/{company_id}/ignore")
def ignore_company(company_id: int):
    require_company(company_id)
    with get_db() as db:
        db.execute("UPDATE company SET status = ?, status_manual = 0 WHERE id = ?", (IGNORED, company_id))
        db.execute("DELETE FROM draft WHERE company_id = ?", (company_id,))
    return {"ok": True}


@router.post("/{company_id}/restore")
def restore_company(company_id: int):
    require_company(company_id)
    with get_db() as db:
        db.execute("UPDATE company SET status = ?, status_manual = 0 WHERE id = ?", (PROSPECT, company_id))
        services.recompute_company(db, company_id, new_activity=True)
    return services.get_company_dict(company_id)


@router.get("/{company_id}/thread")
def company_thread(company_id: int):
    company = require_company(company_id)
    with get_db() as db:
        rows = db.execute("SELECT * FROM message WHERE company_id = ? ORDER BY sent_at", (company_id,)).fetchall()
        db.execute("UPDATE notification SET is_read = 1 WHERE company_id = ? AND type = 'reply'", (company_id,))
    messages = [
        {"id": r["id"], "direction": r["direction"], "sender": r["sender"], "recipients": r["recipients"],
         "subject": r["subject"], "body": r["body"], "sent_at": r["sent_at"],
         "has_attachments": bool(r["has_attachments"])}
        for r in rows
    ]
    return {"company": company, "messages": messages, "draft": services.get_draft(company_id)}

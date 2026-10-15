"""Réaffectation manuelle : changer l'entreprise d'un ou plusieurs messages.

Le classement automatique (mot-clé + IA) n'est jamais parfait ; ces routes permettent de
corriger à la main, sans repasser par Gmail. Déplacer un mail ne l'envoie nulle part : on ne
touche qu'aux lignes SQLite locales.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import services
from ..constants import IGNORED, PROSPECT
from ..db import get_db

router = APIRouter(prefix="/api/messages", tags=["messages"])


class MoveIn(BaseModel):
    message_ids: list[str] = Field(min_length=1)
    company_id: int


@router.post("/move")
def move_messages(body: MoveIn):
    """Déplace des messages (un mail ou toute une conversation) vers une autre entreprise."""
    with get_db() as db:
        target = db.execute("SELECT id, status FROM company WHERE id = ?", (body.company_id,)).fetchone()
        if target is None:
            raise HTTPException(404, "Entreprise cible introuvable.")
        # Une entreprise rangée dans la corbeille ressort de la corbeille si on y range un mail.
        if target["status"] == IGNORED:
            db.execute("UPDATE company SET status = ?, status_manual = 0 WHERE id = ?",
                       (PROSPECT, body.company_id))

        marks = ",".join("?" for _ in body.message_ids)
        rows = db.execute(
            f"SELECT id, thread_id, company_id FROM message WHERE id IN ({marks})",
            body.message_ids,
        ).fetchall()
        if not rows:
            raise HTTPException(404, "Ces messages n'existent plus.")

        thread_ids = {r["thread_id"] for r in rows}
        sources = {r["company_id"] for r in rows if r["company_id"] != body.company_id}

        db.execute(f"UPDATE message SET company_id = ? WHERE id IN ({marks})",
                   [body.company_id, *body.message_ids])

        # Un fil dont plus aucun message ne reste dans une autre entreprise suit le transfert,
        # sinon les prochains mails de ce fil repartiraient dans l'ancienne entreprise.
        for thread_id in thread_ids:
            rest = db.execute("SELECT COUNT(*) AS n FROM message WHERE thread_id = ? AND company_id != ?",
                              (thread_id, body.company_id)).fetchone()["n"]
            if not rest:
                db.execute("UPDATE thread SET company_id = ? WHERE id = ?", (body.company_id, thread_id))

        services.recompute_company(db, body.company_id, new_activity=True)
        for company_id in sources:
            left = db.execute("SELECT COUNT(*) AS n FROM message WHERE company_id = ?",
                              (company_id,)).fetchone()["n"]
            if left:
                services.recompute_company(db, company_id, new_activity=True)
            else:
                # Plus aucun message : l'entreprise devenue vide part à la corbeille (réversible).
                db.execute("UPDATE company SET status = ?, status_manual = 0 WHERE id = ?",
                           (IGNORED, company_id))
                db.execute("DELETE FROM draft WHERE company_id = ?", (company_id,))

    return {"ok": True, "moved": len(rows), "company": services.get_company_dict(body.company_id)}

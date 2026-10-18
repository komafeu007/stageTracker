"""Logique métier : statuts, relances (J+7 / J+14), notifications, brouillons, jauge de tokens."""
from datetime import datetime, timedelta

from . import textutils as tu
from .config import settings
from .constants import (
    FOLLOWUP,
    IGNORED,
    OUTCOME_REFUSAL,
    PENDING,
    PROSPECT,
    REFUSED,
    REPLIED,
)
from .db import get_db, iso, kv_get, now_iso, parse_iso, utcnow

# Ordre d'affichage de la liste « Candidatures » : ce qui demande une action d'abord
ACTIVE_ORDER = {FOLLOWUP: 0, REPLIED: 1, PENDING: 2, REFUSED: 3}


# --------------------------------------------------------------------------- relances
def followup_label(count: int) -> str | None:
    days = settings.followup_days
    return f"J+{days[count]}" if 0 <= count < len(days) else None


def compute_due(row) -> datetime | None:
    """Date de la prochaine relance : dernier envoi + FOLLOWUP_DAYS[n], ou None.

    Le compteur part du dernier mail envoyé (et non de la candidature) : une relance envoyée en
    retard ne doit pas faire apparaître aussitôt la suivante comme déjà due.
    """
    if row["status"] != PENDING or not row["anchor_at"]:
        return None
    count = row["followup_count"]
    if count >= len(settings.followup_days):
        return None
    anchor = parse_iso(row["anchor_at"])
    if utcnow() - anchor > timedelta(days=settings.followup_max_age_days):
        return None  # candidature trop ancienne : on ne harcèle plus
    return anchor + timedelta(days=settings.followup_days[count])


def company_to_dict(row) -> dict:
    due = compute_due(row)
    overdue = bool(due and due <= utcnow())
    status = FOLLOWUP if (row["status"] == PENDING and overdue) else row["status"]
    return {
        "id": row["id"],
        "name": row["name"],
        "website": row["website"],
        "email": row["email"],
        "sector": row["sector"],
        "status": status,                 # statut affiché
        "stored_status": row["status"],   # statut en base
        "status_manual": bool(row["status_manual"]),
        "followup_count": row["followup_count"],
        "due_at": iso(due) if due else None,
        "due_label": followup_label(row["followup_count"]) if due else None,
        "last_message_at": row["last_message_at"],
        "last_direction": row["last_direction"],
        "summary": row["summary"],
    }


def list_companies() -> dict:
    with get_db() as db:
        rows = db.execute("SELECT * FROM company").fetchall()
    active, prospects, trash = [], [], []
    for row in rows:
        data = company_to_dict(row)
        if row["status"] == IGNORED:
            trash.append(data)
        elif row["status"] == PROSPECT:
            prospects.append(data)
        else:
            active.append(data)

    def ts(d):
        dt = parse_iso(d["last_message_at"])
        return dt.timestamp() if dt else 0

    active.sort(key=lambda d: (ACTIVE_ORDER.get(d["status"], 9), -ts(d)))
    prospects.sort(key=lambda d: d["name"].lower())
    trash.sort(key=lambda d: d["name"].lower())
    return {"active": active, "prospects": prospects, "trash": trash}


def get_company_dict(company_id: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
    return company_to_dict(row) if row else None


def next_followup() -> dict | None:
    """Relance la plus urgente (en retard en priorité), pour le bandeau du haut."""
    with get_db() as db:
        rows = db.execute("SELECT * FROM company WHERE status = ?", (PENDING,)).fetchall()
    best = None
    for row in rows:
        due = compute_due(row)
        if due and (best is None or due < best[0]):
            best = (due, row)
    if best is None:
        return None
    due, row = best
    return {
        "company_id": row["id"],
        "name": row["name"],
        "due_at": iso(due),
        "label": followup_label(row["followup_count"]),
        "overdue": due <= utcnow(),
    }


def check_followups() -> None:
    """Recalcule les échéances, notifie chaque relance due (une seule fois) et purge les obsolètes."""
    recompute_followups()
    now = utcnow()
    valid: list[str] = []
    with get_db() as db:
        for row in db.execute("SELECT * FROM company WHERE status = ?", (PENDING,)).fetchall():
            due = compute_due(row)
            if not due or due > now:
                continue
            key = f"followup:{row['id']}:{row['followup_count']}:{row['anchor_at']}"
            valid.append(key)
            add_notification(
                db, "followup", row["id"],
                f"Relance {followup_label(row['followup_count'])} à envoyer à {row['name']}",
                key,
            )
        # Une relance notifiée puis envoyée (ou repoussée) ne doit plus traîner dans le journal.
        marks = ",".join("?" for _ in valid) or "''"
        db.execute(
            f"DELETE FROM notification WHERE type = 'followup' AND dedupe_key NOT IN ({marks})", valid
        )


def recompute_followups() -> int:
    """Recalcule statut et échéance de relance de toutes les entreprises.

    L'échéance ne dépend que des dates des mails déjà en base : le simple passage du temps doit
    donc suffire à la faire évoluer, sans attendre qu'un nouveau mail arrive. Appelé à chaque
    synchro (et à chaque tour de la boucle de fond) pour que les relances restent justes.
    """
    with get_db() as db:
        rows = db.execute("SELECT id FROM company WHERE status != ?", (IGNORED,)).fetchall()
        for row in rows:
            recompute_company(db, row["id"])
    return len(rows)


# --------------------------------------------------------------------------- recalcul du statut
def recompute_company(db, company_id: int, new_activity: bool = False) -> None:
    """Recalcule statut, compteur de relances et ancre à partir des messages en base."""
    company = db.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
    if company is None or company["status"] == IGNORED:
        return
    messages = db.execute(
        "SELECT direction, sent_at FROM message WHERE company_id = ? ORDER BY sent_at", (company_id,)
    ).fetchall()
    if not messages:
        return

    last = messages[-1]
    trailing = []
    for m in reversed(messages):
        if m["direction"] != "out":
            break
        trailing.append(m)
    # Départ du décompte : le dernier mail que NOUS avons envoyé (trailing[0] = le plus récent).
    # Prendre le 1er envoi de la série ferait passer la relance suivante pour déjà due dès qu'une
    # relance part en retard.
    anchor = trailing[0]["sent_at"] if trailing else None
    followups = max(0, len(trailing) - 1)                    # envois suivants = relances faites

    manual = 0 if new_activity else company["status_manual"]  # un nouveau message annule le statut forcé
    ai_label = None if last["direction"] == "out" else company["ai_label"]
    status = company["status"]
    if not manual:
        if last["direction"] == "out":
            status = PENDING
        else:
            status = REFUSED if ai_label == OUTCOME_REFUSAL else REPLIED

    db.execute(
        "UPDATE company SET status = ?, status_manual = ?, ai_label = ?, followup_count = ?, "
        "anchor_at = ?, last_message_at = ?, last_direction = ? WHERE id = ?",
        (status, manual, ai_label, followups, anchor, last["sent_at"], last["direction"], company_id),
    )


# --------------------------------------------------------------------------- entreprises
def compute_domain(website: str | None, email: str | None) -> str | None:
    """Domaine servant à rattacher les futurs mails : site web, sinon domaine pro de l'e-mail."""
    domain = tu.domain_of_url(website)
    if domain:
        return domain
    email_domain = tu.domain_of_email(email)
    return None if tu.is_generic_domain(email_domain) else email_domain


def create_company(db, name: str, website: str | None, email: str | None,
                   status: str = PROSPECT, summary: str | None = None,
                   sector: str | None = None) -> int:
    email = (email or "").strip().lower() or None
    website = (website or "").strip() or None
    sector = (sector or "").strip() or None
    domain = compute_domain(website, email)
    cur = db.execute(
        "INSERT INTO company(name, website, email, domain, sector, status, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name.strip(), website, email, domain, sector, status, summary, now_iso()),
    )
    return cur.lastrowid


def find_company_for_address(db, address: str):
    """Retrouve une entreprise via l'adresse exacte, sinon via le domaine (hors Gmail/Outlook...)."""
    row = db.execute("SELECT * FROM company WHERE lower(email) = ? ORDER BY (status = ?) LIMIT 1",
                     (address.lower(), IGNORED)).fetchone()
    if row:
        return row
    domain = tu.domain_of_email(address)
    if tu.is_generic_domain(domain):
        return None
    return db.execute("SELECT * FROM company WHERE domain = ? ORDER BY (status = ?), id LIMIT 1",
                      (domain, IGNORED)).fetchone()


def counterpart_address(company_id: int, fallback: str | None) -> str:
    """Adresse de l'interlocuteur : expéditeur du dernier mail reçu, ou destinataire du dernier envoyé."""
    me = (kv_get("my_email", "") or "").lower()
    with get_db() as db:
        last = db.execute(
            "SELECT direction, sender, recipients FROM message WHERE company_id = ? "
            "ORDER BY sent_at DESC LIMIT 1", (company_id,)
        ).fetchone()
    if last:
        if last["direction"] == "in":
            found = tu.parse_addresses(last["sender"])
            if found:
                return found[0][1]
        else:
            for _, addr in tu.parse_addresses(last["recipients"]):
                if addr != me:
                    return addr
    return fallback or ""


# --------------------------------------------------------------------------- notifications
def add_notification(db, ntype: str, company_id: int, message: str, key: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO notification(type, company_id, message, dedupe_key, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ntype, company_id, message, key, now_iso()),
    )


def list_notifications(limit: int = 50) -> dict:
    with get_db() as db:
        rows = db.execute(
            "SELECT n.* FROM notification n JOIN company c ON c.id = n.company_id "
            "WHERE c.status != ? ORDER BY n.created_at DESC, n.id DESC LIMIT ?", (IGNORED, limit)
        ).fetchall()
        unread = db.execute(
            "SELECT COUNT(*) AS n FROM notification n JOIN company c ON c.id = n.company_id "
            "WHERE n.is_read = 0 AND c.status != ?", (IGNORED,)
        ).fetchone()["n"]
    items = [
        {"id": r["id"], "type": r["type"], "company_id": r["company_id"], "message": r["message"],
         "is_read": bool(r["is_read"]), "created_at": r["created_at"]}
        for r in rows
    ]
    return {"unread": unread, "items": items}


def mark_notifications_read(ids: list[int] | None) -> None:
    with get_db() as db:
        if ids:
            marks = ",".join("?" for _ in ids)
            db.execute(f"UPDATE notification SET is_read = 1 WHERE id IN ({marks})", ids)
        else:
            db.execute("UPDATE notification SET is_read = 1")


# --------------------------------------------------------------------------- brouillons
def get_draft(company_id: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM draft WHERE company_id = ?", (company_id,)).fetchone()
    if not row:
        return None
    return {"kind": row["kind"], "to": row["to_addr"] or "", "subject": row["subject"] or "",
            "body_html": row["body_html"] or "", "updated_at": row["updated_at"]}


def save_draft(company_id: int, kind: str, to: str, subject: str, body_html: str) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO draft(company_id, kind, to_addr, subject, body_html, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(company_id) DO UPDATE SET "
            "kind = excluded.kind, to_addr = excluded.to_addr, subject = excluded.subject, "
            "body_html = excluded.body_html, updated_at = excluded.updated_at",
            (company_id, kind, to, subject, body_html, now_iso()),
        )


def delete_draft(company_id: int) -> None:
    with get_db() as db:
        db.execute("DELETE FROM draft WHERE company_id = ?", (company_id,))


# --------------------------------------------------------------------------- tokens IA
def token_stats() -> dict:
    now = utcnow()
    month_start = now.strftime("%Y-%m-01T00:00:00")
    day_start = now.strftime("%Y-%m-%dT00:00:00")
    with get_db() as db:
        month = db.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t, COUNT(*) AS n FROM token_log WHERE ts >= ?",
            (month_start,),
        ).fetchone()
        today = db.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t, COUNT(*) AS n FROM token_log WHERE ts >= ?",
            (day_start,),
        ).fetchone()
    budget = settings.token_budget
    return {
        "month_tokens": month["t"],
        "month_requests": month["n"],
        "today_tokens": today["t"],
        "today_requests": today["n"],
        "budget": budget,
        "remaining": max(0, budget - month["t"]),
        "percent": round(100 * month["t"] / budget, 1) if budget else 0,
        "model": settings.gemini_model,
    }

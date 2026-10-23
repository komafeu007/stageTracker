"""Synchronisation Gmail -> SQLite.

* 1er lancement : balayage complet (filtré par mots-clés côté Gmail) de l'historique.
* Lancements suivants : synchro delta via l'historique Gmail (history.list).
* Pré-filtrage local par mots-clés, puis classification par l'IA locale (Ollama)
  uniquement pour les fils inconnus et pertinents.
"""
import logging
import threading

from googleapiclient.errors import HttpError

from . import gmail_client as gm, local_ai, services, textutils as tu
from .config import settings
from .constants import IGNORED, PENDING
from .db import get_db, kv_get, kv_set, now_iso

log = logging.getLogger("stage-tracker.sync")

REAUTH_KEY = "gmail_reauth_required"

state: dict = {
    "running": False, "mode": None, "done": 0, "total": 0,
    "last_run": None, "last_error": None, "needs_reauth": False,
}
_lock = threading.Lock()


def run_sync(force_full: bool = False) -> bool:
    """Lance une synchro. Retourne False si une synchro est déjà en cours."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        state.update(running=True, mode=None, done=0, total=0, last_error=None)
        try:
            _run(force_full)
            state["needs_reauth"] = False
            kv_set(REAUTH_KEY, "0")
        except gm.NotConnectedError as exc:
            state["last_error"] = str(exc)
            state["needs_reauth"] = True
            # Persisté : sinon, après un redémarrage, l'UI croit encore Gmail connecté
            # (token.json existe toujours) et n'affiche pas le bouton « Reconnecter Gmail ».
            kv_set(REAUTH_KEY, "1")
        except Exception as exc:  # noqa: BLE001 - on affiche l'erreur à l'utilisateur
            log.exception("Erreur de synchronisation")
            state["last_error"] = f"Erreur de synchronisation : {exc}"
        state["last_run"] = now_iso()
        return True
    finally:
        state["running"] = False
        _lock.release()


def _run(force_full: bool) -> None:
    service = gm.get_service()
    profile = gm.get_profile(service)
    me = profile["emailAddress"].lower()
    kv_set("my_email", me)

    history_id = kv_get("history_id")
    initial_done = kv_get("initial_scan_done") == "1"

    if force_full or not initial_done or not history_id:
        _full_scan(service, me, str(profile["historyId"]))
    else:
        try:
            _delta_sync(service, me, history_id)
        except gm.HistoryExpiredError:
            log.warning("historyId expiré : scan complet de rattrapage")
            _full_scan(service, me, str(profile["historyId"]))
    services.check_followups()
    backfill_sectors()


def backfill_sectors(limit: int = 200) -> int:
    """Complète le secteur des entreprises qui n'en ont pas, d'après leurs mails déjà en base.

    Utile pour les entreprises importées avant l'existence du champ « secteur ». Ne touche jamais
    un secteur déjà renseigné (ni par l'IA, ni par l'utilisateur). Retourne le nombre complété.
    """
    with get_db() as db:
        companies = db.execute(
            "SELECT id, name FROM company WHERE (sector IS NULL OR sector = '') AND status != ? "
            "ORDER BY id LIMIT ?", (IGNORED, limit),
        ).fetchall()
    filled = 0
    for company in companies:
        with get_db() as db:
            rows = db.execute(
                "SELECT direction, sent_at, subject, body FROM message WHERE company_id = ? "
                "ORDER BY sent_at DESC LIMIT 6", (company["id"],),
            ).fetchall()
        if not rows:
            continue  # rien à lire (entreprise ajoutée à la main, jamais contactée)
        msgs = [dict(row) for row in reversed(rows)]
        subject = msgs[0].get("subject") or company["name"]
        classification = None
        if local_ai.enabled():
            try:
                classification = local_ai.classify_thread(subject, msgs)
            except local_ai.LocalAIError as exc:
                log.warning("Secteur : classification impossible pour %s : %s", company["name"], exc)
        sector = _sector_from(classification, subject, msgs)
        if sector:
            with get_db() as db:
                db.execute("UPDATE company SET sector = ? WHERE id = ? AND (sector IS NULL OR sector = '')",
                           (sector, company["id"]))
            filled += 1
    return filled


def _full_scan(service, me: str, start_history_id: str) -> None:
    state["mode"] = "full"
    notify = kv_get("initial_scan_done") == "1"  # pas de notifications pour le tout 1er import
    thread_ids = gm.list_thread_ids(service, gm.build_query(settings.keywords), settings.scan_max_threads)

    with get_db() as db:
        existing = {row["id"] for row in db.execute("SELECT id FROM thread").fetchall()}
    new_thread_ids = [tid for tid in thread_ids if tid not in existing]

    state["total"] = len(new_thread_ids)
    for i, thread_id in enumerate(new_thread_ids, 1):
        _safe_process(service, thread_id, me, notify)
        state["done"] = i
    # historyId pris AVANT le scan : rien ne se perd si un mail arrive pendant l'import
    kv_set("history_id", start_history_id)
    kv_set("initial_scan_done", "1")


def _delta_sync(service, me: str, start_history_id: str) -> None:
    state["mode"] = "delta"
    threads, latest = gm.history_threads(service, start_history_id)
    state["total"] = len(threads)
    for i, (thread_id, labels) in enumerate(threads.items(), 1):
        with get_db() as db:
            known = db.execute("SELECT 1 FROM thread WHERE id = ?", (thread_id,)).fetchone() is not None
        if known or not (labels & gm.SKIP_LABELS):
            _safe_process(service, thread_id, me, notify=True)
        state["done"] = i
    kv_set("history_id", latest)


def _safe_process(service, thread_id: str, me: str, notify: bool) -> None:
    try:
        process_thread(service, thread_id, me, notify)
    except HttpError as exc:
        if getattr(exc.resp, "status", None) in (401, 403):
            raise  # problème d'autorisation : on remonte pour déclencher la reconnexion
        log.warning("Fil %s ignoré (erreur Gmail %s)", thread_id, getattr(exc.resp, "status", "?"))


# --------------------------------------------------------------------------- traitement d'un fil
def process_thread(service, thread_id: str, me: str, notify: bool,
                   force_company_id: int | None = None) -> None:
    thread = gm.get_thread(service, thread_id)
    msgs = [gm.parse_message(m, me) for m in thread.get("messages", [])]
    msgs = [m for m in msgs if "DRAFT" not in m["labels"]]
    if not msgs:
        return
    msgs.sort(key=lambda m: m["sent_at"])
    subject = msgs[0]["subject"]

    with get_db() as db:
        known = db.execute("SELECT company_id FROM thread WHERE id = ?", (thread_id,)).fetchone()

    company_id = force_company_id or (known["company_id"] if known else None)
    analysis = None
    if company_id is None:
        if known is not None:
            return
        company_id, analysis = _resolve_new_thread(thread_id, msgs, subject, me)
        if company_id is None:
            return
    _store_messages(thread_id, company_id, subject, msgs, notify, analysis)


def _is_system_address(addr: str) -> bool:
    local = addr.split("@", 1)[0]
    return local in {"mailer-daemon", "postmaster"}


def _is_noreply(addr: str) -> bool:
    local = addr.split("@", 1)[0]
    return "noreply" in local or "no-reply" in local or "donotreply" in local


def _external_addresses(msgs: list[dict], me: str) -> list[str]:
    """Interlocuteurs (hors moi), l'autre partie du premier message en tête."""
    seen: set[str] = set()
    ordered: list[str] = []
    for m in msgs:
        if m["direction"] == "in":
            counterpart = [m["from_addr"]]
        else:
            counterpart = [addr for _, addr in tu.parse_addresses(m["recipients"])]
        for addr in counterpart + [addr for _, addr in m["participants"]]:
            if addr and addr != me and addr not in seen and not _is_system_address(addr):
                seen.add(addr)
                ordered.append(addr)
    return ordered


def _mark_skipped(thread_id: str, count: int) -> None:
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO skipped_thread(thread_id, msg_count) VALUES (?, ?)", (thread_id, count))


def _default_company_name(external: list[str]) -> str:
    primary = next((a for a in external if not _is_noreply(a)), external[0])
    domain = tu.domain_of_email(primary)
    return tu.name_from_domain(domain) if not tu.is_generic_domain(domain) else primary.split("@")[0].title()


def _find_or_create_company(db, name: str, email: str, summary: str | None,
                            sector: str | None = None) -> int | None:
    existing = db.execute("SELECT id, status FROM company WHERE lower(name) = lower(?)", (name,)).fetchone()
    if existing:
        return None if existing["status"] == IGNORED else existing["id"]
    return services.create_company(db, name=name, website=None, email=email, status=PENDING,
                                   summary=summary, sector=sector)


def _resolve_new_thread(thread_id: str, msgs: list[dict], subject: str, me: str) -> tuple[int | None, dict | None]:
    """Fil inconnu : pré-filtre local -> entreprise déjà connue -> classification IA locale -> création.

    Retourne (company_id, analyse) ; l'analyse vaut None quand on n'a pas eu besoin de l'IA.
    """
    with get_db() as db:
        skipped = db.execute("SELECT msg_count FROM skipped_thread WHERE thread_id = ?", (thread_id,)).fetchone()
    if skipped and skipped["msg_count"] == len(msgs):
        return None, None

    external = _external_addresses(msgs, me)
    if not external:
        return None, None

    corpus = subject + "\n" + "\n".join(m["body"] for m in msgs)
    if not tu.contains_keyword(corpus, settings.keywords):
        _mark_skipped(thread_id, len(msgs))
        return None, None

    # Entreprise déjà connue (même adresse ou même domaine) : pas besoin de l'IA.
    with get_db() as db:
        for addr in external:
            row = services.find_company_for_address(db, addr)
            if row is not None:
                if row["status"] == IGNORED:
                    _mark_skipped(thread_id, len(msgs))
                    return None, None
                return row["id"], None

    analysis = None
    if local_ai.enabled():
        try:
            analysis = local_ai.classify_thread(subject, msgs)
        except local_ai.LocalAIError as exc:
            log.warning("Classification IA locale impossible pour %s : %s", thread_id, exc)
        if analysis is not None and not analysis["relevant"]:
            _mark_skipped(thread_id, len(msgs))
            return None, None

    primary = next((a for a in external if not _is_noreply(a)), external[0])
    name = (analysis or {}).get("company_name") or _default_company_name(external)
    summary = (analysis or {}).get("summary") or None
    sector = _sector_from(analysis, subject, msgs) or None
    with get_db() as db:
        company_id = _find_or_create_company(db, name, primary, summary, sector)
    return company_id, analysis


def _sector_from(classification: dict | None, subject: str, msgs: list[dict]) -> str:
    """Secteur proposé par l'IA locale, sinon deviné par mots-clés dans le contenu des mails."""
    from_ai = (classification or {}).get("sector")
    if from_ai:
        return from_ai
    corpus = subject + "\n" + "\n".join(m["body"] for m in msgs)
    return tu.guess_sector(corpus) or ""


def _store_messages(thread_id: str, company_id: int, subject: str, msgs: list[dict],
                    notify: bool, analysis: dict | None) -> None:
    with get_db() as db:
        company = db.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
        if company is None or company["status"] == IGNORED:
            return
        company_name = company["name"]
        sector = (company["sector"] or "").strip()
        db.execute("INSERT OR IGNORE INTO thread(id, company_id, subject, created_at) VALUES (?, ?, ?, ?)",
                   (thread_id, company_id, subject, now_iso()))
        new_msgs: list[dict] = []
        for m in msgs:
            cur = db.execute(
                "INSERT OR IGNORE INTO message(id, thread_id, company_id, direction, sender, recipients, "
                "subject, body, sent_at, rfc_id, has_attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m["id"], thread_id, company_id, m["direction"], m["sender"], m["recipients"],
                 m["subject"], m["body"], m["sent_at"], m["rfc_id"], int(m["has_attachments"])),
            )
            if cur.rowcount:
                new_msgs.append(m)
    new_inbound = [m for m in new_msgs if m["direction"] == "in"]

    # Une seule classification locale au besoin : issue du fil et/ou secteur manquant.
    classification = analysis
    if classification is None and local_ai.enabled() and (new_inbound or not sector):
        try:
            classification = local_ai.classify_thread(subject, msgs)
        except local_ai.LocalAIError as exc:
            log.warning("Classification IA locale impossible pour %s : %s", thread_id, exc)

    if not new_msgs:
        # Fil déjà connu, rien de neuf : on complète seulement le secteur s'il manque.
        sector = sector or _sector_from(classification, subject, msgs)
        if sector:
            with get_db() as db:
                db.execute("UPDATE company SET sector = ? WHERE id = ? AND (sector IS NULL OR sector = '')",
                           (sector, company_id))
        return

    outcome = classification["outcome"] if classification else None
    new_sector = sector or _sector_from(classification, subject, msgs)

    with get_db() as db:
        if new_inbound and outcome:
            db.execute("UPDATE company SET ai_label = ? WHERE id = ?", (outcome, company_id))
        if not sector and new_sector:
            db.execute("UPDATE company SET sector = ? WHERE id = ? AND (sector IS NULL OR sector = '')",
                       (new_sector, company_id))
        if analysis and analysis.get("summary"):
            db.execute("UPDATE company SET summary = ? WHERE id = ?", (analysis["summary"], company_id))
        services.recompute_company(db, company_id, new_activity=True)
        if notify and new_inbound:
            services.add_notification(db, "reply", company_id, f"Nouvelle réponse de {company_name}",
                                      f"reply:{new_inbound[-1]['id']}")

"""OAuth Gmail : connexion, retour Google, déconnexion et synchro manuelle."""
import html
import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import gmail_client as gm, sync
from ..config import settings
from ..db import kv_set

log = logging.getLogger("stage-tracker")
router = APIRouter()


def _page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    style = ("font-family:system-ui,sans-serif;max-width:40rem;"
             "margin:4rem auto;padding:0 1rem;line-height:1.6")
    body = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<body style='{style}'>"
        f"<h1>{html.escape(title)}</h1><p>{message}</p><p><a href='/'>Retour à l'application</a></p></body>"
    )
    return HTMLResponse(body, status_code=status_code)


@router.get("/auth/login", include_in_schema=False)
def auth_login(request: Request):
    if not gm.has_client_secret():
        return _page(
            "Fichier credentials.json introuvable",
            "Place le fichier téléchargé depuis Google Cloud dans le dossier <code>secrets/</code> "
            "(nom : <code>credentials.json</code>), puis recharge cette page. Voir le README, étape 1.",
            400,
        )
    redirect_uri = settings.oauth_redirect_uri or str(request.base_url).rstrip("/") + "/auth/callback"
    try:
        return RedirectResponse(gm.start_login(redirect_uri))
    except Exception as exc:  # noqa: BLE001
        return _page("Connexion Gmail impossible", html.escape(str(exc)), 500)


@router.get("/auth/callback", include_in_schema=False)
def auth_callback(state: str | None = None, code: str | None = None, error: str | None = None):
    if error:
        return _page("Connexion annulée", f"Google a répondu : <code>{html.escape(error)}</code>.", 400)
    if not code or not state:
        return _page("Connexion impossible", "Paramètres de retour Google manquants.", 400)
    try:
        gm.finish_login(state, code)
    except gm.NotConnectedError as exc:
        return _page("Connexion impossible", html.escape(str(exc)), 400)
    except Exception as exc:  # noqa: BLE001
        log.exception("Échec de l'échange du code OAuth")
        return _page("Connexion impossible", html.escape(str(exc)), 500)
    sync.state["needs_reauth"] = False
    try:
        kv_set("my_email", gm.get_profile(gm.get_service())["emailAddress"].lower())
    except Exception:  # noqa: BLE001
        log.warning("Impossible de lire le profil Gmail juste après la connexion")
    threading.Thread(target=sync.run_sync, daemon=True, name="first-sync").start()
    return RedirectResponse("/", status_code=303)


@router.post("/auth/logout")
def auth_logout():
    gm.disconnect()
    return {"ok": True}


@router.post("/api/sync")
def api_sync(full: bool = False):
    if not gm.is_connected():
        raise HTTPException(400, "Gmail n'est pas connecté.")
    if sync.state["running"]:
        return {"started": False}
    threading.Thread(target=sync.run_sync, kwargs={"force_full": full}, daemon=True, name="manual-sync").start()
    return {"started": True}

"""Authentification Basic optionnelle et protection anti-CSRF des écritures."""
import base64
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import settings

# En-tête personnalisé que seule notre interface envoie : une page tierce ne peut
# pas le forger en cross-site, ce qui bloque les requêtes d'écriture non désirées.
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "stagetracker"
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_AUTH_EXEMPT_PATHS = {"/api/health"}


def _authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return False
    _, _, password = decoded.partition(":")
    return secrets.compare_digest(password.encode(), settings.app_password.encode())


async def guard(request: Request, call_next):
    # 1) mot de passe optionnel (APP_PASSWORD) : identifiant libre, mot de passe vérifié
    if settings.app_password and request.url.path not in _AUTH_EXEMPT_PATHS and not _authorized(request):
        return Response("Authentification requise", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Stage Tracker"'})
    # 2) anti-CSRF : toute écriture doit venir de notre interface
    if request.method in _WRITE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return JSONResponse({"detail": "Requête refusée (en-tête de sécurité manquant)."}, status_code=403)
    return await call_next(request)


def install(app: FastAPI) -> None:
    app.middleware("http")(guard)

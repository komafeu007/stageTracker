"""Messages d'erreur compréhensibles.

Toute erreur technique montrée à l'utilisateur (bandeau de synchro, toast de l'interface,
réponse JSON) passe par `explain()` : une phrase en français qui dit ce qui s'est passé et
quoi faire. Le détail technique (trace complète, réponse HTTP brute) reste dans les logs
(`docker compose logs -f`).
"""
import errno
import json
import logging
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

log = logging.getLogger("stage-tracker")

_GENERIC = (
    "Une erreur inattendue s'est produite. Réessaie ; si cela continue, "
    "regarde les logs (docker compose logs -f)."
)
_NO_NETWORK = (
    "Pas d'accès à Internet : le serveur n'a pas pu être joint (nom de domaine introuvable). "
    "Vérifie ta connexion, puis relance la synchro. Après un changement de réseau ou de VPN, "
    "redémarre l'application."
)
_REFUSED = (
    "Le service ne répond pas : il n'est probablement pas démarré (Ollama ?). "
    "Démarre-le, puis réessaie."
)
_TIMEOUT = "Le serveur met trop de temps à répondre. Vérifie ta connexion, puis réessaie."
_TLS = (
    "Connexion sécurisée impossible (le certificat a été refusé). "
    "Vérifie la date et l'heure de la machine, puis réessaie."
)

# Fragments de message qui trahissent la cause réelle, quelle que soit la bibliothèque
# (requests, urllib3, httpx, ssl, socket…) : on ne peut pas se fier au seul type.
_DNS_MARKERS = (
    "name resolution",
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo",
    "no address associated",
    "temporary failure in name",
)
_REFUSED_MARKERS = ("connection refused", "actively refused", "errno 111")
_TIMEOUT_MARKERS = ("timed out", "timeout", "timed-out")
_TLS_MARKERS = ("certificate verify failed", "ssl", "tls", "handshake")

_ERRNO_MESSAGES = {
    errno.ENOSPC: "Le disque est plein : libère de l'espace, puis réessaie.",
    errno.EACCES: "Accès refusé à un fichier : vérifie les droits du dossier data.",
    errno.EPERM: "Opération non autorisée sur un fichier : vérifie les droits du dossier data.",
    errno.EROFS: "Le fichier est en lecture seule : vérifie les droits du dossier data.",
    errno.ECONNREFUSED: _REFUSED,
    errno.EHOSTUNREACH: "Le réseau est injoignable : vérifie ta connexion, puis réessaie.",
    errno.ENETUNREACH: "Le réseau est injoignable : vérifie ta connexion, puis réessaie.",
}


def _chain(exc: BaseException):
    """Parcourt la chaîne des exceptions (cause et contexte), sans boucle infinie."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _chain_text(exc: BaseException) -> str:
    return " | ".join(str(e).lower() for e in _chain(exc))


def _first(exc: BaseException, cls: type) -> BaseException | None:
    return next((e for e in _chain(exc) if isinstance(e, cls)), None)


def _user_facing_errors() -> tuple[type[BaseException], ...]:
    """Exceptions dont le message est déjà écrit pour l'utilisateur : on le garde tel quel.

    Import tardif : ces modules utilisent `errors`, un import en tête créerait un cycle.
    """
    from . import gemini_client, gmail_client, local_ai

    return (gemini_client.AIError, gmail_client.NotConnectedError, local_ai.LocalAIError)


def _google_message(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", None)
    raw = str(exc).lower()
    if status == 401:
        return "Google a refusé l'accès : reconnecte Gmail depuis l'application."
    if status == 403:
        if "ratelimit" in raw or "quota" in raw or "userratelimit" in raw:
            return ("Gmail limite temporairement les requêtes : attends quelques minutes, "
                    "puis relance la synchro.")
        return ("Google a refusé l'action (autorisation insuffisante). Reconnecte Gmail "
                "en laissant toutes les cases cochées.")
    if status == 404:
        return "Cet élément n'existe plus chez Google (message supprimé ou fil déplacé)."
    if status == 429:
        return "Trop de requêtes envoyées à Google : attends quelques minutes, puis réessaie."
    if status == 400:
        return "Google a refusé la requête (données invalides). Vérifie le contenu, puis réessaie."
    if status and status >= 500:
        return "Google a un problème temporaire. Réessaie dans quelques minutes."
    return f"Google a renvoyé une erreur ({status or 'inconnue'}). Réessaie dans quelques minutes."


def _sqlite_message(exc: sqlite3.Error) -> str:
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return "La base de données est occupée par une autre opération. Réessaie dans un instant."
    if "readonly" in message or "read-only" in message:
        return "La base de données est en lecture seule : vérifie les droits du dossier data."
    if "disk" in message or "malformed" in message or "corrupt" in message:
        return ("Le fichier de base de données semble abîmé. Sauvegarde data/stage_tracker.db, "
                "puis redémarre l'application.")
    if "unable to open" in message:
        return "Impossible d'ouvrir la base de données : vérifie que le dossier data est accessible."
    return ("La base de données a rencontré un problème. Réessaie ; si cela continue, "
            "regarde les logs.")


def explain(exc: BaseException) -> str:
    """Traduit une exception en une phrase compréhensible et actionnable (français)."""
    if isinstance(exc, _user_facing_errors()):
        return str(exc).strip() or _GENERIC

    http_error = _first(exc, HttpError)
    if http_error is not None:
        return _google_message(http_error)

    if _first(exc, RefreshError) is not None:
        return ("L'autorisation Gmail a expiré ou a été révoquée. Clique sur « Reconnecter Gmail », "
                "puis relance la synchro.")

    text = _chain_text(exc)
    if any(marker in text for marker in _DNS_MARKERS):
        return _NO_NETWORK
    if any(marker in text for marker in _REFUSED_MARKERS):
        return _REFUSED
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return _TIMEOUT
    if any(marker in text for marker in _TLS_MARKERS):
        return _TLS

    sqlite_error = _first(exc, sqlite3.Error)
    if sqlite_error is not None:
        return _sqlite_message(sqlite_error)  # type: ignore[arg-type]

    if _first(exc, TransportError) is not None:
        return _NO_NETWORK

    if isinstance(exc, OSError) and exc.errno in _ERRNO_MESSAGES:
        return _ERRNO_MESSAGES[exc.errno]

    if isinstance(exc, json.JSONDecodeError):
        return "La réponse reçue est illisible (format inattendu). Réessaie dans un instant."

    return _GENERIC


def reason(exc: BaseException) -> str:
    """Libellé très court, pour composer un message (« modèle X : raison »)."""
    text = _chain_text(exc)
    if any(marker in text for marker in _DNS_MARKERS):
        return "Internet inaccessible (DNS)"
    if any(marker in text for marker in _REFUSED_MARKERS):
        return "connexion refusée"
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "délai dépassé"
    if any(marker in text for marker in _TLS_MARKERS):
        return "connexion sécurisée impossible"
    return "erreur réseau"


def install(app: FastAPI) -> None:
    """Dernier filet : aucune trace technique ne doit atteindre l'interface.

    À installer en dernier pour être le middleware le plus externe (il voit donc aussi
    les erreurs des routes et des autres middlewares). Les `HTTPException` gardent leur
    message : elles sont traitées plus bas et ne passent pas par ici.
    """

    @app.middleware("http")
    async def _explain_unexpected(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - filet de sécurité volontairement large
            log.exception("Erreur inattendue sur %s %s", request.method, request.url.path)
            return JSONResponse({"detail": explain(exc)}, status_code=500)

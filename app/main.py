"""Stage Tracker : application FastAPI (API JSON + interface servie en statique).

La logique est répartie dans app/api (routes) et app/services (métier) ; ce module
ne fait que câbler l'application et démarrer la tâche de fond.
"""
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import errors, gmail_client as gm, security, services, sync
from .api import auth, companies, cv, letter, mail, messages, notifications, system
from .config import settings
from .db import get_db, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stage-tracker")


def _is_schema_error(exc: sqlite3.OperationalError) -> bool:
    """Vrai si la base existe mais que son schéma manque (ex. fichier vidé)."""
    message = str(exc).lower()
    return "no such table" in message or "no such column" in message


def ensure_schema() -> None:
    """Vérifie que le schéma est présent et le recrée si la base a été vidée/corrompue."""
    try:
        with get_db() as db:
            db.execute("SELECT 1 FROM kv LIMIT 1")
    except sqlite3.OperationalError as exc:
        if not _is_schema_error(exc):
            raise
        log.warning("Schéma SQLite manquant (%s) : recréation des tables", exc)
        init_db()


def _background_loop(stop: threading.Event) -> None:
    """Synchro Gmail périodique (delta) + vérification des relances, 24h/24."""
    stop.wait(5)
    while not stop.is_set():
        try:
            ensure_schema()
            if gm.is_connected():
                sync.run_sync()
            else:
                services.check_followups()
        except Exception:  # noqa: BLE001
            log.exception("Erreur dans la tâche de fond")
        stop.wait(settings.sync_interval_min * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    stop = threading.Event()
    threading.Thread(target=_background_loop, args=(stop,), daemon=True, name="sync-loop").start()
    yield
    stop.set()


def create_app() -> FastAPI:
    app = FastAPI(title="Stage Tracker", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(sqlite3.OperationalError)
    async def _sqlite_error(_request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        """Ne laisse pas une base abîmée faire croire que Gmail est déconnecté."""
        if not _is_schema_error(exc):
            raise exc
        log.warning("Base SQLite illisible en cours d'exécution (%s) : recréation", exc)
        try:
            init_db()
        except Exception:  # noqa: BLE001
            log.exception("Impossible de recréer le schéma SQLite")
        return JSONResponse(
            {"detail": "La base de données a été réparée. Recharge la page."}, status_code=503
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 en français, au lieu de la liste technique de FastAPI (en anglais)."""
        log.warning("Requête invalide : %s", exc.errors())
        fields = sorted({str(err.get("loc", ("",))[-1]) for err in exc.errors() if err.get("loc")})
        detail = "Données invalides"
        if fields:
            detail += f" ({', '.join(fields)})"
        return JSONResponse(
            {"detail": detail + ". Vérifie le formulaire, puis réessaie."}, status_code=422
        )

    security.install(app)
    errors.install(app)  # en dernier : filet le plus externe, il explique toute erreur imprévue
    for module in (system, auth, companies, mail, letter, messages, cv, notifications):
        app.include_router(module.router)
    return app


app = create_app()

"""Santé, état global et page d'accueil."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from .. import gemini_client, gmail_client as gm, services, sync
from ..config import settings
from ..constants import SECTORS
from ..db import kv_get
from .deps import cv_info

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@router.get("/api/health")
def health():
    return {"ok": True}


@router.get("/api/status")
def api_status():
    return {
        "gmail_connected": gm.is_connected(),
        "gmail_email": kv_get("my_email"),
        "has_client_secret": gm.has_client_secret(),
        "needs_reauth": sync.state["needs_reauth"],
        "ai_enabled": gemini_client.enabled(),
        "ai_model": settings.gemini_model,
        "user_name": settings.user_name,
        "sectors": list(SECTORS),
        "cv": cv_info(),
        "sync": {k: sync.state[k] for k in ("running", "mode", "done", "total", "last_run", "last_error")},
        "tokens": services.token_stats(),
    }


@router.get("/api/tokens")
def api_tokens():
    return services.token_stats()

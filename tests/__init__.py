"""Tests hors-ligne (Gmail et IA simulés).

L'environnement est configuré ICI, avant tout import de `app`, car `app.config`
lit les variables au moment de l'import.

Lancer :  python -m unittest discover -v
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="stagetracker-tests-"))
os.environ.setdefault("DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("SECRETS_DIR", str(_TMP / "secrets"))
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OLLAMA_ENABLED", "false")
os.environ.setdefault("APP_PASSWORD", "")

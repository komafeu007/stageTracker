"""Configuration centralisée : tout vient des variables d'environnement (.env)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secrets_dir: Path
    client_secret_file: Path
    token_file: Path
    db_path: Path
    cv_path: Path
    prompts_dir: Path

    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: list[str]  # modèles essayés si le principal est saturé/indisponible
    token_budget: int          # budget mensuel de tokens (indicatif, pour la jauge)
    ai_min_interval: float     # secondes mini entre 2 appels IA (limite le risque de 429)

    user_name: str
    keywords: list[str]
    followup_days: list[int]   # ex. [7, 14]
    followup_max_age_days: int # au-delà, on ne propose plus de relance
    sync_interval_min: int
    scan_max_threads: int

    app_password: str          # vide = pas de mot de passe
    oauth_redirect_uri: str    # vide = déduit de l'URL utilisée dans le navigateur

    ollama_enabled: bool       # si True, Ollama classe les fils
    ollama_host: str           # URL du serveur Ollama
    ollama_model: str          # modèle Ollama utilisé pour la classification


def load_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", "./data"))
    secrets_dir = Path(_env("SECRETS_DIR", "./secrets"))

    days = [int(x) for x in _env_list("FOLLOWUP_DAYS", "7,14") if x.isdigit()] or [7, 14]

    return Settings(
        data_dir=data_dir,
        secrets_dir=secrets_dir,
        client_secret_file=secrets_dir / "credentials.json",
        token_file=data_dir / "token.json",
        db_path=data_dir / "stage_tracker.db",
        cv_path=data_dir / "cv_active.pdf",
        prompts_dir=data_dir / "prompts",
        gemini_api_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL", "gemini-flash-latest"),
        gemini_fallback_models=_env_list("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash,gemini-flash-lite-latest"),
        token_budget=_env_int("GEMINI_MONTHLY_TOKEN_BUDGET", 1_000_000),
        ai_min_interval=_env_float("AI_MIN_INTERVAL_SECONDS", 4.0),
        user_name=_env("USER_FULL_NAME", "Prénom Nom"),
        keywords=_env_list(
            "GMAIL_KEYWORDS",
            "stage,stagiaire,candidature,alternance,apprentissage,internship,recrutement",
        ),
        followup_days=sorted(days),
        followup_max_age_days=_env_int("FOLLOWUP_MAX_AGE_DAYS", 60),
        sync_interval_min=max(1, _env_int("SYNC_INTERVAL_MINUTES", 10)),
        scan_max_threads=max(10, _env_int("SCAN_MAX_THREADS", 300)),
        app_password=_env("APP_PASSWORD"),
        oauth_redirect_uri=_env("OAUTH_REDIRECT_URI"),
        ollama_enabled=_env("OLLAMA_ENABLED", "true").lower() == "true",
        ollama_host=_env("OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
        ollama_model=_env("OLLAMA_MODEL", "llama3.2:1b"),
    )


settings = load_settings()

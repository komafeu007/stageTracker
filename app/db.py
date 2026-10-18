"""SQLite : schéma + petits helpers. Une connexion par opération (thread-safe)."""
import contextlib
import sqlite3
from datetime import UTC, datetime

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    website         TEXT,
    email           TEXT,
    domain          TEXT,
    sector          TEXT,                              -- ce que fait l'entreprise (sous-domaine IT)
    status          TEXT NOT NULL DEFAULT 'prospect',  -- prospect | pending | followup | replied | refused | ignored
    status_manual   INTEGER NOT NULL DEFAULT 0,        -- 1 = statut forcé par l'utilisateur
    ai_label        TEXT,                              -- refusal | positive | none (dernier mail reçu)
    followup_count  INTEGER NOT NULL DEFAULT 0,
    anchor_at       TEXT,                              -- date du dernier mail envoyé (départ de la relance suivante)
    last_message_at TEXT,
    last_direction  TEXT,                              -- in | out
    summary         TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thread (
    id         TEXT PRIMARY KEY,                       -- threadId Gmail
    company_id INTEGER REFERENCES company(id) ON DELETE SET NULL,
    subject    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    id              TEXT PRIMARY KEY,                  -- id de message Gmail
    thread_id       TEXT NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
    company_id      INTEGER REFERENCES company(id) ON DELETE CASCADE,
    direction       TEXT NOT NULL,                     -- in | out
    sender          TEXT,
    recipients      TEXT,
    subject         TEXT,
    body            TEXT,
    sent_at         TEXT NOT NULL,
    rfc_id          TEXT,                              -- en-tête Message-ID (pour In-Reply-To)
    has_attachments INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_message_company ON message(company_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_message_thread  ON message(thread_id);

CREATE TABLE IF NOT EXISTS token_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    purpose       TEXT,
    model         TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_token_ts ON token_log(ts);

CREATE TABLE IF NOT EXISTS notification (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,                          -- reply | followup
    company_id INTEGER REFERENCES company(id) ON DELETE CASCADE,
    message    TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft (
    company_id INTEGER PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                          -- application | followup | reply
    to_addr    TEXT,
    subject    TEXT,
    body_html  TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skipped_thread (
    thread_id TEXT PRIMARY KEY,
    msg_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Colonnes ajoutées après la première version du schéma : `CREATE TABLE IF NOT EXISTS`
# ne les ajoute pas à une base existante, on les applique donc explicitement (idempotent).
_ADDED_COLUMNS = {
    "company": {"sector": "TEXT"},
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


@contextlib.contextmanager
def get_db():
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.prompts_dir.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        _apply_added_columns(db)


def _apply_added_columns(db) -> None:
    """Ajoute les colonnes manquantes sur une base déjà créée (migration additive)."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def kv_get(key: str, default: str | None = None) -> str | None:
    with get_db() as db:
        row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

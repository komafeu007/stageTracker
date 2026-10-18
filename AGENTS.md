# AGENTS.md

Stage Tracker: single-user FastAPI + SQLite web app. It syncs a Gmail inbox, classifies
threads with a **local** model (Ollama), and drafts internship applications/follow-ups with
**Gemini**; mail is sent only on an explicit user click. The whole UI is one static HTML file
(Vue + Tailwind via CDN) calling the JSON API. User-facing text and prompt files are French.

## Run & verify

- Docker (documented path): `docker compose up -d --build` → http://localhost:8000
  (health: `curl localhost:8000/api/health`). Code changes need a rebuild; there is no hot reload.
- Docker and a local `uvicorn` both use `./data` and port 8000 — run only one at a time.
- The container uses `network_mode: host` so it can reach the host's Ollama, which listens only on
  `127.0.0.1` (`OLLAMA_HOST=http://127.0.0.1:11434`). `ports:` is therefore ignored; the listen
  address comes from `APP_BIND`/`APP_PORT` (default `127.0.0.1:8000`) via the compose `command`.
- Local: `settings` defaults are `./data` and `./secrets` (relative to the repo root), so
  `uvicorn app.main:app --reload` works as-is from the repo root (a `venv/` with the deps exists).
- Tests (offline, no Gmail/Ollama needed): `python -m unittest discover -v`. `tests/__init__.py`
  sets a throwaway `DATA_DIR` before importing `app`, because `app.config` reads env at import time.
- Logs: `docker compose logs -f`.

## Hard constraints

- Run uvicorn with a **single worker**. The Gmail-sync + follow-up loop is a daemon thread inside the
  process (`app/main.py:_background_loop`); extra workers would duplicate it.
- Every non-GET API call must send header `X-Requested-With: stagetracker` or the middleware returns
  403 (`app/security.py`). Required when probing endpoints with curl or TestClient.
- `settings` is built once at import (`app/config.py`); `.env` changes need a restart. Exception:
  files in `data/prompts/` are re-read on every generation.
- Prompts are shipped as `data/prompts/*.md.example`; the live `*.md` are **gitignored** so a
  filled-in profile (name, phone, experience) can never be committed. `gemini_client.load_prompt_file`
  falls back to a generic text when a file is missing. Setup: `cp data/prompts/<name>.md.example
  data/prompts/<name>.md`.
- Conventions: ruff config in `pyproject.toml` (`ruff check .`); identifiers in English, comments and
  user-facing text in French, lines ≤ 120 chars, `snake_case` / `PascalCase` / `UPPER_CASE`.
- SQLite schema (`app/db.py:SCHEMA`) is applied idempotently at startup; there is **no migration
  tool**. Keep schema changes backward-compatible with an existing `data/stage_tracker.db`
  (`CREATE TABLE IF NOT EXISTS`, additive columns). Une nouvelle colonne doit aussi être déclarée
  dans `app/db.py:_ADDED_COLUMNS` pour être ajoutée (`ALTER TABLE ... ADD COLUMN`) aux bases
  existantes, car `CREATE TABLE IF NOT EXISTS` ne touche pas une table déjà créée.
- Never send mail automatically; sending is always a user action.

## AI split (easy to get wrong)

- **Ollama = classification only** (`app/local_ai.py`): relevance, company name, refusal/positive
  outcome, summary, et **secteur d'activité** (déduit du contenu des mails). Called from
  `app/sync.py`. Tunable via `OLLAMA_ENABLED/HOST/MODEL`. Si Ollama est indisponible, le secteur
  est deviné par mots-clés (`textutils.guess_sector`).
- **Gemini = drafting only** (`app/gemini_client.py`): application and follow-up text, plus website
  context, plus spelling-only correction of an edited draft (`correct_spelling`, ne touche pas au
  contenu), plus réécriture d'un seul paragraphe (`draft_paragraph`). Token usage is logged to
  `token_log` (feeds the UI gauge).
- **Style imposé aux brouillons** : aucun superlatif ni mot compliqué (vocabulaire simple, phrases
  courtes), et **jamais de ligne « Objet : »/d'en-tête dans le corps** (l'objet est un champ séparé).
  Consigne présente dans `_application_system`, `draft_followup`, `draft_reply` (qui présentent le
  mail précédent avec « Sujet : … » et non « Objet : … », sinon le modèle le recopie) ;
  `gemini_client._clean_body()` retire en plus un éventuel bloc d'en-têtes en tête de corps.
  Ne pas retirer ces règles, c'est une demande explicite de l'utilisateur.
- Le 2e paragraphe des candidatures est repris des `exemples_valides.md` (formation, disponibilité,
  objectif) : c'est volontaire, ne pas le faire réinventer par le modèle.
- Le projet mentionné dans la candidature est choisi selon le domaine de l'entreprise (bras
  robotique, vision, IoT, infra…), à partir du `profil.md` et du contexte du site : garder cette
  consigne dans `_application_system`/`draft_application`.
- `company.sector` (sous-domaine IT, valeurs proposées dans `constants.SECTORS`) sert de référence
  au choix du projet dans la candidature. L'UI l'affiche en pastille et filtre la barre de gauche.
- Do not move classification to Gemini or drafting to Ollama. If Ollama is disabled or unreachable,
  sync falls back to keyword filtering + domain-based company naming; drafting still needs Gemini.

## Layout

- `app/main.py` — app factory, lifespan, router wiring. Entry point.
- `app/api/` — one router per domain: `system.py` (health/status/tokens/index), `auth.py` (OAuth +
  `/api/sync`), `companies.py`, `mail.py` (drafts + send), `messages.py` (réaffectation manuelle d'un
  mail à une autre entreprise), `cv.py`, `notifications.py`, and `deps.py` (request models + shared
  helpers).
- `app/security.py` — optional Basic auth + anti-CSRF middleware.
- `app/config.py` — env-driven `settings`; source of config truth (`.env.example` mirrors it).
- `app/constants.py` — shared status/draft-kind string values (avoid magic strings).
- `app/db.py` — SQLite schema and `get_db()` (one connection per operation).
- `app/sync.py` — Gmail→SQLite pipeline: initial full scan, then delta via `history.list`; keyword
  pre-filter, known-company match, then Ollama classification. Remplit aussi `company.sector`
  (IA locale, sinon mots-clés) pour les entreprises qui n'en ont pas, via `backfill_sectors()`.
- `app/services.py` — status derivation, J+7/J+14 follow-ups, notifications, drafts, token stats.
- `app/gmail_client.py` — OAuth2 (état PKCE persisté sur disque pour survivre à un redémarrage
  entre `/auth/login` et `/auth/callback`), thread fetch/parse, MIME build + send.
- `app/textutils.py` — HTML/text cleanup, address/domain helpers.
- `app/static/index.html` — entire frontend; Vue/Tailwind load from CDNs, so the browser needs internet.

## Domain notes

- Company `status` is derived from message history in `services.recompute_company`
  (last message out → `pending`; in → `replied`/`refused`). PATCH `status` sets `status_manual=1`,
  which overrides until the next message. `ignored` = trash.
- Le classement (mot-clé + Ollama) reste une heuristique : l'UI permet de **corriger à la main**.
  `PATCH /api/companies/{id}` renomme ; `POST /api/messages/move` (`{message_ids, company_id}`)
  déplace un mail ou toute une conversation. Un transfert réaffecte `message.company_id`, met à jour
  `thread.company_id` si plus aucun message ne reste ailleurs, et envoie à la corbeille une entreprise
  devenue vide (réversible via `/restore`).
- Follow-ups depend on `anchor_at` (date du **dernier mail envoyé**, pas de la candidature) and
  `FOLLOWUP_DAYS` (`anchor_at + FOLLOWUP_DAYS[followup_count]`) ; `FOLLOWUP_MAX_AGE_DAYS` stops
  proposing them for stale applications. `services.recompute_followups()` recalcule tout à chaque
  synchro (appelé par `check_followups`), car seule la date fait évoluer une échéance ; les
  notifications de relance devenues obsolètes sont purgées au passage.
- Gemini calls are throttled by `AI_MIN_INTERVAL_SECONDS` and retried on 429/5xx; if the primary
  model stays unavailable, `GEMINI_FALLBACK_MODELS` are tried in order (so a saturated
  `gemini-flash-latest` no longer blocks drafting). `AIQuotaError` pauses sync until the next cycle.
- Le raisonnement des modèles Gemini 2.5 et des alias `*-latest` doit rester coupé
  (`thinkingConfig.thinkingBudget = 0`, cf. `gemini_client._thinks_by_default`) : sinon il consomme
  `maxOutputTokens` et la réponse JSON arrive tronquée (« Réponse IA illisible »). Une réponse
  `finishReason == "MAX_TOKENS"` est traitée comme un échec temporaire (essai du modèle suivant).
- Persistent state lives under `DATA_DIR` (default `/data` in Docker, `./data` locally): DB,
  `cv_active.pdf`, `token.json`, `prompts/`, `oauth_pending.json` (connexion Gmail en cours).
  `credentials.json` lives under `SECRETS_DIR`.
- Si la base SQLite perd son schéma en cours d'exécution (fichier vidé, etc.), `main.ensure_schema`
  et le gestionnaire d'erreur `sqlite3.OperationalError` recréent les tables au lieu de laisser
  toutes les routes en 500 (ce qui faisait croire à tort que Gmail était déconnecté).

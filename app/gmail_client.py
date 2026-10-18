"""Gmail : connexion OAuth2, lecture des fils (threads), synchro delta (history) et envoi."""
import base64
import json
import os
import random
import re
import threading
import time
from datetime import UTC, datetime
from email.message import EmailMessage

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import textutils as tu
from .config import settings

# L'app tourne en HTTP sur localhost : on autorise ce transport pour le retour OAuth.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",  # lire l'historique de la boîte
    "https://www.googleapis.com/auth/gmail.send",      # envoyer (jamais sans clic de l'utilisateur)
]

# Libellés Gmail qui indiquent un message à ignorer pour un fil inconnu
SKIP_LABELS = {"SPAM", "TRASH", "DRAFT", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}


class NotConnectedError(Exception):
    """Gmail n'est pas connecté (ou l'autorisation a expiré)."""


class HistoryExpiredError(Exception):
    """Le historyId est trop ancien : il faut refaire un scan complet."""


# --------------------------------------------------------------------------- OAuth
# L'état de la connexion (code_verifier PKCE + redirect_uri) est conservé sur disque :
# ainsi un redémarrage du serveur entre /auth/login et /auth/callback ne casse plus la
# connexion (avant, l'état vivait uniquement en mémoire et était perdu au redémarrage).
_pending_lock = threading.Lock()
_pending_flows: dict[str, dict] = {}
_pending_loaded = False
_MAX_PENDING = 10  # on garde les dernières tentatives, les plus récentes


def _pending_file():
    return settings.data_dir / "oauth_pending.json"


def _pending_state() -> dict:
    """Charge une fois l'état depuis le disque, puis garde le tout en mémoire."""
    global _pending_loaded
    if not _pending_loaded:
        try:
            data = json.loads(_pending_file().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _pending_flows.update(data)
        except (OSError, ValueError):
            pass
        _pending_loaded = True
    return _pending_flows


def _save_pending() -> None:
    try:
        _pending_file().parent.mkdir(parents=True, exist_ok=True)
        _pending_file().write_text(json.dumps(_pending_flows), encoding="utf-8")
        os.chmod(_pending_file(), 0o600)  # contient le vérificateur PKCE : lisible par le seul propriétaire
    except OSError:
        pass  # au pire, on retombe sur l'état en mémoire (comme avant)


def has_client_secret() -> bool:
    return settings.client_secret_file.exists()


def is_connected() -> bool:
    return settings.token_file.exists()


def start_login(redirect_uri: str) -> str:
    flow = Flow.from_client_secrets_file(
        str(settings.client_secret_file), scopes=SCOPES, redirect_uri=redirect_uri
    )
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    with _pending_lock:
        pending = _pending_state()
        pending[state] = {"redirect_uri": redirect_uri, "code_verifier": flow.code_verifier}
        for old in list(pending)[:-_MAX_PENDING]:
            pending.pop(old)
        _save_pending()
    return url


def finish_login(state: str, code: str) -> None:
    with _pending_lock:
        pending = _pending_state()
        info = pending.pop(state, None)
        if info is not None:
            _save_pending()
    if not info:
        raise NotConnectedError("Session de connexion introuvable ou expirée. Relance « Connecter Gmail ».")
    flow = Flow.from_client_secrets_file(
        str(settings.client_secret_file),
        scopes=SCOPES,
        redirect_uri=info["redirect_uri"],
        code_verifier=info.get("code_verifier"),
    )
    flow.fetch_token(code=code)
    granted = flow.oauth2session.token.get("scope") or []
    if isinstance(granted, str):
        granted = granted.split()
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise NotConnectedError(
            "Toutes les autorisations n'ont pas été accordées (lecture + envoi Gmail). "
            "Relance la connexion et laisse les cases cochées."
        )
    _save_credentials(flow.credentials)


def _save_credentials(creds: Credentials) -> None:
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(creds.to_json())
    try:
        os.chmod(settings.token_file, 0o600)
    except OSError:
        pass


def disconnect() -> None:
    settings.token_file.unlink(missing_ok=True)


def load_credentials() -> Credentials:
    if not settings.token_file.exists():
        raise NotConnectedError("Gmail n'est pas connecté.")
    creds = Credentials.from_authorized_user_file(str(settings.token_file))
    if not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
                _save_credentials(creds)
            except RefreshError as exc:
                raise NotConnectedError(
                    "L'autorisation Gmail a expiré ou a été révoquée. Clique sur « Connecter Gmail »."
                ) from exc
        else:
            raise NotConnectedError("Autorisation Gmail invalide. Clique sur « Connecter Gmail ».")
    return creds


def get_service():
    """Nouveau client par opération (les clients Google ne sont pas thread-safe)."""
    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)


def get_profile(service) -> dict:
    return service.users().getProfile(userId="me").execute(num_retries=3)


# --------------------------------------------------------------------------- lecture
def build_query(keywords: list[str]) -> str:
    terms = " OR ".join(f'"{k}"' if " " in k else k for k in keywords)
    return f"({terms}) -category:promotions -category:social -category:forums"


def _is_rate_limit(exc: HttpError) -> bool:
    return getattr(exc.resp, "status", None) == 403 and "rateLimitExceeded" in str(exc)


def _backoff(attempt: int, cap: float) -> None:
    """Pause exponentielle avec un peu de hasard (anti rate-limit Gmail)."""
    time.sleep(min(2 ** attempt + random.uniform(0, 1), cap))


def list_thread_ids(service, query: str, limit: int) -> list[str]:
    """Liste des identifiants de fils (pagination + reprise sur rate-limit)."""
    ids: list[str] = []
    token = None
    attempt = 0
    while len(ids) < limit and attempt < 10:
        try:
            resp = service.users().threads().list(
                userId="me", q=query, maxResults=min(50, limit - len(ids)), pageToken=token
            ).execute()
        except HttpError as exc:
            if not _is_rate_limit(exc):
                raise
            _backoff(attempt, cap=60)
            attempt += 1
            continue
        ids += [t["id"] for t in resp.get("threads", [])]
        token = resp.get("nextPageToken")
        attempt = 0  # remis à zéro après un appel réussi
        if not token:
            break
    return ids


def get_thread(service, thread_id: str, max_retries: int = 5) -> dict:
    """Récupère un fil complet, avec reprise sur rate-limit."""
    last_error: HttpError | None = None
    for attempt in range(max_retries):
        try:
            return service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        except HttpError as exc:
            if not _is_rate_limit(exc):
                raise
            last_error = exc
            _backoff(attempt, cap=30)
    raise last_error or RuntimeError(f"Fil {thread_id} inaccessible après {max_retries} tentatives")


def history_threads(service, start_history_id: str) -> tuple[dict[str, set[str]], str]:
    """Synchro delta : fils ayant reçu de nouveaux messages depuis start_history_id."""
    threads: dict[str, set[str]] = {}
    latest = str(start_history_id)
    token = None
    try:
        while True:
            resp = service.users().history().list(
                userId="me", startHistoryId=start_history_id,
                historyTypes=["messageAdded"], maxResults=500, pageToken=token,
            ).execute(num_retries=3)
            latest = str(resp.get("historyId", latest))
            for entry in resp.get("history", []):
                for added in entry.get("messagesAdded", []):
                    msg = added["message"]
                    threads.setdefault(msg["threadId"], set()).update(msg.get("labelIds", []))
            token = resp.get("nextPageToken")
            if not token:
                break
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            raise HistoryExpiredError() from exc
        raise
    return threads, latest


# --------------------------------------------------------------------------- parsing
def _header(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_part(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    content_type = _header(part.get("headers", []), "Content-Type")
    match = re.search(r"charset=\"?([\w\-]+)", content_type, re.I)
    charset = match.group(1) if match else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict, out: dict) -> None:
    mime = part.get("mimeType", "")
    if part.get("filename"):
        out["attachments"] = True
    elif mime == "text/plain":
        out["text"].append(_decode_part(part))
    elif mime == "text/html":
        out["html"].append(_decode_part(part))
    for child in part.get("parts") or []:
        _walk(child, out)


def parse_message(msg: dict, my_email: str) -> dict:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    out = {"text": [], "html": [], "attachments": False}
    _walk(payload, out)

    plain = "\n".join(t for t in out["text"] if t.strip())
    if plain.strip():
        body = tu.strip_quoted_text(plain)
    else:
        body = tu.html_to_text("\n".join(out["html"]))  # retire déjà gmail_quote / blockquote
    body = tu.truncate(body, 20000)

    labels = set(msg.get("labelIds", []))
    from_hdr = _header(headers, "From")
    from_addrs = tu.parse_addresses(from_hdr)
    from_addr = from_addrs[0][1] if from_addrs else ""
    to_hdr = _header(headers, "To")
    cc_hdr = _header(headers, "Cc")
    direction = "out" if ("SENT" in labels or from_addr == my_email.lower()) else "in"
    sent_at = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=UTC)

    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "direction": direction,
        "sender": from_hdr,
        "from_addr": from_addr,
        "recipients": to_hdr,
        "participants": tu.parse_addresses(from_hdr, to_hdr, cc_hdr),
        "subject": _header(headers, "Subject"),
        "body": body,
        "sent_at": sent_at.isoformat(timespec="seconds"),
        "rfc_id": _header(headers, "Message-ID") or _header(headers, "Message-Id"),
        "labels": labels,
        "has_attachments": out["attachments"],
    }


# --------------------------------------------------------------------------- envoi
def build_raw(
    to: str,
    subject: str,
    html: str,
    attachments: list[tuple[str, bytes, str]],
    in_reply_to: str | None = None,
) -> str:
    """Construit le message MIME (texte + HTML + pièces jointes) encodé pour l'API Gmail."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(tu.html_to_text(html, drop_quotes=False) or " ")
    msg.add_alternative(html, subtype="html")
    for filename, data, content_type in attachments:
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        msg.add_attachment(
            data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename
        )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_raw(service, raw: str, thread_id: str | None = None) -> dict:
    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    # pas de num_retries sur l'envoi : on évite tout risque de doublon
    return service.users().messages().send(userId="me", body=body).execute()

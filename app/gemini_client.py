"""Gemini (API REST) : rédaction des candidatures et des relances.

Seule brique qui produit du texte destiné à être envoyé. La classification des fils
est assurée par l'IA locale (local_ai). Chaque appel est journalisé dans token_log
(alimente la jauge de tokens de l'interface).
"""
import json
import re
import threading
import time

import httpx
from bs4 import BeautifulSoup

from . import textutils as tu
from .config import settings
from .db import get_db, now_iso

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


class AIError(Exception):
    """Erreur d'appel IA (message lisible par l'utilisateur)."""


class AIQuotaError(AIError):
    """Quota / limite de débit Gemini atteint."""


def enabled() -> bool:
    return bool(settings.gemini_api_key)


# --------------------------------------------------------------------------- appel bas niveau
_throttle_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    with _throttle_lock:
        wait = settings.ai_min_interval - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _log_usage(purpose: str, model: str, usage: dict) -> None:
    prompt = int(usage.get("promptTokenCount", 0) or 0)
    total = int(usage.get("totalTokenCount", 0) or 0)
    output = max(0, total - prompt)
    with get_db() as db:
        db.execute(
            "INSERT INTO token_log(ts, purpose, model, prompt_tokens, output_tokens, total_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), purpose, model, prompt, output, total),
        )


def _api_error_message(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]
    except Exception:
        return response.text[:300]


_ROUNDS = 3  # nombre de passes sur l'ensemble des modèles si tout échoue


def _post_model(model: str, body: dict) -> httpx.Response:
    url = f"{API_ROOT}/models/{model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"}
    return httpx.post(url, json=body, headers=headers, timeout=90)


def _try_model(model: str, body: dict, purpose: str) -> tuple[str | None, str, bool]:
    """Tente un unique appel sur un modèle.

    Retourne ``(texte, raison, quota)`` : ``texte`` vaut None si l'échec est
    temporaire (on essaiera un autre modèle) ou ``raison`` décrit l'échec.
    Les erreurs définitives (clé/requête invalide) lèvent directement.
    """
    _throttle()
    try:
        response = _post_model(model, body)
    except httpx.HTTPError as exc:
        return None, f"connexion impossible ({exc})", False

    status = response.status_code
    if status in (400, 401, 403):
        raise AIError(f"Gemini a refusé la requête ({status}) : {_api_error_message(response)}")
    if status == 429:
        return None, "quota atteint", True
    if status == 404:
        return None, "modèle introuvable", False
    if status >= 400:
        return None, f"{status} {_api_error_message(response)}", False

    data = response.json()
    _log_usage(purpose, model, data.get("usageMetadata", {}))
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "inconnue")
        raise AIError(f"Gemini n'a renvoyé aucune réponse (raison : {reason}).")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        return None, "réponse vide", False
    return text, "", False


def _build_body(model: str, system: str, prompt: str, temperature: float,
                max_tokens: int, json_mode: bool) -> dict:
    generation = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        generation["responseMimeType"] = "application/json"
    # Gemini 2.5 « flash » réfléchit par défaut : le raisonnement consomme le
    # budget maxOutputTokens et peut tronquer le JSON attendu. On le désactive.
    if model.startswith("gemini-2.5-flash"):
        generation["thinkingConfig"] = {"thinkingBudget": 0}
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation,
    }


def generate(
    prompt: str, *, system: str, purpose: str, json_mode: bool = False,
    temperature: float = 0.4, max_tokens: int = 4096,
) -> str:
    if not enabled():
        raise AIError("Gemini n'est pas configuré : ajoute GEMINI_API_KEY dans le fichier .env.")

    # Modèles essayés dans l'ordre : le principal puis les replis (sans doublon).
    models = list(dict.fromkeys([settings.gemini_model, *settings.gemini_fallback_models]))

    failures: dict[str, str] = {}
    quota_only = True
    for round_no in range(_ROUNDS):
        if round_no:
            time.sleep(min(60, 8 * 2 ** (round_no - 1)))
        for model in models:
            body = _build_body(model, system, prompt, temperature, max_tokens, json_mode)
            text, reason, is_quota = _try_model(model, body, purpose)
            if text is not None:
                return text
            failures[model] = reason
            quota_only = quota_only and is_quota

    detail = " ; ".join(f"{m}: {r}" for m, r in failures.items())
    if failures and quota_only:
        raise AIQuotaError(
            f"Quota Gemini atteint sur tous les modèles essayés. Réessaie dans quelques minutes. ({detail})"
        )
    raise AIError(f"Gemini indisponible (modèles saturés ?). Réessaie dans quelques minutes. ({detail})")


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise AIError("Réponse IA illisible (JSON attendu).")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AIError("Réponse IA illisible (JSON invalide).") from exc


# --------------------------------------------------------------------------- prompts personnalisables
_FALLBACKS = {
    "profil.md": "Étudiant à la recherche d'un stage. Complète data/prompts/profil.md avec ta formation, "
                 "tes compétences et tes projets pour des candidatures personnalisées.",
    "schema_cours.md": "1. Accroche (qui je suis, ce que je cherche)\n2. Pourquoi cette entreprise\n"
                       "3. Ce que j'apporte (compétences, projets)\n4. Disponibilité et modalités\n"
                       "5. Proposition d'échange\n6. Formule de politesse et signature",
    "exemples_valides.md": "(aucun exemple fourni)",
}


def load_prompt_file(name: str) -> str:
    path = settings.prompts_dir / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = _FALLBACKS.get(name, "")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)  # commentaires d'aide
    lines = [line for line in text.splitlines()
             if "À COMPLÉTER" not in line.upper() and "A COMPLETER" not in line.upper()]
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- contexte entreprise
def fetch_company_context(website: str | None) -> str:
    """Récupère quelques lignes de la page d'accueil pour personnaliser le mail (échec silencieux)."""
    if not website:
        return ""
    url = website if "://" in website else "https://" + website
    try:
        r = httpx.get(url, timeout=6, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (StageTracker)"})
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            return ""
        soup = BeautifulSoup(r.text[:300_000], "html.parser")
    except Exception:
        return ""
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append("Titre : " + soup.title.string.strip())
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            parts.append("Description : " + meta["content"].strip())
            break
    for h in soup.find_all(["h1", "h2"], limit=6):
        text = h.get_text(" ", strip=True)
        if text:
            parts.append("- " + text)
    paragraphs = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p", limit=8))
    if paragraphs:
        parts.append("Extrait : " + paragraphs)
    return tu.truncate("\n".join(parts), 1500)


# --------------------------------------------------------------------------- fonctions métier
def _application_system() -> str:
    return f"""Tu es l'assistant de rédaction d'un étudiant francophone qui cherche un stage.
Tu rédiges des e-mails de candidature en français : naturels, sincères, concis (120 à 180 mots).
Règles :
- Respecte STRICTEMENT la structure du schéma de cours fourni.
- Imite le ton et le style des exemples validés, sans les recopier.
- N'invente aucun fait sur l'étudiant (utilise uniquement son profil) ni sur l'entreprise
  (utilise uniquement le contexte fourni ; s'il est vide ou insuffisant, reste sobre).
- Aucun texte entre crochets, aucun placeholder, aucun markdown. Vouvoiement.
- Indique que le CV est joint à l'e-mail.
- Termine par une formule de politesse puis la signature : {settings.user_name}
Réponds uniquement en JSON :
{{"subject": "objet de l'e-mail", "body": "texte brut, paragraphes séparés par une ligne vide"}}"""


def draft_application(company: dict, context: str) -> tuple[str, str]:
    prompt = f"""## Profil de l'étudiant
{load_prompt_file('profil.md')}

## Schéma du cours à respecter
{load_prompt_file('schema_cours.md')}

## Exemples validés
{load_prompt_file('exemples_valides.md')}

## Entreprise ciblée
Nom : {company['name']}
Site web : {company.get('website') or 'non renseigné'}
Contexte récupéré sur le site (peut être vide ou imparfait) :
{context or '(aucun)'}

Rédige l'e-mail de candidature."""
    data = _parse_json(generate(prompt, system=_application_system(), purpose="candidature",
                                json_mode=True, temperature=0.6))
    subject = str(data.get("subject") or "").strip().replace("\n", " ")
    body = str(data.get("body") or "").strip()
    if not body:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return subject or "Candidature pour un stage", body


def draft_followup(company: dict, last_sent: dict, number: int, label: str, days_since: int) -> str:
    system = f"""Tu rédiges des e-mails de relance courts et polis, en français, pour un étudiant qui cherche un stage.
Règles : 60 à 90 mots maximum ; vouvoiement ; rappelle brièvement la candidature précédente ; réaffirme l'intérêt ;
propose de fournir des informations complémentaires ou d'échanger ; ne culpabilise jamais le destinataire ;
n'invente aucun fait ; aucun texte entre crochets ; aucun markdown ; le CV reste disponible en pièce jointe.
Termine par une formule de politesse puis la signature : {settings.user_name}
Réponds uniquement en JSON : {{"body": "texte brut, paragraphes séparés par une ligne vide"}}"""
    prompt = f"""## Profil de l'étudiant
{load_prompt_file('profil.md')}

## Contexte
Entreprise : {company['name']}
Relance n°{number} ({label}), {days_since} jours après la candidature.
{'C est la dernière relance : reste très courtois et ouvre la porte à un futur échange.' if number >= 2 else ''}

## Dernier e-mail envoyé à l'entreprise
Objet : {last_sent.get('subject') or ''}
{tu.truncate(last_sent.get('body') or '', 1200)}

Rédige la relance."""
    data = _parse_json(generate(prompt, system=system, purpose="relance", json_mode=True,
                                temperature=0.5, max_tokens=2048))
    body = str(data.get("body") or "").strip()
    if not body:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return body


def draft_reply(company: dict, last_inbound: dict) -> str:
    system = f"""Tu rédiges des réponses d'e-mails courtes et polies, en français, pour un étudiant
qui cherche un stage et répond à un recruteur.
Règles : réponds directement au message reçu ; 60 à 120 mots ; vouvoiement ; ton chaleureux et professionnel ;
n'invente aucun fait (reste sur le contenu du message) ; aucun texte entre crochets ; aucun markdown ;
le CV reste disponible en pièce jointe.
Termine par une formule de politesse puis la signature : {settings.user_name}
Réponds uniquement en JSON : {{"body": "texte brut, paragraphes séparés par une ligne vide"}}"""
    prompt = f"""## Profil de l'étudiant
{load_prompt_file('profil.md')}

## Entreprise
{company['name']}

## Message reçu (auquel répondre)
Objet : {last_inbound.get('subject') or ''}
{tu.truncate(last_inbound.get('body') or '', 2000)}

Rédige la réponse."""
    data = _parse_json(generate(prompt, system=system, purpose="reponse", json_mode=True,
                                temperature=0.5, max_tokens=2048))
    body = str(data.get("body") or "").strip()
    if not body:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return body

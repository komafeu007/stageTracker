"""Gemini (API REST) : rédaction des candidatures et des relances.

Seule brique qui produit du texte destiné à être envoyé. La classification des fils
est assurée par l'IA locale (local_ai). Chaque appel est journalisé dans token_log
(alimente la jauge de tokens de l'interface).
"""
import ipaddress
import json
import re
import threading
import time
from urllib.parse import urlparse

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
    if status == 400 and "thinking" in _api_error_message(response).lower() \
            and "thinkingConfig" in body.get("generationConfig", {}):
        # Modèle qui ne connaît pas l'option : on réessaie sans couper le raisonnement.
        without = {**body,
                   "generationConfig": {k: v for k, v in body["generationConfig"].items()
                                        if k != "thinkingConfig"}}
        return _try_model(model, without, purpose)
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
    if candidates[0].get("finishReason") == "MAX_TOKENS":
        # Réponse coupée (souvent le raisonnement qui a mangé le budget) : on retente ailleurs
        # plutôt que de laisser échouer la lecture du JSON.
        return None, f"réponse tronquée ({len(text)} caractères)", False
    return text, "", False


def _thinks_by_default(model: str) -> bool:
    """Vrai si le modèle « réfléchit » par défaut (Gemini 2.5 et les alias `*-latest`).

    Le raisonnement est décompté du budget de sortie : sans le désactiver, il peut tronquer
    la réponse attendue (JSON incomplet). Les modèles plus anciens ignorent l'option.
    """
    return "2.5" in model or model.endswith("-latest")


def _build_body(model: str, system: str, prompt: str, temperature: float,
                max_tokens: int, json_mode: bool) -> dict:
    generation = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        generation["responseMimeType"] = "application/json"
    # Gemini 2.5 « flash » réfléchit par défaut : le raisonnement consomme le
    # budget maxOutputTokens et peut tronquer le JSON attendu. On le désactive.
    if _thinks_by_default(model):
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
    if failures and all("tronquée" in r for r in failures.values()):
        raise AIError(f"Réponse de l'IA coupée : trop longue pour le modèle. Réessaie. ({detail})")
    raise AIError(f"Gemini indisponible (modèles saturés ?). Réessaie dans quelques minutes. ({detail})")


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1:
        raise AIError("Réponse IA illisible (aucun JSON renvoyé).")
    if end < start:
        raise AIError("Réponse IA illisible (JSON tronqué).")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AIError("Réponse IA illisible (JSON invalide).") from exc


# En-têtes qu'un modèle peut recopier en tête du corps (l'objet est un champ à part dans l'appli).
_BODY_HEADERS = {"objet", "subject", "de", "à", "a", "envoyé", "envoyée", "date", "cc"}


def _clean_body(text: str) -> str:
    """Retire un bloc d'en-têtes recopié par le modèle (« Objet : … », « De : … »).

    L'objet est saisi dans un champ séparé : le modèle ne doit pas le remettre dans le corps.
    On ne touche au texte que s'il *commence* par une en-tête, pour ne jamais amputer un vrai
    paragraphe.
    """
    lines = text.strip().splitlines()
    if not lines:
        return ""

    def is_header(line: str) -> bool:
        return ":" in line and line.split(":", 1)[0].strip().lower() in _BODY_HEADERS

    if not is_header(lines[0]):
        return "\n".join(lines).strip()
    while lines and (is_header(lines[0]) or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


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
def _is_private_host(host: str) -> bool:
    """Vrai pour une adresse locale/interne : on ne va pas la chercher (SSRF + bruit inutile).

    Sans ce garde-fou, un site saisi par erreur (`localhost:8000`, `192.168.x.x`…) ferait
    injecter dans le prompt le contenu d'un service local au lieu du site de l'entreprise.
    """
    host = (host or "").lower().strip("[]")
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # nom de domaine : on ne résout pas de DNS supplémentaire
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def fetch_company_context(website: str | None) -> str:
    """Récupère quelques lignes de la page d'accueil pour personnaliser le mail (échec silencieux)."""
    if not website:
        return ""
    url = website if "://" in website else "https://" + website
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or _is_private_host(parsed.hostname or ""):
        return ""
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
- Imite le ton et le style des exemples validés ; seul le 2e paragraphe se reprend (voir ci-dessous).
- Le 2e paragraphe (présentation : formation, période de stage recherchée, objectif professionnel)
  est REPRIS presque mot pour mot du 2e paragraphe des « Exemples validés ». Ne l'invente jamais et
  n'y ajoute aucune date, école, diplôme ou expérience absents des exemples.
- Les autres paragraphes (accroche, motivation, conclusion) restent personnalisés pour l'entreprise.
- Style : aucun superlatif (pas de « exceptionnel », « incontournable », « passionné », « leader »,
  « idéal »…), aucun mot rare ou compliqué. Vocabulaire simple et courant, phrases courtes.
- Adapte le projet présenté au domaine de l'entreprise : déduis son activité (robotique et bras
  robotiques, vision/perception, IoT et systèmes embarqués, données/IA, web, infrastructure/DevOps…)
  du nom et du contexte fourni, puis choisis dans le profil de l'étudiant le projet qui colle le mieux
  à ce domaine. Exemples de correspondance : bras robotique → modélisation et robotique ; vision/
  perception → LiDAR, traitement d'images ; IoT/embarqué → capteurs, ESP32, microcontrôleurs ;
  infra/DevOps → Docker, Proxmox, serveurs. Mentionne uniquement les projets pertinents (1 à 2) et
  jamais un projet hors sujet ; n'invente aucun projet que le profil ne contient pas.
- Si un « Secteur » est renseigné dans la fiche entreprise, c'est la référence : c'est lui qui
  détermine le domaine à viser (ex. « Vision / Perception » → projet de perception ; « Robotique » →
  projet robotique).
- N'invente aucun fait sur l'étudiant (utilise uniquement son profil) ni sur l'entreprise
  (utilise uniquement le contexte fourni ; s'il est vide ou insuffisant, reste sobre).
- Aucun texte entre crochets, aucun placeholder, aucun markdown. Vouvoiement.
- Indique que le CV est joint à l'e-mail.
- Le champ « body » ne contient QUE le corps du message : jamais de ligne « Objet : », jamais
  l'objet recopié, aucune en-tête ; il commence directement par la formule d'appel.
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
Secteur (ce que fait l'entreprise) : {company.get('sector') or 'non renseigné'}
Site web : {company.get('website') or 'non renseigné'}
Contexte récupéré sur le site (peut être vide ou imparfait) :
{context or '(aucun)'}

Rédige l'e-mail de candidature.
1. Le domaine à viser est le « Secteur » ci-dessus s'il est renseigné ; sinon déduis-le du nom et du
   contexte.
2. Choisis dans le profil de l'étudiant le ou les projets/compétences les plus pertinents pour ce
   domaine (ex. bras robotique → modélisation/robotique ; vision/perception → LiDAR, traitement
   d'images ; IoT/embarqué → capteurs, ESP32 ; infra → Docker, Proxmox), et mentionne uniquement
   ceux-là : jamais un projet hors sujet.
3. Réutilise le 2e paragraphe des exemples validés (formation, disponibilité, objectif) sans le
   réinventer."""
    data = _parse_json(generate(prompt, system=_application_system(), purpose="candidature",
                                json_mode=True, temperature=0.6))
    subject = str(data.get("subject") or "").strip().replace("\n", " ")
    body = _clean_body(str(data.get("body") or ""))
    if not body:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return subject or "Candidature pour un stage", body


def draft_followup(company: dict, last_sent: dict, number: int, label: str, days_since: int) -> str:
    system = f"""Tu rédiges des e-mails de relance courts et polis, en français, pour un étudiant qui cherche un stage.
Règles : 60 à 90 mots maximum ; vouvoiement ; rappelle brièvement la candidature précédente ; réaffirme l'intérêt ;
propose de fournir des informations complémentaires ou d'échanger ; ne culpabilise jamais le destinataire ;
n'invente aucun fait ; aucun texte entre crochets ; aucun markdown ; le CV reste disponible en pièce jointe ;
aucun superlatif ni mot compliqué, vocabulaire simple et courant, phrases courtes.
Écris UNIQUEMENT le corps : aucune ligne « Objet : », aucun sujet, aucune en-tête, aucune signature
d'e-mail ; commence directement par la formule d'appel (Madame, Monsieur,).
Termine par une formule de politesse puis la signature : {settings.user_name}
Réponds uniquement en JSON : {{"body": "texte brut, paragraphes séparés par une ligne vide"}}"""
    prompt = f"""## Profil de l'étudiant
{load_prompt_file('profil.md')}

## Contexte
Entreprise : {company['name']}
Relance n°{number} ({label}), {days_since} jours après le dernier e-mail que nous avons envoyé.
{'C est la dernière relance : reste très courtois et ouvre la porte à un futur échange.' if number >= 2 else ''}

## Dernier e-mail envoyé à l'entreprise (pour contexte, à ne pas recopier)
Sujet : {last_sent.get('subject') or ''}
Corps :
{tu.truncate(last_sent.get('body') or '', 1200)}

Rédige la relance. Rappel : uniquement le corps du message, sans ligne d'objet ni en-tête."""
    data = _parse_json(generate(prompt, system=system, purpose="relance", json_mode=True,
                                temperature=0.5, max_tokens=2048))
    body = _clean_body(str(data.get("body") or ""))
    if not body:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return body


def draft_paragraph(company: dict, context: str, paragraphs: list[str], index: int,
                    instruction: str = "") -> str:
    """Réécrit un seul paragraphe d'un e-mail, sans toucher aux autres."""
    system = """Tu réécris UN SEUL paragraphe d'un e-mail de candidature de stage déjà rédigé, en français.
Règles :
- Renvoie uniquement le texte de ce paragraphe : pas de salutation, pas de signature, pas de numéro,
  aucun markdown, aucun texte entre crochets.
- Garde le même rôle que le paragraphe d'origine (introduction, parcours, motivation, conclusion…).
- Reste cohérent avec le reste du mail fourni, sans le répéter.
- N'invente aucun fait sur l'étudiant (utilise uniquement son profil) ni sur l'entreprise.
- Aucun superlatif ni mot compliqué : vocabulaire simple et courant, phrases courtes. Vouvoiement.
- S'il est demandé une autre version, propose une formulation nettement différente de l'actuelle.
Réponds uniquement en JSON : {"text": "nouveau paragraphe"}"""
    numbered = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(paragraphs))
    second = index == 1
    prompt = f"""## Profil de l'étudiant
{load_prompt_file('profil.md')}

## Schéma du cours à respecter
{load_prompt_file('schema_cours.md')}

## Exemples validés
{load_prompt_file('exemples_valides.md')}

## Entreprise ciblée
Nom : {company['name']}
Secteur (ce que fait l'entreprise) : {company.get('sector') or 'non renseigné'}
Contexte récupéré sur le site (peut être vide) :
{context or '(aucun)'}

## E-mail actuel (paragraphes numérotés)
{numbered}

## Paragraphe à réécrire : le n°{index + 1} (texte actuel ci-dessous)
{paragraphs[index]}
{f'''## Consigne de l'étudiant
{instruction}''' if instruction.strip() else ''}
{'''## Rappel
C'est le 2e paragraphe : reprends une AUTRE version de ce paragraphe depuis les exemples validés.''' if second else ''}
Propose une nouvelle version de ce paragraphe."""
    data = _parse_json(generate(prompt, system=system, purpose="paragraphe", json_mode=True,
                                temperature=0.7, max_tokens=2048))
    text = str(data.get("text") or "").strip()
    if not text:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return text


def correct_spelling(text: str) -> str:
    """Corrige uniquement l'orthographe d'un texte (sans reformuler ni changer le contenu)."""
    system = """Tu es un correcteur d'orthographe et de grammaire pour des e-mails en français.
Règles STRICTES :
- Corrige uniquement l'orthographe, la grammaire, la conjugaison, la ponctuation, les accents
  et les majuscules.
- Ne reformule pas, ne change aucun mot ni aucune tournure, n'ajoute et ne supprime aucune phrase.
- Ne touche ni aux noms propres, ni aux chiffres, ni aux adresses e-mail, ni aux liens.
- Conserve exactement la mise en page : mêmes paragraphes, mêmes retours à la ligne.
- En cas de doute sur un mot ou un accord, laisse-le tel quel plutôt que d'inventer.
Réponds uniquement en JSON : {"body": "texte corrigé, identique hormis les fautes"}"""
    prompt = (
        "Corrige uniquement les fautes d'orthographe du texte ci-dessous, "
        "sans rien changer d'autre (même contenu, même style, même mise en page) :\n\n"
        f"{text}"
    )
    data = _parse_json(generate(prompt, system=system, purpose="correction", json_mode=True,
                                temperature=0.0, max_tokens=4096))
    body = str(data.get("body") or "").strip()
    if not body:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return body


def draft_reply(company: dict, last_inbound: dict) -> str:
    system = f"""Tu rédiges des réponses d'e-mails courtes et polies, en français, pour un étudiant
qui cherche un stage et répond à un recruteur.
Règles : réponds directement au message reçu ; 60 à 120 mots ; vouvoiement ; ton chaleureux et professionnel ;
n'invente aucun fait (reste sur le contenu du message) ; aucun texte entre crochets ; aucun markdown ;
le CV reste disponible en pièce jointe ; aucun superlatif ni mot compliqué, vocabulaire simple et courant.
Écris UNIQUEMENT le corps : aucune ligne « Objet : », aucun sujet, aucune en-tête, aucune signature
d'e-mail ; commence directement par la formule d'appel (Madame, Monsieur, / Bonjour,).
Termine par une formule de politesse puis la signature : {settings.user_name}
Réponds uniquement en JSON : {{"body": "texte brut, paragraphes séparés par une ligne vide"}}"""
    prompt = f"""## Profil de l'étudiant
{load_prompt_file('profil.md')}

## Entreprise
{company['name']}

## Message reçu (auquel répondre, à ne pas recopier)
Sujet du fil : {last_inbound.get('subject') or ''}
Corps :
{tu.truncate(last_inbound.get('body') or '', 2000)}

Rédige la réponse. Rappel : uniquement le corps du message, sans ligne d'objet ni en-tête."""
    data = _parse_json(generate(prompt, system=system, purpose="reponse", json_mode=True,
                                temperature=0.5, max_tokens=2048))
    body = _clean_body(str(data.get("body") or ""))
    if not body:
        raise AIError("L'IA n'a produit aucun texte. Réessaie.")
    return body

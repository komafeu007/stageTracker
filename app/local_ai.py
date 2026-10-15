"""Classification locale des e-mails via Ollama.

Rôle unique d'Ollama dans l'application : lire un fil et décider
1) s'il concerne la recherche de stage (pertinence),
2) l'entreprise concernée,
3) l'issue de la dernière réponse (refus / positif / neutre).

La rédaction et l'envoi des e-mails sont assurés par Gemini (gemini_client).
"""
import json
import logging
import re
from typing import Any

import httpx

from . import textutils as tu
from .config import settings
from .constants import OUTCOME_NONE, OUTCOMES

log = logging.getLogger("stage-tracker.local_ai")

SYSTEM_PROMPT = """Tu analyses des fils d'e-mails d'une boîte Gmail : un étudiant y suit ses candidatures de stage.
Décide si le fil concerne la RECHERCHE DE STAGE : candidature envoyée par l'étudiant, réponse d'une
entreprise ou d'un recruteur, proposition d'entretien, refus, relance.
NE SONT PAS pertinents : newsletters, publicités, alertes d'offres génériques, notifications de
réseaux sociaux, factures, e-mails scolaires sans lien avec la recherche de stage, spams.
Réponds uniquement en JSON, sans texte autour :
{"relevant": true|false, "company_name": "nom ou ''", "outcome": "none|positive|refusal", "summary": "phrase"}
- outcome = "refusal" si la dernière réponse de l'entreprise est un refus ;
- outcome = "positive" si elle propose un entretien, un échange ou montre de l'intérêt ;
- outcome = "none" sinon (aucune réponse d'entreprise, accusé de réception...)."""

# Longueur du contexte envoyé au modèle (en caractères).
_PROMPT_BUDGET = 3500
_MSG_CHARS = 700
_MAX_MESSAGES = 6


class LocalAIError(Exception):
    """Erreur d'appel à l'IA locale (message lisible par l'utilisateur)."""


def enabled() -> bool:
    return settings.ollama_enabled


# --------------------------------------------------------------------------- appel bas niveau
def _generate(system: str, prompt: str, *, temperature: float = 0.1, max_tokens: int = 1024) -> str:
    """Appelle /api/chat d'Ollama et renvoie le contenu texte de la réponse."""
    if not enabled():
        raise LocalAIError("L'IA locale (Ollama) est désactivée : positionne OLLAMA_ENABLED=true.")
    payload: dict[str, Any] = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    try:
        response = httpx.post(f"{settings.ollama_host}/api/chat", json=payload, timeout=120)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LocalAIError(
            f"Ollama injoignable sur {settings.ollama_host} (démarre « ollama serve ») : {exc}"
        ) from exc
    content = ((response.json().get("message") or {}).get("content") or "").strip()
    if not content:
        raise LocalAIError("Réponse vide d'Ollama.")
    return content


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise LocalAIError("Réponse IA illisible (JSON attendu).")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LocalAIError("Réponse IA illisible (JSON invalide).") from exc


# --------------------------------------------------------------------------- classification
def _build_prompt(subject: str, messages: list[dict]) -> str:
    lines = [f"Objet : {subject or '(sans objet)'}"]
    budget = _PROMPT_BUDGET
    for message in messages[-_MAX_MESSAGES:]:
        tag = "ENVOYÉ PAR L'ÉTUDIANT" if message["direction"] == "out" else "REÇU"
        chunk = f"[{tag} le {message['sent_at'][:10]}] {tu.truncate(message['body'], _MSG_CHARS)}"
        budget -= len(chunk)
        if budget < 0:
            break
        lines.append(chunk)
    return "\n\n".join(lines)


def classify_thread(subject: str, messages: list[dict]) -> dict:
    """Classe un fil : pertinence, nom d'entreprise, issue et résumé.

    Lève LocalAIError en cas d'échec (appelant libre de retomber sur un mode dégradé).
    """
    if not messages:
        return {"relevant": False, "company_name": "", "outcome": OUTCOME_NONE, "summary": ""}
    raw = _generate(SYSTEM_PROMPT, _build_prompt(subject, messages))
    data = _parse_json(raw)
    outcome = data.get("outcome") if data.get("outcome") in OUTCOMES else OUTCOME_NONE
    return {
        "relevant": bool(data.get("relevant")),
        "company_name": str(data.get("company_name") or "").strip(),
        "outcome": outcome,
        "summary": str(data.get("summary") or "").strip(),
    }

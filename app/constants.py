"""Valeurs métier partagées : statuts d'entreprise et types de brouillon.

Centralisées ici pour éviter les chaînes magiques dispersées dans le code.
"""

# --- Statuts stockés dans company.status -------------------------------------
PROSPECT = "prospect"   # ajoutée à la main, jamais contactée
PENDING = "pending"     # candidature envoyée, en attente de réponse
REPLIED = "replied"     # une réponse a été reçue
REFUSED = "refused"     # réponse négative
IGNORED = "ignored"     # faux positif, rangée dans la corbeille

# Statut d'affichage dérivé : `pending` dont la relance est due.
FOLLOWUP = "followup"

STORED_STATUSES = frozenset({PROSPECT, PENDING, REPLIED, REFUSED, IGNORED})
# Statuts qu'un utilisateur peut forcer manuellement depuis l'interface.
FORCEABLE_STATUSES = frozenset({PENDING, FOLLOWUP, REPLIED, REFUSED})

# --- Types de brouillon / d'envoi --------------------------------------------
APPLICATION = "application"
REPLY = "reply"
DRAFT_KINDS = frozenset({APPLICATION, FOLLOWUP, REPLY})

# --- Résultat d'analyse d'un fil (IA) ----------------------------------------
OUTCOME_NONE = "none"
OUTCOME_POSITIVE = "positive"
OUTCOME_REFUSAL = "refusal"
OUTCOMES = frozenset({OUTCOME_NONE, OUTCOME_POSITIVE, OUTCOME_REFUSAL})

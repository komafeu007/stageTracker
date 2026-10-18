# Fonctionnement détaillé

> Détails internes de Stage Tracker : comment l'application classe les mails, rédige les
> brouillons et calcule les relances.
> Pour installer et utiliser l'application, voir le [README](../README.md).

---

## Vue d'ensemble

Une seule application FastAPI (donc **un seul worker**) sert l'API JSON et une page HTML statique
(Vue + Tailwind via CDN). L'état vit dans une base **SQLite** locale. Une tâche de fond démarre avec
le processus :

1. sync Gmail (scan complet au premier lancement, puis delta via `history.list`) ;
2. recalcul des échéances de relance et notifications.

Docker et `uvicorn` écrivent tous les deux dans `./data` et écoutent sur le port 8000 : il ne faut
en lancer qu'un à la fois.

---

## 1. Synchronisation Gmail (`app/sync.py`)

```
Fil Gmail
   │
   ▼
1. Pré-filtre par mots-clés (GMAIL_KEYWORDS)  → les mails hors sujet sont écartés sans IA
   │
   ▼
2. Entreprise déjà connue ? (même adresse ou même domaine)  → rattachement direct
   │
   ▼
3. Classification par Ollama : pertinent ? quelle entreprise ? refus / positif ? résumé ? secteur ?
   │
   ▼
4. Sinon repli : entreprise déduite du domaine de l'adresse
```

Le premier passage lit jusqu'à `SCAN_MAX_THREADS` fils ; ensuite seul l'historique nouveau est
relu. Les mails écartés sont mémorisés (`skipped_thread`) pour ne pas être réexaminés.

---

## 2. Classification — Ollama, en local (`app/local_ai.py`)

Ollama renvoie, pour un fil : la **pertinence**, le **nom de l'entreprise**, l'issue
(**refus** / **positif**), un **résumé** et le **secteur d'activité**. Il n'écrit jamais de texte
destiné à être envoyé.

Si Ollama est indisponible (`OLLAMA_ENABLED=false` ou serveur injoignable), la synchronisation
continue : le pré-filtre par mots-clés reste actif, le nom d'entreprise est déduit du domaine de
l'adresse, et le secteur est deviné par mots-clés (`textutils.guess_sector`).

---

## 3. Rédaction — Gemini (`app/gemini_client.py`)

Gemini ne sert **qu'à rédiger**, à la demande :

| Ce qu'il produit | Rôle |
|---|---|
| Candidature | accroche + parcours + motivation + conclusion |
| Relance | rappel court et poli de la candidature |
| Réponse | réponse au dernier message reçu |
| Révision d'un paragraphe | réécrit **un seul** paragraphe, sans toucher aux autres |
| Correction d'orthographe | corrige uniquement les fautes, **sans** reformuler |

Le contexte du site de l'entreprise (titre, description, titres, extrait) est récupéré au
passage pour personnaliser la candidature.

Règles de style imposées aux brouillons :

- **aucun superlatif ni mot compliqué** (vocabulaire simple, phrases courtes) ;
- le corps ne contient **jamais** de ligne « Objet : » ni d'en-tête : l'objet est un champ séparé
  (un nettoyage `_clean_body()` retire en plus tout bloc d'en-têtes recopié par le modèle) ;
- le **2e paragraphe** des candidatures reprend celui des exemples validés (formation,
  disponibilité, objectif) plutôt que d'être réinventé ;
- le **projet mis en avant** est choisi selon le secteur de l'entreprise (bras robotique, vision,
  IoT, infrastructure…), à partir du profil de l'étudiant.

Côté fiabilité : les appels sont espacés de `AI_MIN_INTERVAL_SECONDS` (limite les erreurs 429) et
réessayés sur 429/5xx ; si le modèle principal reste indisponible, `GEMINI_FALLBACK_MODELS` sont
essayés dans l'ordre. Le « raisonnement » des modèles Gemini 2.5 et des alias `*-latest` est
désactivé (`thinkingBudget = 0`) car il consommait le budget de sortie et tronquait la réponse.
Chaque appel est journalisé dans `token_log`, qui alimente la jauge de tokens.

---

## 4. Secteur d'activité

`company.sector` est un **champ libre**. L'interface propose des sous-domaines de l'informatique
(`constants.SECTORS` : développement, web, mobile, robotique, vision, IA/data, embarqué/IoT,
réseau/infra, cybersécurité, cloud/DevOps, jeux vidéo, recherche) mais accepte n'importe quelle
valeur saisie.

Il est déduit du contenu des mails (Ollama, sinon mots-clés) et **jamais écrasé** s'il a déjà une
valeur ; `sync.backfill_sectors()` remplit les entreprises qui n'en ont pas à la fin de chaque
synchro. Il sert à choisir le projet présenté dans la candidature et filtre la barre de gauche.

---

## 5. Statuts et relances (`app/services.py`)

Statut d'une entreprise, recalculé depuis l'historique (`recompute_company`) :

| Statut | Sens |
|---|---|
| `prospect` | entreprise connue, pas encore contactée |
| `pending` | candidature envoyée, **dernier** message sortant |
| `replied` | le recruteur a répondu |
| `refused` | réponse négative détectée par l'IA locale (`ai_label`) |
| `ignored` | corbeille (réversible) |

- Un `PATCH` manuel du statut pose `status_manual = 1`, qui prime **jusqu'au message suivant**.
- `anchor_at` = date du **dernier mail envoyé** (pas de la première candidature). C'est le départ
  du décompte : la relance suivante est due à `anchor_at + FOLLOWUP_DAYS[followup_count]`
  (par défaut `FOLLOWUP_DAYS=7,14`, donc J+7 puis J+14). `followup_count` = nombre de relances
  déjà envoyées.
- `FOLLOWUP_MAX_AGE_DAYS` arrête les relances des candidatures trop anciennes.
- `services.recompute_followups()` recalcule tout à chaque synchro : seule la date fait évoluer
  une échéance, il n'est pas besoin d'un nouveau mail pour que l'état change. Les notifications
  de relance devenues obsolètes sont purgées au passage.

---

## 6. Données (SQLite, `app/db.py`)

Le schéma est appliqué de façon idempotente au démarrage ; les colonnes ajoutées après coup sont
déclarées dans `_ADDED_COLUMNS` (pas d'outil de migration).

| Table | Contenu |
|---|---|
| `company` | entreprises (nom, site, e-mail, domaine, secteur, statut, ancre de relance…) |
| `thread` | fils Gmail |
| `message` | messages d'un fil (direction `in`/`out`, objet, corps, date) |
| `draft` | brouillon édité en cours (un par entreprise) |
| `notification` | journal des relances/réponses à traiter |
| `token_log` | consommation Gemini (jauge de tokens) |
| `skipped_thread` | fils écartés par le pré-filtre |
| `kv` | petites valeurs de service (adresse Gmail, `history_id`, nom du CV…) |

Sous `DATA_DIR` (par défaut `./data`, `/data` dans Docker) : la base, `cv_active.pdf`,
`token.json`, `oauth_pending.json` (connexion Gmail en cours) et `prompts/`.
`credentials.json` vit sous `SECRETS_DIR`.

---

## 7. Structure du projet

```
stageTracker/
├── app/
│   ├── main.py            # application FastAPI (lifespan, routeurs, sécurité)
│   ├── api/               # routes HTTP, un module par domaine
│   │   ├── system.py      # /, /api/health, /api/status, /api/tokens
│   │   ├── auth.py        # OAuth Gmail, /api/sync
│   │   ├── companies.py   # entreprises et fils
│   │   ├── mail.py        # brouillons et envoi
│   │   ├── messages.py    # réaffectation manuelle d'un mail
│   │   ├── cv.py          # import / téléchargement du CV
│   │   ├── notifications.py
│   │   └── deps.py        # modèles de requête + helpers partagés
│   ├── config.py          # configuration (variables d'environnement)
│   ├── constants.py       # statuts et types métier partagés
│   ├── db.py              # schéma SQLite + helpers
│   ├── sync.py            # pipeline Gmail → SQLite
│   ├── gmail_client.py    # client Gmail (OAuth, lecture, envoi)
│   ├── services.py        # statuts, relances, notifications, brouillons
│   ├── local_ai.py        # classification locale (Ollama)
│   ├── gemini_client.py   # rédaction (Gemini)
│   ├── textutils.py       # utilitaires texte (HTML, adresses, domaines)
│   ├── security.py        # mot de passe optionnel + anti-CSRF
│   └── static/index.html  # interface (Vue 3 + Tailwind via CDN)
├── data/                  # ⚠️ local, non versionné (sauf modèles de prompts)
│   ├── stage_tracker.db   # base SQLite
│   ├── token.json         # jeton Gmail
│   ├── cv_active.pdf      # CV importé
│   └── prompts/           # *.md.example versionnés, *.md locaux
├── secrets/               # ⚠️ local, non versionné
│   └── credentials.json   # identifiant OAuth Google Cloud
├── docs/                  # cette documentation technique
├── tests/                 # tests hors-ligne (unittest)
├── .env.example           # modèle de configuration
├── docker-compose.yml     # exécution en conteneur
├── Dockerfile
├── pyproject.toml         # configuration ruff (conventions de code)
├── requirements.txt
└── requirements-dev.txt   # outils de développement (ruff)
```

---

## 8. Sécurité (`app/security.py`)

- ❌ **Aucun envoi automatique** : l'envoi est toujours une action de l'utilisateur.
- 🔑 **Mot de passe optionnel** : `APP_PASSWORD` déclenche une authentification Basic.
  Seule exception : `/api/health` (sonde du conteneur), qui ne renvoie que `{"ok": true}`.
- 🛡️ **Anti-CSRF** : toute requête non-`GET` doit porter l'en-tête
  `X-Requested-With: stagetracker` ; un site tiers ne peut pas la forger en cross-site.
- 🔒 **Secrets hors Git** : `.env`, `secrets/` et `data/` sont dans `.gitignore` ; seuls les
  modèles `*.md.example` (prompts) sont versionnés. `token.json` et `oauth_pending.json` sont
  écrits en `0600`.
- 🔐 **OAuth** : PKCE, `state` vérifié au retour de Google, état conservé sur disque pour
  survivre à un redémarrage entre `/auth/login` et `/auth/callback`.
- 🧼 **HTML nettoyé avant envoi** : le corps rédigé passe par `sanitize_html` ; seules les
  balises de mise en forme simples sont conservées.
- 🌐 **Site de l'entreprise lu avec prudence** : seules les adresses publiques `http(s)` sont
  récupérées pour le contexte ; `localhost`, les domaines internes et les IP privées sont ignorés.
- 📎 **Pièces jointes bornées** : le CV est limité à 10 Mo, le total des pièces jointes à 20 Mo,
  et la lecture est plafonnée (on ne charge pas un fichier entier en mémoire avant de le refuser).

> ⚠️ Si une clé a pu être exposée (commit, capture, dépôt public), **révoque-la** :
> Google Cloud → *Clients* pour le client OAuth, Google AI Studio pour la clé Gemini.

---

## 9. Tests et qualité de code

Les tests sont **hors-ligne** : Gmail, Ollama et Gemini sont simulés.

```bash
python -m unittest discover -v
```

Conventions de code (ruff) :

```bash
pip install -r requirements-dev.txt
ruff check .          # vérifie
ruff check --fix .    # corrige ce qui peut l'être
```

Conventions : code et identifiants en anglais, commentaires et textes utilisateur en français,
lignes ≤ 120 caractères, `snake_case` / `PascalCase` / `UPPER_CASE`.

---

## 10. Modifier le code

Les règles à respecter (répartition Ollama/Gemini, style des brouillons, schéma, relances) sont
résumées dans [`AGENTS.md`](../AGENTS.md), à lire avant toute modification.

# Stage Tracker

> Application web **locale et mono-utilisateur** pour suivre tes candidatures de stage :
> elle lit ta boîte Gmail, classe les fils automatiquement, rédige candidatures et relances
> avec l'IA, et **n'envoie jamais un e-mail sans ton clic**.

FastAPI + SQLite + une page unique (Vue 3 / Tailwind via CDN). Aucune donnée ne quitte ta
machine, sauf ce que tu envoies volontairement à Google (Gmail, Gemini).

---

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Comment ça marche](#comment-ça-marche)
- [Prérequis](#prérequis)
- [Installation](#installation)
  - [Option A — Docker (recommandé sous Linux)](#option-a--docker-recommandé-sous-linux)
  - [Option B — Python local (Linux / macOS / Windows)](#option-b--python-local-linux--macos--windows)
- [Configurer les services externes](#configurer-les-services-externes)
  - [1. Gmail via Google Cloud (obligatoire)](#1-gmail-via-google-cloud-obligatoire)
  - [2. Ollama — IA locale de classification (recommandé)](#2-ollama--ia-locale-de-classification-recommandé)
  - [3. Gemini — rédaction des e-mails (optionnel)](#3-gemini--rédaction-des-e-mails-optionnel)
- [Configuration de l'application (`.env`)](#configuration-de-lapplication-env)
- [Personnaliser les prompts (ton profil)](#personnaliser-les-prompts-ton-profil)
- [Premier lancement](#premier-lancement)
- [Utilisation](#utilisation)
- [Structure du projet](#structure-du-projet)
- [Tests et qualité de code](#tests-et-qualité-de-code)
- [Sauvegarder, réinstaller, changer de machine](#sauvegarder-réinstaller-changer-de-machine)
- [Dépannage](#dépannage)
- [Sécurité et confidentialité](#sécurité-et-confidentialité)
- [Licence](#licence)

---

## Fonctionnalités

- **Synchronisation Gmail** : scan complet au premier lancement, puis synchronisation
  incrémentale (delta) en tâche de fond.
- **Pré-filtre par mots-clés** : les mails hors sujet ne sont jamais envoyés à l'IA.
- **Classification par IA locale (Ollama)** : pertinence, nom de l'entreprise, refus ou
  réponse positive, résumé, **secteur d'activité**.
- **Rédaction assistée (Gemini)** : brouillons de candidature, de relance et de réponse, plus
  **correction d'orthographe à la demande** (corrige les fautes **sans** réécrire le contenu) et
  **révision paragraphe par paragraphe** (l'IA propose une autre version d'un seul paragraphe).
  Le style reste simple : **aucun superlatif ni mot compliqué**.
  Le 2e paragraphe des candidatures reprend celui des exemples validés, et le projet présenté est
  choisi selon l'activité de l'entreprise (bras robotique, vision, IoT, infrastructure…), sans
  jamais citer un projet hors sujet.
- **Relances proposées** `FOLLOWUP_DAYS` jours après **le dernier mail envoyé** (J+7 puis J+14
  par défaut), recalculées à chaque synchro ; arrêt automatique au bout de `FOLLOWUP_MAX_AGE_DAYS`.
- **Correction manuelle** : renommer une entreprise, déplacer un mail (ou toute une
  conversation) vers une autre entreprise.
- **Secteur d'activité** : chaque entreprise peut être rangée dans un sous-domaine IT
  (développement, web, robotique, vision, infra…). **Déduit automatiquement du contenu des mails**
  échangés (IA locale Ollama, sinon mots-clés), il peut être corrigé à la main. Le secteur est
  utilisé pour choisir le projet mis en avant dans la candidature, et la barre de gauche se filtre
  par secteur.
- **Jauge de tokens**, centre de notifications, corbeille pour les faux positifs.
- **Jamais d'envoi automatique** : tout passe par le bouton « Envoyer ».

## Comment ça marche

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

**Répartition des rôles (à ne pas inverser)** :

| Rôle | Service | Fichier |
|------|---------|---------|
| Classer les fils | **Ollama** (local) | `app/local_ai.py` |
| Rédiger les e-mails | **Gemini** (API Google) | `app/gemini_client.py` |

Si Ollama est indisponible, la synchronisation continue (filtrage par mots-clés + nom
d'entreprise déduit du domaine) ; la rédaction, elle, nécessite Gemini.

---

## Prérequis

| Prérequis | Version | Remarque |
|-----------|---------|----------|
| Python | **3.11+** | nécessaire seulement pour l'installation locale |
| Docker + Docker Compose | récent | optionnel (Option A) |
| Git | — | pour cloner le dépôt |
| Compte Gmail | — | la boîte à analyser |
| Ollama | — | IA locale de classification (gratuit, recommandé) |
| Clé API Gemini | — | optionnel : uniquement pour la rédaction |

---

## Installation

### Option A — Docker (recommandé sous Linux)

```bash
git clone https://github.com/komafeu007/stageTracker.git
cd stageTracker

cp .env.example .env          # puis édite .env (voir plus bas)
mkdir -p secrets data
# place ton credentials.json dans secrets/ (voir « Configurer Gmail »)

docker compose up -d --build
```

L'application est ensuite sur **http://localhost:8000**.

- Le conteneur utilise `network_mode: host` : il partage le réseau de la machine, ce qui lui
  permet de joindre Ollama qui n'écoute que sur `127.0.0.1`.
- L'adresse d'écoute se règle par `APP_BIND` / `APP_PORT` dans `.env` (par défaut
  `127.0.0.1:8000`).
- `./data` et `./secrets` sont montés dans le conteneur : tes données restent sur la machine.

> ⚠️ **macOS / Windows (Docker Desktop)** : le réseau hôte s'y comporte différemment. Le plus
> simple est d'utiliser l'installation locale (Option B) ou, dans `docker-compose.yml`, de
> remplacer `network_mode: host` par un mapping de ports et de pointer `OLLAMA_HOST` vers
> `http://host.docker.internal:11434`.

### Option B — Python local (Linux / macOS / Windows)

```bash
git clone https://github.com/komafeu007/stageTracker.git
cd stageTracker

python -m venv venv
source venv/bin/activate          # Windows : venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env              # puis édite .env
mkdir -p data secrets
# place ton credentials.json dans secrets/

uvicorn app.main:app --reload --port 8000
```

Puis ouvre **http://localhost:8000**.

> ⚠️ Lance **un seul worker** (`uvicorn` par défaut). La synchronisation Gmail tourne dans un
> thread de fond du processus : plusieurs workers la dupliqueraient.

> ℹ️ N'utilise pas Docker et `uvicorn` en même temps : les deux écrivent dans `./data` et
> écoutent sur le port 8000.

---

## Configurer les services externes

### 1. Gmail via Google Cloud (obligatoire)

Il faut créer un identifiant OAuth « application de bureau ». Compte ~10 minutes.

1. Va sur <https://console.cloud.google.com> et **crée un projet** (ex. `stage-tracker`).
2. **API et services → Bibliothèque** → cherche **Gmail API** → **Activer**.
3. **Google Auth Platform** (écran de consentement OAuth) → **Commencer** :
   - Nom de l'application : `Stage Tracker`
   - E-mail d'assistance : le tien
   - Type d'utilisateur : **Externe**
   - Ajoute **ton adresse Gmail** comme utilisateur test, accepte les conditions, **Créer**.
4. **Audience → Utilisateurs tests** → ajoute ton adresse Gmail.
   (Pour un usage durable, tu peux ensuite **Publier l'application**.)
5. **Clients → Créer un client** → type **Application de bureau** → nom libre → **Créer**.
6. **Télécharge le JSON**, renomme-le en `credentials.json` et place-le dans `secrets/`.

Scopes demandés par l'application (visibles à la connexion) :

- `gmail.readonly` — lire les fils pour les classer ;
- `gmail.send` — envoyer un e-mail **uniquement quand tu cliques sur « Envoyer »**.

> 🔒 `credentials.json` et `data/token.json` sont des secrets : ne les partage jamais et ne les
> commite jamais (ils sont dans `.gitignore`).

### 2. Ollama — IA locale de classification (recommandé)

Ollama analyse les fils **sur ta machine** (aucun envoi de mail à un service tiers).

```bash
# Linux / macOS
curl -fsSL https://ollama.com/install.sh | sh

# Windows : installeur sur https://ollama.com/download
```

Puis télécharge un modèle et laisse le serveur tourner :

```bash
ollama pull llama3.2:1b     # ~1,5 Go, très rapide (recommandé)
# ou, plus précis mais plus lourd :
ollama pull mistral:7b      # ~4,5 Go

ollama serve                # ou : le service démarre tout seul après installation
```

Dans `.env` :

```bash
OLLAMA_ENABLED=true
OLLAMA_HOST=http://localhost:11434   # sous Docker : http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2:1b
```

> Sans Ollama, l'application fonctionne quand même : la classification retombe sur un
> filtrage par mots-clés et un nom d'entreprise déduit du domaine.

### 3. Gemini — rédaction des e-mails (optionnel)

Gemini rédige les brouillons. Sans clé, l'application reste utilisable : tu écris tes mails
à la main, mais les boutons « Rédiger avec l'IA » ne fonctionnent pas.

1. Va sur <https://aistudio.google.com/apikey>.
2. **Create API key** → choisis (ou crée) un projet Google Cloud → copie la clé.
3. Colle-la dans `.env` :

```bash
GEMINI_API_KEY=ta_cle_ici
GEMINI_MODEL=gemini-flash-latest
GEMINI_FALLBACK_MODELS=gemini-2.5-flash,gemini-flash-lite-latest
```

Les modèles de repli sont essayés dans l'ordre si le principal est saturé (erreur 503).

---

## Configuration de l'application (`.env`)

```bash
cp .env.example .env
```

`.env` n'est jamais versionné. Variables disponibles :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `USER_FULL_NAME` | `Prénom Nom` | Nom utilisé dans la signature des e-mails générés |
| `GEMINI_API_KEY` | *(vide)* | Clé Gemini (vide = rédaction IA désactivée) |
| `GEMINI_MODEL` | `gemini-flash-latest` | Modèle principal |
| `GEMINI_FALLBACK_MODELS` | `gemini-2.5-flash,gemini-flash-lite-latest` | Modèles de repli (503) |
| `GEMINI_MONTHLY_TOKEN_BUDGET` | `1000000` | Budget affiché dans la jauge de tokens |
| `AI_MIN_INTERVAL_SECONDS` | `4` | Pause minimale entre deux appels IA |
| `OLLAMA_ENABLED` | `true` | Active la classification locale |
| `OLLAMA_HOST` | `http://localhost:11434` | URL du serveur Ollama |
| `OLLAMA_MODEL` | `llama3.2:1b` | Modèle Ollama utilisé |
| `GMAIL_KEYWORDS` | `stage,stagiaire,candidature,alternance,apprentissage,internship,recrutement` | Mots-clés du pré-filtre (**ajoute ton domaine**) |
| `FOLLOWUP_DAYS` | `7,14` | Délai (jours) après le dernier mail envoyé, pour chaque relance |
| `FOLLOWUP_MAX_AGE_DAYS` | `60` | Arrêt des relances au-delà |
| `SYNC_INTERVAL_MINUTES` | `10` | Fréquence de la synchro de fond |
| `SCAN_MAX_THREADS` | `300` | Fils max lus au premier scan |
| `APP_PASSWORD` | *(vide)* | Mot de passe de l'interface (vide = aucun) |
| `APP_BIND` | `127.0.0.1` | Interface d'écoute (`0.0.0.0` = réseau local) |
| `APP_PORT` | `8000` | Port d'écoute |
| `OAUTH_REDIRECT_URI` | *(vide)* | À définir seulement si Google renvoie `redirect_uri_mismatch` |
| `DATA_DIR` | `./data` | Dossier des données (base, CV, jeton, prompts) |
| `SECRETS_DIR` | `./secrets` | Dossier des secrets (`credentials.json`) |

> `settings` est lu **au démarrage** : après une modification de `.env`, redémarre
> l'application. Seuls les fichiers de `data/prompts/` sont relus à chaque génération.

---

## Personnaliser les prompts (ton profil)

L'IA rédige mieux si elle connaît ton parcours. Trois fichiers, fournis comme **modèles** :

```bash
cp data/prompts/profil.md.example        data/prompts/profil.md
cp data/prompts/schema_cours.md.example  data/prompts/schema_cours.md
cp data/prompts/exemples_valides.md.example data/prompts/exemples_valides.md
```

- **`profil.md`** — ta formation, tes compétences, tes projets. C'est ce que l'IA utilisera ;
  elle n'inventera rien d'autre.
- **`schema_cours.md`** — la structure attendue de tes e-mails.
- **`exemples_valides.md`** — 1 à 3 mails que tu as validés (style, ton).

Ces fichiers sont relus à chaque génération : pas besoin de redémarrer.

> 🔒 **Important** : seuls les `*.md.example` sont versionnés. Tes `*.md` remplis (nom,
> téléphone, expériences) restent **locaux** et ne partiront jamais sur GitHub.

Si un fichier est absent, l'application utilise un profil générique de repli.

---

## Premier lancement

1. Ouvre <http://localhost:8000>.
2. Clique sur **Connecter Gmail**, choisis ton compte.
3. Google peut afficher « Google n'a pas validé cette application » → **Paramètres avancés** →
   **Accéder à Stage Tracker**, puis laisse les cases cochées et **Continuer**.
4. Le premier import démarre (quelques minutes selon la taille de la boîte).
5. Importe ton CV en PDF via le bandeau du bas (il sera joint aux envois).
6. (Recommandé) remplis `data/prompts/profil.md`.

---

## Utilisation

- **Colonne de gauche** : tes candidatures.
  - 🟡 **En attente** — candidature envoyée, pas de réponse
  - 🔴 **Relance** — relance à faire
  - 🟢 **Répondu** — le recruteur a répondu
  - ⚪ **Refusé**
  - ➕ **À contacter** — entreprise connue, pas encore contactée
- **Colonne centrale** : l'échange complet avec l'entreprise sélectionnée.
- **Bandeau du bas** : éditeur de message, pièces jointes, envoi.

| Action | Où | Effet |
|--------|----|-------|
| Renommer une entreprise | clic sur son nom | corrige un nom mal deviné par l'IA |
| Définir le secteur | modale « Modifier l'entreprise » | range l'entreprise dans un sous-domaine IT |
| Filtrer la barre de gauche | liste « Secteur » en haut | n'affiche que ce secteur |
| Déplacer un mail | bouton ⇄ sur une bulle | réaffecte **ce mail** à une autre entreprise |
| Déplacer la conversation | bouton ⇄ dans l'en-tête | réaffecte **tous** les mails du fil |
| Rédiger / Régénérer | boutons de l'éditeur | brouillon Gemini (éditable) |
| Corriger l'orthographe | bouton de l'éditeur | corrige **uniquement** les fautes, sans reformuler |
| Réviser un paragraphe | bouton « Réviser un paragraphe » | l'IA change **un seul** paragraphe (ex. prendre un autre exemple) |
| Répondre / Relancer | boutons de l'éditeur | brouillon adapté au contexte |
| Ignorer / Restaurer | en-tête ou corbeille | met une entreprise à la corbeille (réversible) |
| Envoyer | bouton de l'éditeur | **seul moyen d'envoyer un e-mail** |

Lorsqu'une entreprise se vide (tous ses mails déplacés), elle part automatiquement à la
corbeille — réversible.

---

## Structure du projet

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
├── tests/                 # tests hors-ligne (unittest)
├── .env.example           # modèle de configuration
├── docker-compose.yml     # exécution en conteneur
├── Dockerfile
├── pyproject.toml         # configuration ruff (conventions de code)
├── requirements.txt
└── requirements-dev.txt   # outils de développement (ruff)
```

---

## Tests et qualité de code

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

Conventions : code et identifiants en anglais, commentaires et textes utilisateur en
français, lignes ≤ 120 caractères, `snake_case` / `PascalCase` / `UPPER_CASE`.

---

## Sauvegarder, réinstaller, changer de machine

Tout ce qui est **personnel** vit hors de Git. Avant de supprimer le dossier du projet,
sauvegarde ces trois éléments :

```bash
# depuis la racine du projet
tar czf stage-tracker-sauvegarde.tar.gz .env secrets data
```

Pour réinstaller (nouvelle machine, ou après suppression du dossier) :

```bash
git clone https://github.com/komafeu007/stageTracker.git
cd stageTracker

# restaure ta configuration et tes données
tar xzf /chemin/vers/stage-tracker-sauvegarde.tar.gz

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

| Élément | Versionné ? | À sauvegarder ? |
|---------|-------------|-----------------|
| Code, `.env.example`, modèles de prompts, tests | ✅ oui | non (sur GitHub) |
| `.env` | ❌ non | **oui** (clé Gemini, mot de passe, mots-clés) |
| `secrets/credentials.json` | ❌ non | **oui** (ou re-télécharge depuis Google Cloud) |
| `data/stage_tracker.db` | ❌ non | **oui** (tes candidatures) |
| `data/token.json` | ❌ non | oui (sinon : reconnecte Gmail) |
| `data/cv_active.pdf` | ❌ non | oui (sinon : réimporte ton CV) |
| `data/prompts/*.md` | ❌ non | oui (ton profil rempli) |

Repartir de zéro (sans sauvegarde) est possible : il suffit de refaire les étapes
d'installation, de se reconnecter à Gmail et de réimporter le CV — les mails seront
re-analysés depuis Gmail.

---

## Dépannage

| Symptôme | Cause probable / solution |
|----------|---------------------------|
| `credentials.json introuvable` | Le fichier doit être dans `secrets/` |
| Gmail « non connecté » alors que tu étais connecté | Le jeton a été révoqué ou a expiré : reclique **Connecter Gmail** |
| `redirect_uri_mismatch` | Définis `OAUTH_REDIRECT_URI` (ex. `http://localhost:8000/auth/callback`) |
| `Ollama injoignable` | `ollama serve` n'est pas lancé, ou `OLLAMA_HOST` est faux |
| `Model not found` | `ollama pull llama3.2:1b` |
| Classification toujours identique | Modèle trop petit : essaie `OLLAMA_MODEL=mistral:7b` |
| Secteur vide après une synchro | Ollama est indisponible : définis le secteur à la main dans la modale |
| `429` sur Gemini | Quota atteint : attends, ou laisse les modèles de repli faire le travail |
| « Réponse IA illisible » ou texte coupé | Le modèle a tronqué sa réponse : réessaie, ou change `GEMINI_MODEL` |
| « La base de données a été réparée. Recharge la page. » | Le fichier SQLite avait perdu son schéma : les tables ont été recréées, relance une synchro |
| Erreur 403 en testant l'API | Ajoute l'en-tête `X-Requested-With: stagetracker` |
| Page blanche | Le navigateur a besoin d'Internet (Vue et Tailwind viennent de CDN) |
| Rien ne se met à jour | Redémarre : `.env` n'est lu qu'au démarrage |

```bash
# Docker
docker compose logs -f

# Local : les logs s'affichent dans le terminal uvicorn
```

Réinitialiser la base (tout sera ré-analysé depuis Gmail) :

```bash
rm data/stage_tracker.db
# relance l'application : le schéma est recréé automatiquement
```

---

## Sécurité et confidentialité

- ❌ **Aucun envoi automatique** : chaque e-mail part sur un clic.
- 🔒 **Secrets ignorés par Git** : `.env`, `secrets/`, `data/` sont dans `.gitignore`.
- 🔒 **Profil local** : seuls les modèles `*.md.example` sont versionnés ; ton `profil.md`
  rempli ne quitte pas ta machine.
- 🛡️ **Anti-CSRF** : toute requête non-`GET` doit porter l'en-tête
  `X-Requested-With: stagetracker`.
- 🔑 **Mot de passe optionnel** : `APP_PASSWORD` protège l'interface si tu l'exposes.
- 🩺 **Seule exception au mot de passe** : `/api/health`, la sonde du conteneur — elle ne
  renvoie que `{"ok": true}`.
- 🧼 **HTML nettoyé avant envoi** : le corps rédigé est filtré (`sanitize_html`) ; seules les
  balises de mise en forme simples passent.
- 🌐 **Site de l'entreprise lu avec prudence** : pour personnaliser un mail, seules les adresses
  publiques `http(s)` sont récupérées ; `localhost` et les IP privées sont ignorées.

> ⚠️ Si une clé a pu être exposée (commit, capture, dépôt public), **révoque-la** :
> - Google Cloud → *Clients* → supprime/régénère le client OAuth ;
> - Google AI Studio → supprime la clé Gemini.
>
> Ne commite jamais `credentials.json`, `data/token.json`, `.env` ni ton `profil.md` rempli.

---

## Licence

MIT — voir [LICENSE](LICENSE).

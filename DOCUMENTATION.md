# FFBB Agenda — Documentation technique

## Vue d'ensemble

Ce projet scrape automatiquement [competitions.ffbb.com](https://competitions.ffbb.com) et génère des **calendriers `.ics` statiques** (un par championnat/poule et un par équipe), hébergés sur GitHub Pages. Une page publique (`docs/index.html`) permet à n'importe qui de chercher son équipe et de s'abonner depuis son mobile. Un Cloudflare Worker gère l'abonnement, l'email de remerciement, et — sur Android uniquement — la création à la demande d'un vrai calendrier Google.

Le pipeline est 100 % automatisé via GitHub Actions : scraping, régénération des `.ics`, commit et push tournent sans intervention, plusieurs fois par jour.

---

## Architecture

```
competitions.ffbb.com (HTML SSR, JSON __next_f embarqué)
        │
        ▼
  scraper_http.py        ← aiohttp + BeautifulSoup, sans navigateur
  (scrape poules/journées/matchs, découverte des compétitions)
        │
        ▼
  generate_ics.py        ← construit les .ics + le manifest
        │  ├─ NocoDB (table "Abreviations Equipes") → noms courts
        │
        ▼
  docs/calendars/*.ics          ← 1 fichier par championnat/poule
  docs/calendars/teams/*.ics    ← 1 fichier par équipe
  docs/calendars.json           ← manifest lu par le frontend
        │
        ▼
  GitHub Pages (dossier docs/, servi sur basket.varai.fr)
        │
        ▼
  docs/index.html         ← page publique (recherche équipe + abonnement)
        │
        ▼
  Cloudflare Worker (cloudflare-worker/worker.js)
  ├─ POST /subscribe       → enregistre dans NocoDB, email de remerciement (Brevo)
  ├─ GET  /gcal             → crée/retourne un vrai calendrier Google (Android)
  ├─ GET  /ics/{fichier}    → proxy des .ics (force charset=utf-8)
  ├─ POST /feedback         → email de signalement
  └─ POST /contact-parents  → enregistre les coordonnées parents (NocoDB)
        │
        ▼
  sync_google_calendars.py ← répercute les mises à jour .ics dans les
                              calendriers Google réellement créés (NocoDB)
```

---

## Fichiers

| Fichier | Rôle |
|---|---|
| `scraper_http.py` | Scraping HTTP (aiohttp + BeautifulSoup) : matchs, arbitres, découverte des poules/compétitions/régions |
| `generate_ics.py` | Génère les `.ics` (championnats + équipes) et `docs/calendars.json` |
| `sync_google_calendars.py` | Répercute les mises à jour dans les vrais calendriers Google créés à la demande |
| `setup_nocodb_abbreviations.py` | Crée la table NocoDB "Abreviations Equipes" |
| `setup_nocodb_gcal.py` | Crée la table NocoDB "Calendriers Google" et met à jour son ID dans `worker.js` |
| `setup_nocodb_joueurs.py` | Ajoute/remplit les colonnes licence/naissance sur "Contacts Joueurs" (dry-run par défaut, `--apply` pour écrire) |
| `cleanup_amicales.py` | Script ponctuel : supprime les compétitions "Amicale" déjà scrapées avant l'ajout du filtre d'exclusion |
| `cloudflare-worker/worker.js` | Worker Cloudflare : abonnement, email, calendrier Google à la demande, proxy `.ics`, feedback, contact parents |
| `docs/index.html` | Page publique GitHub Pages : recherche équipe + abonnement (édité directement, pas généré) |
| `docs/calendars.json` | Manifest : liste des championnats et équipes → chemin du `.ics` |
| `docs/calendars/` | Fichiers `.ics` statiques (championnats à la racine, équipes dans `teams/`) |
| `.env` | Toutes les clés API du projet, centralisées (NocoDB, Brevo, Cloudflare, Google service account) |

### Fichiers legacy (non utilisés par le pipeline actuel)

`scraper.py` (Playwright), `calendar_sync.py` (sync directe vers Google Calendar API), `generate_frontend.py` (générait un ancien `index.html` à la racine depuis `calendars.json` à la racine), `calendars.json` / `calendars_sauv.json` (racine, ancien cache). Conservés dans le repo mais plus référencés par aucun workflow — l'architecture est passée d'une sync Google Calendar directe à des `.ics` statiques + création Google à la demande via le Worker.

---

## Prérequis

### Python
```bash
pip install -r requirements.txt
```
(`aiohttp`, `beautifulsoup4`, `requests`, `google-auth`, `google-api-python-client`, `python-dotenv`)

### Variables d'environnement (`.env` à la racine)

| Variable | Usage |
|---|---|
| `NOCODB_TOKEN` | Clé API NocoDB (abréviations, abonnements, calendriers Google, contacts) |
| `NOCODB_TABLE_GCAL` | ID de la table NocoDB "Calendriers Google" |
| `BREVO_KEY` | Envoi d'emails (remerciement, feedback) |
| `WORKER_URL` | URL publique du Worker (fallback : déduite de la requête si absent) |
| `CLOUDFLARE_API_TOKEN` | Déploiement du Worker via `wrangler` |
| `GOOGLE_SA_JSON` | Credentials du compte de service Google (JSON complet sur une ligne), utilisé par le Worker et `sync_google_calendars.py` pour créer/mettre à jour les calendriers Google |

Les mêmes clés (sauf `WORKER_URL`) sont dupliquées en secrets GitHub Actions pour les workflows CI, et en secrets Cloudflare pour le Worker (`wrangler secret put ...`).

---

## Fonctionnement du scraping (`scraper_http.py`)

Aucun navigateur : le HTML SSR de competitions.ffbb.com embarque les données dans des scripts `self.__next_f.push([1,"..."])` (payload Next.js échappé). Le scraper décode ces scripts et extrait le tableau `rencontres` en cherchant l'`id` de la poule ciblée (le HTML contient en fait **toutes** les poules de la page).

### Chaîne de scraping

```
URL compétition (phase= et poule=)
    │
    ├─ find_all_poule_urls()      → <select aria-label="Poules"> puis fallback liens / JSON __next_f
    │
    └─ Pour chaque poule (scrape_poule) :
         │
         ├─ extract_journee_numbers() → journées disponibles
         │
         └─ Pour chaque journée (en parallèle, Semaphore(8)) :
              │
              ├─ extract_next_f_json() → tableau rencontres brut FFBB
              ├─ parse_rencontre()     → format match normalisé
              └─ fetch_arbitres()      → page /match/{id}, uniquement pour les
                                          matchs joués ou dans les 14 prochains jours
```

### Modes de découverte

- **`--direct URL...`** : scrape directement les poules données (pas d'auto-détection).
- **mode par défaut (URL de compétition)** : auto-détecte toutes les poules via `find_all_poule_urls()`.
- **`--region URL_LIGUE`** : `discover_competitions()` parcourt une page de ligue/comité et résout l'URL `?phase=` de chaque compétition trouvée (exclut coupes, plateaux, amicales via regex sur le slug `/(?:\d+-)?ami-`).
- **`--national`** : liste fixe de championnats nationaux (`NF1-3`, `NM1-3`, `NFU18/U15 Elite`, `NMU18/U15 Elite`) dont l'URL `?phase=` est résolue dynamiquement.

### Résilience

- SSL non vérifié (`SSL_CTX.verify_mode = ssl.CERT_NONE`) : le certificat de competitions.ffbb.com est parfois expiré.
- Retry avec backoff (1s, 2s) sur les erreurs réseau par journée.
- Déduplication par `match_url` (au sein d'une poule) et par empreinte de l'ensemble des `match_url` (entre poules, dans `generate_ics.py`) pour éviter de retraiter deux fois les mêmes données si le HTML renvoie un faux poule_id.

---

## Génération des calendriers (`generate_ics.py`)

Pour chaque poule scrapée :
1. Un `.ics` **championnat** avec tous les matchs → `docs/calendars/{slug}.ics`
2. Un `.ics` **par équipe** (matchs filtrés, avec 🏠/✈️ domicile-extérieur dans le titre) → `docs/calendars/teams/{slug}.ics`
3. Mise à jour de `docs/calendars.json` (entrées `calendriers` et `equipes`, dédupliquées par slug)

### Abréviations d'équipe

`load_team_abbreviations()` charge la table NocoDB "Abreviations Equipes" (nom long → nom court) au démarrage. Si le token ou la table est indisponible, les noms restent affichés tels quels (le script ne plante jamais pour cette raison). Le matching gère aussi les suffixes `- 1`, `- 2` (clubs à plusieurs équipes).

### Format d'un événement

```
UID      : slug({uid_prefix}_{date}_{heure}_{eq1}_{eq2})@ffbb-agenda
Titre    : 🏠 US ISSOIRE - 1 – FIRMINY CHAZEAU-FAYOL AL (72-65)
           (🏠/✈️ uniquement dans le calendrier équipe ; score si disponible)
Location : GYMNASE FERNAND COUNIL, Chemin des Croizettes, 63500 Issoire

Description :
  🏀 PNF – ARA – Poule A
  ⚔️ US ISSOIRE - 1 vs FIRMINY CHAZEAU-FAYOL AL
  📊 Score : 72-65

  GYMNASE FERNAND COUNIL
  📍 Chemin des Croizettes, 63500 Issoire
  🚗 Waze : https://waze.com/ul?ll=...

  📢 Arbitres : NOM Prénom, NOM Prénom  (ou "Pas de désignation")

  🔗 Feuille FFBB : https://competitions.ffbb.com/...
```

Chaque `.ics` inclut une définition `VTIMEZONE` Europe/Paris (CET/CEST) minimale, pas de dépendance à une base tz externe.

---

## Abonnement et Cloudflare Worker (`cloudflare-worker/worker.js`)

Le Worker (`ffbb-agenda`, déployé sur `*.workers.dev`, appelé depuis `docs/index.html`) expose :

| Route | Rôle |
|---|---|
| `POST /subscribe` | Enregistre `{email, equipe, comp_nom, fichier, device}` dans NocoDB, lance l'email de remerciement (Brevo) en tâche de fond, retourne directement les liens d'abonnement (`httpsUrl`, `webcalUrl`, `googleUrl`) — pas d'étape de confirmation |
| `GET /gcal?fichier=&equipe=&comp_nom=` | Crée (ou retourne l'id existant d') un **vrai** calendrier Google pour l'équipe, appelé en tâche de fond par le front juste après `/subscribe` |
| `GET /ics/{fichier}` | Proxy vers le `.ics` GitHub Pages, force `Content-Type: text/calendar; charset=utf-8` (sinon Google Agenda Android mésinterprète les emoji/accents du nom d'agenda) |
| `POST /feedback` | Envoie un email de signalement (Brevo) |
| `POST /contact-parents` | Enregistre les coordonnées d'un parent (NocoDB, table "Contacts Joueurs") |

### Pourquoi un vrai calendrier Google (Android uniquement)

Sur Android, un abonnement à une URL `.ics` externe est ajouté au compte mais reste invisible tant que l'utilisateur ne l'active pas manuellement. Un lien `cid=<id calendrier Google>` s'affiche lui immédiatement. Créer les ~1600 calendriers d'équipe à l'avance dépasserait les limites opérationnelles de Google : le Worker en crée donc un uniquement **au premier abonnement** de chaque équipe, en :
1. signant un JWT RS256 avec le compte de service (`GOOGLE_SA_JSON`) pour obtenir un access token OAuth2,
2. créant le calendrier + ACL public (`reader`/`default`),
3. import des événements du `.ics` statique correspondant via `events/import` (conserve l'UID d'origine → resynchronisable sans doublon).

### Anti-doublon sous concurrence

Deux appels concurrents pour la même équipe (`/subscribe` en tâche de fond + polling `/gcal` du front) pouvaient créer deux calendriers Google en double, faute de contrainte unique disponible côté NocoDB sur ce plan. Chaque candidat pose un jalon (ligne NocoDB avec `google_calendar_id` vide), relit toutes les lignes de l'équipe : la plus ancienne (`Id` le plus petit) gagne et crée le calendrier ; les autres suppriment leur jalon et attendent (poll 500ms, jusqu'à 30s) via `waitForGoogleCalendarId()`.

### Déploiement

```bash
cd cloudflare-worker
wrangler deploy
```
Secrets à configurer côté Cloudflare (`wrangler secret put <NOM>`) : `NOCODB_TOKEN`, `BREVO_KEY`, `WORKER_URL`, `GOOGLE_SA_JSON`.

---

## Synchronisation Google Calendar (`sync_google_calendars.py`)

Ne touche que les calendriers **réellement créés** (listés dans la table NocoDB "Calendriers Google", donc quelques unités — jamais les ~1600 équipes). Après chaque scraping, répercute les changements (horaires, scores, arbitres) du `.ics` statique correspondant vers le vrai calendrier Google, via l'API Google Calendar avec les mêmes credentials de compte de service que le Worker.

---

## Automatisation (GitHub Actions)

Tous les workflows tournent sur `ubuntu-latest`, poussent directement sur `main`, et gèrent les conflits de push concurrents sur `docs/calendars.json` en régénérant par-dessus l'état distant plutôt qu'en rejouant un rebase (`git rebase --abort` + `git reset --hard origin/main` + relance du script).

| Workflow | Déclenchement | Commande |
|---|---|---|
| `scrape.yml` (région GES) | 8h00 et 23h00 Paris (`6h`/`21h` UTC) + manuel | `generate_ics.py --region "https://competitions.ffbb.com/ligues/ges"` |
| `scrape-departements.yml` | 22h00 Paris (`20h` UTC) + manuel | `generate_ics.py --region` sur les 10 comités départementaux Grand Est |
| `scrape-national.yml` | 23h15 Paris (`21h15` UTC, décalé de 15 min pour éviter les pushs simultanés) + manuel | `generate_ics.py --national` |
| `sync-google.yml` | À la fin (succès) de `scrape.yml` ou `scrape-national.yml`, ou manuel | `sync_google_calendars.py` |

Secrets GitHub requis : `NOCODB_TOKEN`, `NOCODB_TABLE_GCAL`, `GOOGLE_SA_JSON`.

⚠️ Les horaires en commentaire sont calés sur l'heure d'été (UTC+2) — à décaler d'1h en hiver si besoin de précision, sinon dérive d'une heure entre novembre et mars.

---

## Abonnement (utilisateur final)

Depuis `docs/index.html` (basket.varai.fr) : recherche de l'équipe → le front appelle `/subscribe`, reçoit `httpsUrl`/`webcalUrl`/`googleUrl`, puis :

- **iPhone / iPad / Desktop (iCal, Outlook)** : lien `webcal://` (proxifié via `/ics/{fichier}`) ou import direct du `.ics`.
- **Android / Google Agenda** : le front appelle aussi `/gcal` en tâche de fond ; dès que le vrai calendrier Google est prêt, le bouton "Ajouter à Google Agenda" pointe vers `calendar.google.com/calendar/u/0?cid=...` (visible immédiatement, contrairement à un simple lien `.ics`).

### Mise à jour automatique des abonnés

- Abonnés `.ics` classiques : mise à jour selon la fréquence de polling du client (iOS ~1h, Google Agenda plusieurs fois/jour).
- Abonnés avec vrai calendrier Google : mis à jour par `sync_google_calendars.py` après chaque scraping (quasi temps réel).

---

## ⚠️ Attention : `docs/calendars.json`

Seule correspondance entre une compétition/équipe et son fichier `.ics`. Il est réécrit intégralement à chaque run de `generate_ics.py` (lu puis regénéré en dédupliquant par slug) — ne pas l'éditer à la main pendant qu'un workflow tourne (risque de conflit de push, déjà géré par le fallback "reset + régénère" des workflows).

Contrairement à l'ancienne architecture Google Calendar directe, une suppression de ce fichier n'a **pas** d'impact destructeur sur les abonnements existants : les fichiers `.ics` gardent les mêmes UID/chemins, un `generate_ics.py` régénère le manifest à l'identique. Le seul risque est côté table NocoDB "Calendriers Google" : ne pas la vider sans raison, elle est la seule trace des calendriers Google réellement créés pour les abonnés Android.

---

## Limitations connues

- **Scraping séquentiel entre workflows, parallèle en interne** : chaque poule scrape ses journées en parallèle (`Semaphore(8)`), mais les compétitions/poules sont traitées l'une après l'autre.
- **Arbitres** : récupérés uniquement pour les matchs joués ou dans les 14 prochains jours (évite de scraper inutilement ~1600 pages détail à chaque run). Avant désignation, l'event affiche "Pas de désignation".
- **Scores** : disponibles uniquement après la rencontre.
- **Certificat SSL** : vérification désactivée pour competitions.ffbb.com (certificat parfois expiré côté FFBB).
- **Calendriers Google à la demande** : limité aux abonnés Android qui déclenchent `/gcal` ; pas de garantie de synchro temps réel, dépend du déclenchement de `sync-google.yml` après un scraping réussi.

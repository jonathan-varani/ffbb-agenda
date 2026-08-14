# FFBB Agenda — Documentation technique

## Vue d'ensemble

Ce projet scrape automatiquement le site [competitions.ffbb.com](https://competitions.ffbb.com) et synchronise les matchs dans des **Google Calendars publics**, un par championnat/poule et un par équipe. Une page HTML permet à n'importe quel utilisateur de s'abonner au calendrier de son équipe depuis son mobile.

---

## Architecture

```
competitions.ffbb.com
        │
        ▼
  scraper.py          ← Playwright + BeautifulSoup
  (scrape les matchs)
        │
        ▼
  calendar_sync.py    ← Google Calendar API
  (crée/met à jour les agendas)
        │
        ├── calendars.json   ← base locale (clé → calendar_id)
        │
        ▼
  generate_frontend.py
  (génère index.html)
        │
        ▼
    index.html         ← page publique d'abonnement
```

---

## Fichiers

| Fichier | Rôle |
|---|---|
| `scraper.py` | Scraping Playwright, extraction matchs + détails |
| `calendar_sync.py` | Sync Google Calendar (création, MàJ, suppression) |
| `generate_frontend.py` | Génère `index.html` depuis `calendars.json` |
| `index.html` | Page publique : recherche équipe + abonnement |
| `calendars.json` | Cache local : `clé → calendar_id` Google |
| `service_account.json` | Credentials compte de service Google (**ne jamais partager**) |

---

## Prérequis

### 1. Python et dépendances
```bash
pip install playwright beautifulsoup4 google-api-python-client google-auth --break-system-packages
playwright install chromium
```

### 2. Compte de service Google
- Projet GCP : `ffbb-agenda`
- Compte de service : `ffbb-scraper@ffbb-agenda.iam.gserviceaccount.com`
- Fichier credentials : `service_account.json` (à placer dans le dossier du projet)
- API activée : **Google Calendar API**

---

## Fonctionnement du scraping (`scraper.py`)

### Chaîne de scraping pour un championnat

```
URL championnat (avec phase= et poule=)
    │
    ├─ find_all_poule_urls()
    │   └─ Lit le <select aria-label="Poules"> → toutes les poules
    │
    └─ Pour chaque poule :
         │
         ├─ find_journee_urls()
         │   └─ Lit le <select aria-label="Journées"> → toutes les journées
         │
         └─ Pour chaque journée :
              │
              ├─ extract_matches()      ← divs avec 2 <a title="EQUIPE">
              │
              └─ scrape_match_detail()  ← page /match/{id}
                   ├─ Salle (label "Nom" dans section "Salle")
                   ├─ Adresse (label "Adresse")
                   ├─ Lien Waze
                   ├─ Lien Google Maps
                   └─ Arbitres (siblings après label "Arbitre")
```

### Détection des matchs dans le DOM

Un match = un `<div>` ayant **exactement 2 enfants directs `<a title="NOM_EQUIPE">`**.  
Déduplication par `(date, heure, equipe1, equipe2)` pour éviter les doublons mobile/desktop.

### Extraction de la poule active

La poule courante est identifiée par l'ID dans l'URL (`poule=XXXXXXXX`).  
On cherche l'`<option value="XXXXXXXX">` correspondante dans le `<select aria-label="Poules">` pour en lire le libellé (ex: "Poule B").

---

## Fonctionnement de la synchronisation (`calendar_sync.py`)

### Structure des calendriers créés

| Type | Nom | Exemple |
|---|---|---|
| Championnat | `🏀 FFBB – {SLUG} – {REGION} – {POULE}` | `🏀 FFBB – PNF – ARA – Poule A` |
| Équipe | `🏀 FFBB – {EQUIPE}  [{SLUG} – {REGION}]` | `🏀 FFBB – US ISSOIRE - 1  [PNF – ARA]` |

Tous les calendriers sont **publics** (ACL `reader` pour `default`).

### Clés dans `calendars.json`

- Championnat : `{phase_id}_{poule_id}` → ex: `200000002897651_200000003055506`
- Équipe : `eq_{phase_id}_{poule_id}_{NOM_EQUIPE[:40]}` → ex: `eq_200000002897651_200000003055506_US_ISSOIRE`

### Déduplication des événements

Chaque event Google Calendar a une propriété privée `ffbb_match_id` :
```
{date}_{heure}_{equipe1}_{equipe2}
```
Si un match existe déjà, il est mis à jour uniquement si le titre, l'horaire, le lieu ou la description ont changé.

### Format d'un événement

```
Titre    : 🏠 US ISSOIRE - 1 – FIRMINY CHAZEAU-FAYOL AL
           (🏠 domicile / ✈️ extérieur — uniquement dans le calendrier équipe)

Location : GYMNASE FERNAND COUNIL, Chemin des Croizettes, 63500 Issoire

Description :
  🏀 PNF – ARA – Poule A
  ⚔️  US ISSOIRE - 1 vs FIRMINY CHAZEAU-FAYOL AL
  📊 Score : 72-65              (si disponible)

  GYMNASE FERNAND COUNIL
  📍 Chemin des Croizettes, 63500 Issoire
  🚗 Waze : https://waze.com/ul?q=...

  🦺 Arbitres : NOM PRENOM, NOM PRENOM  (ou "Pas de désignation")

  🔗 Feuille FFBB : https://competitions.ffbb.com/ligues/ara/...
```

### Gestion des quotas Google Calendar

- **Retry avec backoff exponentiel** : toute erreur 403/429 déclenche une attente (5s → 10s → 20s → 40s → 80s)
- **Pause de 1,5s** après chaque création de calendrier pour éviter le rate-limiting en rafale

---

## Commandes

### Sync complet d'un championnat (toutes les poules auto-détectées)
```bash
python calendar_sync.py "https://competitions.ffbb.com/ligues/ara/competitions/pnf?phase=200000002897651&poule=200000003055506"
```

### Sync d'une seule poule (mode direct, sans auto-détection)
```bash
python calendar_sync.py --direct "https://competitions.ffbb.com/ligues/ara/competitions/pnf?phase=200000002897651&poule=200000003055507"
```

### Régénérer la page HTML publique
```bash
python generate_frontend.py
```

---

## Abonnement à un calendrier

### iPhone / iPad
1. Ouvrir la page `index.html`
2. Chercher son équipe → cliquer **S'abonner**
3. Choisir **"Ajouter à Calendrier iPhone"** → s'ouvre automatiquement dans l'app Calendrier
4. Confirmer l'abonnement

Le lien utilise le protocole `webcal://` reconnu nativement par iOS.

### Android
1. Ouvrir la page `index.html`
2. Chercher son équipe → cliquer **S'abonner**
3. Choisir **"Ajouter à Google Agenda"** → s'ouvre dans Google Calendar
4. Cliquer **"Ajouter"**

### Desktop / Mac
- **iCal / Calendrier macOS** : clic sur le lien `webcal://` ou importer le fichier `.ics`
- **Google Agenda** : utiliser le lien "Ouvrir dans Google Agenda"
- **Outlook** : importer le fichier `.ics` ou s'abonner via l'URL `https://calendar.google.com/calendar/ical/{id}/public/basic.ics`

### Mise à jour automatique des abonnements
Les abonnés **reçoivent automatiquement les mises à jour** : les changements d'horaire, l'ajout des scores et des arbitres apparaissent sans aucune action de leur part. La fréquence de synchronisation dépend de l'app (iOS : toutes les heures environ, Google Agenda : plusieurs fois par jour).

---

## Cron (synchronisation automatique)

À configurer sur la machine hébergeant le projet (Windows : Planificateur de tâches, Linux/Mac : crontab).

### Exemple crontab Linux
```cron
# Sync rapide toutes les 3h (poule spécifique)
0 */3 * * * cd /chemin/projet && python calendar_sync.py --direct "URL_POULE" >> logs/sync.log 2>&1

# Sync complet hebdomadaire (dimanche 3h du matin)
0 3 * * 0 cd /chemin/projet && python calendar_sync.py "URL_CHAMPIONNAT" >> logs/sync_full.log 2>&1

# Régénération du frontend après chaque sync
5 */3 * * * cd /chemin/projet && python generate_frontend.py >> logs/frontend.log 2>&1
```

---

## ⚠️ Attention : `calendars.json`

Ce fichier est la **seule correspondance** entre les clés FFBB et les IDs Google Calendar. Il ne doit jamais être supprimé en production.

### Ce qui se passe si on le supprime

Le script ne sait plus que les calendriers existent déjà → il en **recrée de nouveaux** avec de nouveaux IDs. Conséquence directe : **tous les abonnés perdent leur abonnement** car leur app (iPhone, Google Agenda) pointe vers l'ancien ID qui n'est plus alimenté.

Les anciens calendriers Google restent orphelins dans le compte de service — il faut les supprimer manuellement depuis la [Google Calendar API Console](https://console.cloud.google.com) ou via un script.

### Quand supprimer `calendars.json` (reset volontaire)

Uniquement si on veut **repartir de zéro**, par exemple pour corriger des noms de calendriers incorrects. Dans ce cas :

1. Supprimer `calendars.json`
2. Supprimer manuellement les anciens calendriers dans Google Calendar (ou via l'API)
3. Relancer `python calendar_sync.py` → recrée tout proprement
4. **Prévenir les utilisateurs** qu'ils doivent se réabonner

### Sauvegarde recommandée

```bash
# Avant toute opération risquée
cp calendars.json calendars.json.backup
```

---

## Limitations connues

- **Scraping séquentiel** : chaque page Playwright est ouverte l'une après l'autre (~4s/page). Pour 6 poules × 10 journées + détails matchs : environ 30-45 min par championnat complet.
- **Quota Google Calendar** : création en masse limitée. Le backoff automatique gère les erreurs, mais un sync initial de plusieurs championnats peut prendre plusieurs heures.
- **Arbitres** : les désignations arrivent tard dans la saison. Avant désignation, l'event affiche "Pas de désignation".
- **Scores** : récupérés sur la page détail du match. Disponibles uniquement après la rencontre.

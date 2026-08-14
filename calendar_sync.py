"""
FFBB Calendar Sync
──────────────────
Pour chaque compétition scrapée :
  - Crée un Google Calendar public par championnat (PNF – ARA – Poule A)
  - Crée un Google Calendar public par équipe (US ISSOIRE - 1)
  - Synchronise les matchs : création / mise à jour / suppression

Usage :
    python calendar_sync.py
    python calendar_sync.py <URL_COMPETITION>
"""
import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import quote

from google.oauth2 import service_account
from googleapiclient.discovery import build

from scraper import scrape_competition, build_calendar_name, find_all_poule_urls

# ── Config ────────────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = "service_account.json"
SCOPES               = ["https://www.googleapis.com/auth/calendar"]
CALENDARS_DB         = "calendars.json"
TIMEZONE             = "Europe/Paris"

TEST_URL = (
    "https://competitions.ffbb.com/ligues/ara/competitions/pnf"
    "?phase=200000002897651&poule=200000003055506&journee=1"
)


# ── Auth ──────────────────────────────────────────────────────────────────────
def get_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)


# ── DB locale des calendriers ─────────────────────────────────────────────────
def load_db() -> dict:
    if os.path.exists(CALENDARS_DB):
        with open(CALENDARS_DB, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_db(db: dict):
    with open(CALENDARS_DB, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


# État global : pause allongée si on a récemment touché un quota
_quota_hit = False


# ── Retry avec backoff exponentiel ───────────────────────────────────────────
def api_call_with_retry(fn, max_retries: int = 6):
    """Exécute fn() avec retry + backoff exponentiel sur les erreurs 429/403 quota.
    Google Calendar quota se reset sur ~15-30 min → on attend jusqu'à 20 min max.
    """
    global _quota_hit
    from googleapiclient.errors import HttpError
    for attempt in range(max_retries):
        try:
            return fn()
        except HttpError as e:
            if e.resp.status in (429, 403) and attempt < max_retries - 1:
                _quota_hit = True
                wait = min(60 * (2 ** attempt), 1200)  # 1min → 2min → 4min → ... → 20min max
                print(f"  ⏳ Quota dépassé, attente {wait}s ({wait//60}min) (tentative {attempt+1}/{max_retries})…")
                time.sleep(wait)
            else:
                raise


# ── Création / récupération d'un calendrier ───────────────────────────────────
def get_or_create_calendar(service, db: dict, key: str, nom: str, description: str = "") -> str:
    global _quota_hit
    if key in db:
        return db[key]["calendar_id"]

    print(f"  ✚ Création calendrier : {nom}")
    cal = api_call_with_retry(lambda: service.calendars().insert(body={
        "summary":     f"🏀 FFBB – {nom}",
        "timeZone":    TIMEZONE,
        "description": description or f"Matchs FFBB – {nom}. Source : competitions.ffbb.com",
    }).execute())
    cal_id = cal["id"]

    # Rendre public
    api_call_with_retry(lambda: service.acl().insert(
        calendarId=cal_id,
        body={"role": "reader", "scope": {"type": "default"}},
    ).execute())
    print(f"    → {cal_id} (public)")

    db[key] = {"calendar_id": cal_id, "nom": nom}
    save_db(db)

    # Pause entre créations : longue si quota récemment atteint, courte sinon
    pause = 30 if _quota_hit else 5
    print(f"    ⏸ Pause {pause}s avant prochain calendrier…")
    time.sleep(pause)
    return cal_id


# ── Construction d'un événement Google Calendar ───────────────────────────────
def build_event(match: dict, competition_nom: str, team: str | None = None) -> dict:
    """
    team : si fourni (calendrier équipe), ajoute 🏠/✈️ selon domicile/extérieur.
           Convention FFBB : equipe1 = domicile, equipe2 = visiteur.
    """
    date    = match["date"]
    heure   = match.get("heure") or "00:00"
    eq1     = match["equipe1"]
    eq2     = match["equipe2"]
    score   = match.get("score", "")
    salle   = match.get("salle", "")
    adresse = match.get("adresse", "")
    waze    = match.get("waze", "")
    maps    = match.get("maps_url", "")
    arb     = match.get("arbitres", [])
    murl    = match.get("match_url", "")

    # Emoji domicile / extérieur (uniquement dans le calendrier équipe)
    lieu_emoji = ""
    if team:
        lieu_emoji = "🏠 " if team == eq1 else "✈️ "

    # Titre
    titre = f"{lieu_emoji}{eq1} – {eq2}"
    if score and score not in ("", "0-0"):
        titre += f"  ({score})"

    # Heure début / fin (2h par défaut)
    h, m = map(int, heure.split(":"))
    start_dt = f"{date}T{h:02d}:{m:02d}:00"
    end_dt   = f"{date}T{(h+2)%24:02d}:{m:02d}:00"

    # Location
    location_parts = [p for p in [salle, adresse] if p]
    location = ", ".join(location_parts)

    # Description
    desc_lines = [
        f"🏀 {competition_nom}",
        f"⚔️  {eq1} vs {eq2}",
    ]
    if score and score not in ("", "0-0"):
        desc_lines.append(f"📊 Score : {score}")
    if salle:
        desc_lines.append(f"\n{salle}")
    if adresse:
        desc_lines.append(f"📍 {adresse}")
    if waze:
        desc_lines.append(f"🚗 Waze : {waze}")
    elif maps:
        desc_lines.append(f"🗺️  Maps : {maps}")
    desc_lines.append(f"\n🦺 Arbitres : {', '.join(arb) if arb else 'Pas de désignation'}")
    if murl:
        desc_lines.append(f"\n🔗 Feuille FFBB : {murl}")

    description = "\n".join(desc_lines)

    # ID unique pour déduplication
    match_id = f"{date}_{heure}_{eq1}_{eq2}".replace(" ", "_")

    event = {
        "summary":     titre,
        "description": description,
        "location":    location,
        "start":       {"dateTime": start_dt, "timeZone": TIMEZONE},
        "end":         {"dateTime": end_dt,   "timeZone": TIMEZONE},
        "extendedProperties": {
            "private": {"ffbb_match_id": match_id}
        },
    }
    if murl:
        event["source"] = {"title": "Voir sur FFBB", "url": murl}

    return event


# ── Sync d'une liste de matchs vers un calendrier ────────────────────────────
def sync_to_calendar(service, cal_id: str, matches: list[dict], competition_nom: str, label: str = "", team: str | None = None):
    tag = f"[{label}] " if label else ""

    # Récupère les événements existants
    existing = {}
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=cal_id,
            pageToken=page_token,
            maxResults=500,
            showDeleted=False,
        ).execute()
        for ev in resp.get("items", []):
            mid = (ev.get("extendedProperties") or {}).get("private", {}).get("ffbb_match_id")
            if mid:
                existing[mid] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    seen_ids   = set()
    created = updated = skipped = deleted = 0

    for match in matches:
        event   = build_event(match, competition_nom, team=team)
        mid     = event["extendedProperties"]["private"]["ffbb_match_id"]
        seen_ids.add(mid)

        if mid not in existing:
            api_call_with_retry(lambda e=event: service.events().insert(calendarId=cal_id, body=e).execute())
            print(f"  {tag}✚ {event['summary']}")
            created += 1
        else:
            old = existing[mid]
            changed = (
                old.get("summary")                          != event["summary"] or
                old.get("location", "")                     != event.get("location", "") or
                (old.get("start") or {}).get("dateTime")    != event["start"]["dateTime"] or
                old.get("description", "")                  != event.get("description", "")
            )
            if changed:
                api_call_with_retry(lambda e=event, o=old: service.events().update(
                    calendarId=cal_id, eventId=o["id"], body=e
                ).execute())
                print(f"  {tag}✎ MàJ : {event['summary']}")
                updated += 1
            else:
                skipped += 1

    # Supprimer les événements qui n'existent plus
    for mid, ev in existing.items():
        if mid not in seen_ids:
            api_call_with_retry(lambda ev=ev: service.events().delete(calendarId=cal_id, eventId=ev["id"]).execute())
            print(f"  {tag}✖ Supprimé : {ev.get('summary','?')}")
            deleted += 1

    print(f"  {tag}→ {created} créés / {updated} MàJ / {skipped} inchangés / {deleted} supprimés")


def ical_link(cal_id: str) -> str:
    return f"https://calendar.google.com/calendar/ical/{quote(cal_id)}/public/basic.ics"


# ── Sync d'une seule poule ────────────────────────────────────────────────────
async def sync_poule(url: str, service, db: dict):
    """Scrape et synchronise une poule complète (toutes journées)."""
    matches, meta = await scrape_competition(url, enrich_details=True)
    if not matches:
        print("  Aucun match trouvé.")
        return

    comp_nom = build_calendar_name(meta)   # ex: "PNF – ARA – Poule A"
    comp_key = meta["key"]

    # ── Calendrier par championnat ─────────────────────────────────────────
    print(f"\n── Calendrier championnat : {comp_nom}")
    champ_cal_id = get_or_create_calendar(
        service, db,
        key=comp_key,
        nom=comp_nom,
        description=f"Tous les matchs du championnat {comp_nom}",
    )
    sync_to_calendar(service, champ_cal_id, matches, comp_nom, label=comp_nom)

    # ── Calendriers par équipe ─────────────────────────────────────────────
    equipes = set()
    for m in matches:
        equipes.add(m["equipe1"])
        equipes.add(m["equipe2"])

    print(f"\n── Calendriers équipes ({len(equipes)} équipes)")
    for equipe in sorted(equipes):
        eq_matches = [m for m in matches if equipe in (m["equipe1"], m["equipe2"])]
        eq_key = f"eq_{comp_key}_{equipe.replace(' ','_')[:40]}"
        poule_suffix = f" – {meta['poule']}" if meta.get("poule") else ""
        eq_nom = f"{equipe}  [{meta['slug']} – {meta['region']}{poule_suffix}]"

        eq_cal_id = get_or_create_calendar(
            service, db,
            key=eq_key,
            nom=eq_nom,
            description=f"Matchs de {equipe} – {comp_nom}",
        )
        sync_to_calendar(service, eq_cal_id, eq_matches, comp_nom, label=equipe[:25], team=equipe)

    # ── Récap liens ────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"LIENS iCal — {comp_nom}")
    print(f"{'─'*60}")
    print(f"  Championnat : {ical_link(champ_cal_id)}")
    for equipe in sorted(equipes):
        eq_key = f"eq_{comp_key}_{equipe.replace(' ','_')[:40]}"
        if eq_key in db:
            print(f"  {equipe[:35]:35s} : {ical_link(db[eq_key]['calendar_id'])}")


# ── Pipeline principal ────────────────────────────────────────────────────────
async def sync_competition(url: str):
    """
    Synchronise toutes les poules d'un championnat.
    Détecte automatiquement les poules via Playwright.
    """
    print(f"\n{'='*60}")
    print(f"Détection des poules pour : {url}")
    poule_urls = await find_all_poule_urls(url)
    print(f"→ {len(poule_urls)} poule(s) détectée(s)")
    for u in poule_urls:
        print(f"   {u}")

    service = get_service()
    db = load_db()

    for i, purl in enumerate(poule_urls, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(poule_urls)}] {purl}")
        print(f"{'='*60}")
        await sync_poule(purl, service, db)


async def sync_all(urls: list[str]):
    """Synchronise plusieurs championnats (une URL par championnat)."""
    for url in urls:
        await sync_competition(url)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Usage :
    #   python calendar_sync.py URL                → détecte et sync toutes les poules
    #   python calendar_sync.py --direct URL       → sync uniquement cette poule
    #   python calendar_sync.py --direct URL1 URL2 → sync ces poules uniquement
    args = sys.argv[1:]
    direct = "--direct" in args
    urls = [u for u in args if not u.startswith("--")] or [TEST_URL]

    if direct:
        async def run_direct():
            service = get_service()
            db = load_db()
            for url in urls:
                print(f"\n{'='*60}")
                print(f"[direct] {url}")
                print(f"{'='*60}")
                await sync_poule(url, service, db)
        asyncio.run(run_direct())
    else:
        asyncio.run(sync_all(urls))

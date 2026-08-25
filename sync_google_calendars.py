"""
Synchronise les vrais calendriers Google créés à la demande.
──────────────────────────────────────────────────────────────
Sur Android, un abonnement à une URL .ics externe reste invisible tant que
l'utilisateur ne l'active pas manuellement. On crée donc un VRAI calendrier
Google au premier abonnement d'une équipe (fait par le Cloudflare Worker).

Ce script répercute ensuite les mises à jour (horaires, scores, arbitres) dans
ces calendriers — uniquement ceux réellement créés, listés dans NocoDB. On ne
touche donc qu'une poignée de calendriers, jamais les ~1600 équipes.

Variables d'environnement :
    NOCODB_TOKEN    — clé API NocoDB
    GOOGLE_SA_JSON  — contenu du service_account.json (compte de service)

Usage :
    python sync_google_calendars.py
"""
import json
import os
import re
import sys

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
NOCODB_API   = "https://app.nocodb.com"
NOCODB_BASE  = "poq54dd1rjvxuki"
# ⚠️ Doit correspondre à NOCODB_TABLE_GCAL dans cloudflare-worker/worker.js
NOCODB_TABLE_GCAL = os.environ.get("NOCODB_TABLE_GCAL", "")

ICS_DIR  = "docs/calendars"
TIMEZONE = "Europe/Paris"
SCOPES   = ["https://www.googleapis.com/auth/calendar"]


# ── Parsing ICS ───────────────────────────────────────────────────────────────
def ics_unescape(s: str) -> str:
    return (s.replace("\\n", "\n")
             .replace("\\,", ",")
             .replace("\\;", ";")
             .replace("\\\\", "\\"))


def parse_ics(text: str) -> list[dict]:
    """Déplie les lignes repliées (RFC 5545) puis extrait les VEVENT."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    events, cur = [], None

    for line in unfolded.split("\n"):
        line = line.rstrip("\r")
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue

        raw_key, value = line.split(":", 1)
        key = raw_key.split(";")[0].upper()

        if key == "UID":
            cur["uid"] = value.strip()
        elif key == "SUMMARY":
            cur["summary"] = ics_unescape(value)
        elif key == "DESCRIPTION":
            cur["description"] = ics_unescape(value)
        elif key == "LOCATION":
            cur["location"] = ics_unescape(value)
        elif key == "DTSTART":
            cur["start"] = value.strip()
        elif key == "DTEND":
            cur["end"] = value.strip()

    return events


def ics_date_to_rfc3339(v: str) -> str | None:
    """20261004T153000 → 2026-10-04T15:30:00 (heure locale Europe/Paris)."""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$", v or "")
    if not m:
        return None
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}"


# ── NocoDB ────────────────────────────────────────────────────────────────────
def fetch_created_calendars(token: str) -> list[dict]:
    """Liste les calendriers Google réellement créés (donc à synchroniser)."""
    rows, offset = [], 0
    while True:
        url = (f"{NOCODB_API}/api/v1/db/data/noco/{NOCODB_BASE}/{NOCODB_TABLE_GCAL}"
               f"?limit=100&offset={offset}")
        res = requests.get(url, headers={"xc-token": token}, timeout=30)
        res.raise_for_status()
        data = res.json()
        page = data.get("list") or data.get("records") or []
        rows.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return rows


# ── Sync ──────────────────────────────────────────────────────────────────────
def sync_calendar(service, calendar_id: str, fichier: str) -> tuple[int, int]:
    """Réimporte tous les matchs du .ics dans le calendrier Google."""
    path = os.path.join("docs", fichier)
    if not os.path.exists(path):
        print(f"  ⚠️  .ics introuvable : {path}")
        return 0, 0

    with open(path, encoding="utf-8") as f:
        events = parse_ics(f.read())

    ok = ko = 0
    for ev in events:
        start = ics_date_to_rfc3339(ev.get("start", ""))
        end   = ics_date_to_rfc3339(ev.get("end", ""))
        if not start or not end or not ev.get("uid"):
            continue

        body = {
            "iCalUID":     ev["uid"],
            "summary":     ev.get("summary") or "Match",
            "description": ev.get("description", ""),
            "location":    ev.get("location", ""),
            "start": {"dateTime": start, "timeZone": TIMEZONE},
            "end":   {"dateTime": end,   "timeZone": TIMEZONE},
        }
        try:
            # import est idempotent sur iCalUID : crée ou met à jour le match,
            # jamais de doublon.
            service.events().import_(calendarId=calendar_id, body=body).execute()
            ok += 1
        except Exception as e:
            ko += 1
            print(f"  ⚠️  {ev['uid'][:50]} : {e}")

    return ok, ko


def main():
    noco_token = os.environ.get("NOCODB_TOKEN")
    sa_json    = os.environ.get("GOOGLE_SA_JSON")

    if not noco_token or not sa_json:
        print("❌ NOCODB_TOKEN et GOOGLE_SA_JSON sont requis.")
        sys.exit(1)
    if not NOCODB_TABLE_GCAL:
        print("❌ NOCODB_TABLE_GCAL non défini (id de la table NocoDB).")
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json), scopes=SCOPES
    )
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    rows = fetch_created_calendars(noco_token)
    print(f"→ {len(rows)} calendrier(s) Google à synchroniser")

    total_ok = total_ko = 0
    for row in rows:
        cal_id  = row.get("google_calendar_id")
        fichier = row.get("fichier")
        if not cal_id or not fichier:
            continue
        print(f"\n── {row.get('equipe') or fichier}")
        ok, ko = sync_calendar(service, cal_id, fichier)
        print(f"   {ok} match(s) synchronisé(s)" + (f", {ko} en erreur" if ko else ""))
        total_ok += ok
        total_ko += ko

    print(f"\n✅ Terminé : {total_ok} match(s) synchronisé(s)"
          + (f", {total_ko} en erreur" if total_ko else ""))


if __name__ == "__main__":
    main()

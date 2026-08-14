"""
FFBB → Fichiers .ics statiques
────────────────────────────────
Scrape les matchs FFBB et génère des fichiers .ics dans docs/calendars/.
Pas de quota, pas d'API externe — 100% statique, hébergeable sur GitHub Pages.

Usage :
    # Toutes les poules auto-détectées
    python generate_ics.py "https://competitions.ffbb.com/ligues/ara/competitions/pnf?phase=200000002897651&poule=200000003055506"

    # Une seule poule
    python generate_ics.py --direct "URL_POULE_A" "URL_POULE_B"
"""
import asyncio
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

from scraper_http import scrape_competition, build_calendar_name, find_all_poule_urls, discover_competitions

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR   = "docs/calendars"       # dossier de sortie GitHub Pages
MANIFEST     = "docs/calendars.json"  # index lu par le frontend
TIMEZONE_STR = "Europe/Paris"
PRODID       = "-//FFBB Agenda//FR"

TEST_URL = (
    "https://competitions.ffbb.com/ligues/ara/competitions/pnf"
    "?phase=200000002897651&poule=200000003055506&journee=1"
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    """Convertit un texte en slug de nom de fichier (minuscules, tirets)."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80]


def ics_escape(text: str) -> str:
    """Échappe les caractères spéciaux ICS : \\ , ; \n"""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def fold_line(line: str) -> str:
    """Replie les lignes > 75 caractères (RFC 5545)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    result = []
    while len(line.encode("utf-8")) > 75:
        cut = 75
        while len(line[:cut].encode("utf-8")) > 75:
            cut -= 1
        result.append(line[:cut])
        line = " " + line[cut:]
    result.append(line)
    return "\r\n".join(result)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── Construction d'un VEVENT ──────────────────────────────────────────────────
def build_vevent(match: dict, competition_nom: str, uid_prefix: str, team: str | None = None) -> str:
    date    = match.get("date") or ""
    heure   = match.get("heure") or "10:00"
    eq1     = match.get("equipe1", "?")
    eq2     = match.get("equipe2", "?")
    score   = match.get("score", "")
    salle   = match.get("salle", "")
    adresse = match.get("adresse", "")
    waze    = match.get("waze", "")
    arb     = match.get("arbitres", [])
    murl    = match.get("match_url", "")

    # Emoji domicile/extérieur uniquement dans le calendrier équipe
    lieu_emoji = ""
    if team:
        lieu_emoji = "🏠 " if team == eq1 else "✈️ "

    # Titre
    titre = f"{lieu_emoji}{eq1} – {eq2}"
    if score and score not in ("", "0-0"):
        titre += f" ({score})"

    # Dates/heures
    if date:
        h, m = map(int, heure.split(":"))
        dt_start = datetime(
            int(date[:4]), int(date[5:7]), int(date[8:10]),
            h, m, 0
        )
        dt_end = dt_start + timedelta(hours=2)
        dtstart = dt_start.strftime("%Y%m%dT%H%M%S")
        dtend   = dt_end.strftime("%Y%m%dT%H%M%S")
    else:
        dtstart = "19700101T000000"
        dtend   = "19700101T020000"

    # UID unique
    uid_raw = f"{uid_prefix}_{date}_{heure}_{eq1}_{eq2}".replace(" ", "_")
    uid = slugify(uid_raw) + "@ffbb-agenda"

    # Location
    location_parts = [p for p in [salle, adresse] if p]
    location = ", ".join(location_parts)

    # Description
    desc_lines = [
        f"🏀 {competition_nom}",
        f"⚔️ {eq1} vs {eq2}",
    ]
    if score and score not in ("", "0-0"):
        desc_lines.append(f"📊 Score : {score}")
    if salle:
        desc_lines.append(f"\n{salle}")
    if adresse:
        desc_lines.append(f"📍 {adresse}")
    if waze:
        desc_lines.append(f"🚗 Waze : {waze}")
    desc_lines.append(
        f"\n📣 Arbitres : {', '.join(arb) if arb else 'Pas de désignation'}"
    )
    if murl:
        desc_lines.append(f"\n🔗 Feuille FFBB : {murl}")

    description = "\n".join(desc_lines)

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_utc()}",
        f"DTSTART;TZID={TIMEZONE_STR}:{dtstart}",
        f"DTEND;TZID={TIMEZONE_STR}:{dtend}",
        f"SUMMARY:{ics_escape(titre)}",
        f"DESCRIPTION:{ics_escape(description)}",
    ]
    if location:
        lines.append(f"LOCATION:{ics_escape(location)}")
    if murl:
        lines.append(f"URL:{murl}")
    lines.append("END:VEVENT")

    return "\r\n".join(fold_line(l) for l in lines)


# ── Construction d'un calendrier .ics complet ─────────────────────────────────
def build_ics(events_text: list[str], cal_name: str, description: str = "") -> str:
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(f'🏀 FFBB – {cal_name}')}",
        f"X-WR-CALDESC:{ics_escape(description or cal_name)}",
        "X-WR-TIMEZONE:Europe/Paris",
        # Définition minimale de la timezone Europe/Paris (CET/CEST)
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Paris",
        "BEGIN:STANDARD",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "END:STANDARD",
        "BEGIN:DAYLIGHT",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "END:DAYLIGHT",
        "END:VTIMEZONE",
    ]
    footer = ["END:VCALENDAR"]

    return "\r\n".join(header) + "\r\n" + "\r\n".join(events_text) + "\r\n" + "\r\n".join(footer) + "\r\n"


# ── Sauvegarde d'un fichier .ics ──────────────────────────────────────────────
def save_ics(content: str, filepath: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    print(f"  → {filepath}")


# ── Chargement/sauvegarde du manifest ────────────────────────────────────────
def load_manifest() -> dict:
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {"calendriers": [], "equipes": []}


def save_manifest(manifest: dict):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"  → Manifest : {MANIFEST}")


# ── Déduplication inter-poules (évite les re-scrapes de la même poule) ───────
_seen_fingerprints: set = set()


# ── Traitement d'une poule ────────────────────────────────────────────────────
async def process_poule(url: str, manifest: dict, base_url_prefix: str = ""):
    global _seen_fingerprints
    matches, meta = await scrape_competition(url, enrich_details=True)
    if not matches:
        print("  Aucun match trouvé.")
        return

    # Déduplique par empreinte des match_urls (évite re-écriture pour faux IDs __next_f)
    fp = frozenset(m.get("match_url", "") for m in matches if m.get("match_url"))
    if fp and fp in _seen_fingerprints:
        print("  ⚠️  Données identiques à une poule déjà traitée — ignoré.")
        return
    if fp:
        _seen_fingerprints.add(fp)

    comp_nom  = build_calendar_name(meta)   # ex: "PNF – ARA – Poule A"
    comp_slug = slugify(comp_nom)           # ex: "pnf-ara-poule-a"
    uid_prefix = meta.get("key", comp_slug)

    print(f"\n── {comp_nom} ({len(matches)} matchs)")

    # ── Calendrier championnat ─────────────────────────────────────────────
    events = [build_vevent(m, comp_nom, uid_prefix) for m in matches]
    ics_content = build_ics(events, comp_nom, f"Tous les matchs {comp_nom}")
    ics_path    = os.path.join(OUTPUT_DIR, f"{comp_slug}.ics")
    save_ics(ics_content, ics_path)

    # Ajouter au manifest (évite doublons)
    champ_entry = {
        "nom":      comp_nom,
        "slug":     comp_slug,
        "fichier":  f"calendars/{comp_slug}.ics",
        "meta":     meta,
    }
    manifest["calendriers"] = [
        c for c in manifest["calendriers"] if c["slug"] != comp_slug
    ]
    manifest["calendriers"].append(champ_entry)

    # ── Calendriers équipes ────────────────────────────────────────────────
    equipes = sorted({m["equipe1"] for m in matches} | {m["equipe2"] for m in matches})
    print(f"\n── Équipes ({len(equipes)})")

    for equipe in equipes:
        eq_matches = [m for m in matches if equipe in (m["equipe1"], m["equipe2"])]
        eq_slug    = slugify(f"{comp_slug}-{equipe}")
        eq_nom     = f"{equipe}  [{meta['slug']} – {meta['region']}" \
                     + (f" – {meta['poule']}" if meta.get("poule") else "") + "]"

        events_eq = [build_vevent(m, comp_nom, uid_prefix, team=equipe) for m in eq_matches]
        ics_eq    = build_ics(events_eq, eq_nom, f"Matchs de {equipe} – {comp_nom}")
        path_eq   = os.path.join(OUTPUT_DIR, "teams", f"{eq_slug}.ics")
        save_ics(ics_eq, path_eq)

        eq_entry = {
            "nom":            equipe,
            "nom_complet":    eq_nom,
            "slug":           eq_slug,
            "comp_slug":      comp_slug,
            "comp_nom":       comp_nom,
            "region":         meta.get("region", ""),
            "poule":          meta.get("poule", ""),
            "fichier":        f"calendars/teams/{eq_slug}.ics",
        }
        manifest["equipes"] = [
            e for e in manifest["equipes"] if e["slug"] != eq_slug
        ]
        manifest["equipes"].append(eq_entry)

    save_manifest(manifest)


# ── Pipeline principal ────────────────────────────────────────────────────────
async def main():
    args   = sys.argv[1:]
    direct = "--direct" in args
    region = "--region" in args
    urls   = [u for u in args if not u.startswith("--")] or [TEST_URL]

    manifest = load_manifest()

    if region:
        # Mode région : découverte automatique de toutes les compétitions
        for region_url in urls:
            print(f"\n{'='*60}")
            print(f"MODE RÉGION : {region_url}")
            print(f"{'='*60}")
            comp_urls = await discover_competitions(region_url)
            for comp_url in comp_urls:
                print(f"\n{'='*60}\n{comp_url}\n{'='*60}")
                poule_urls = await find_all_poule_urls(comp_url)
                print(f"→ {len(poule_urls)} poule(s)")
                for purl in poule_urls:
                    await process_poule(purl, manifest)

    elif direct:
        for url in urls:
            print(f"\n{'='*60}\n[direct] {url}\n{'='*60}")
            await process_poule(url, manifest)

    else:
        for url in urls:
            print(f"\n{'='*60}\nDétection poules : {url}\n{'='*60}")
            poule_urls = await find_all_poule_urls(url)
            print(f"→ {len(poule_urls)} poule(s)")
            for purl in poule_urls:
                await process_poule(purl, manifest)

    print(f"\n✅ Terminé. Fichiers dans ./{OUTPUT_DIR}/")
    print(f"   Manifest : ./{MANIFEST}")


if __name__ == "__main__":
    asyncio.run(main())

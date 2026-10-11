"""
FFBB Scraper « focus » — uniquement les équipes suivies (FOCUS ci-dessous).

Remplace, pour l'usage courant, generate_ics.py (--region / --national) qui
parcourait des centaines de poules et déclenchait le blocage de la FFBB.
Les anciens scripts (generate_ics.py, scraper_http.py, scraper.py) sont conservés.

Ici : 3 poules, 2 requêtes en parallèle, arbitres récupérés pour l'équipe
suivie seulement, et un .ics n'est réécrit que si son contenu a vraiment changé
(le DTSTAMP est ignoré) — donc pas de commit inutile. En cas d'échec réseau ou de
poule vide, le fichier existant est conservé tel quel.

Usage :
    python scrape_focus.py            # met à jour les 3 fichiers .ics d'équipe
    python scrape_focus.py --dry-run  # n'écrit rien, affiche seulement le résultat
Code retour : 0 si tout est OK, 1 si au moins une poule n'a pas pu être lue.
"""
import asyncio
import os
import re
import sys
from datetime import date, timedelta

import aiohttp

import scraper_http
from scraper_http import SSL_CTX, build_calendar_name, fetch_arbitres, scrape_poule
from generate_ics import OUTPUT_DIR, build_ics, build_vevent, save_ics, slugify

# Peu de requêtes à la fois : c'est le débit, pas le volume, qui fait couper la FFBB.
scraper_http.MAX_CONCURRENT = 2

BASE = "https://competitions.ffbb.com/ligues/ges/comites"

# Poules suivies. `slug` = nom du fichier .ics d'équipe déjà publié (inchangé,
# pour que les abonnements et les calendriers Google existants restent valides).
FOCUS = [
    {
        "equipe": "JOEUF HOMECOURT BASKET",
        "slug":   "idmu18-ges-meurthe-et-moselle-poule-c-joeuf-homecourt-basket",
        "url":    f"{BASE}/0054/competitions/idmu18?phase=200000002898335&poule=200000003056771",
    },
    {
        "equipe": "ASP STE MARIE AUX CHENES",
        "slug":   "idmu15-ges-meuse-poule-c-asp-ste-marie-aux-chenes",
        "url":    f"{BASE}/0055/competitions/idmu15?phase=200000002898186&poule=200000003056470",
    },
    {
        "equipe": "ASP STE MARIE AUX CHENES",
        "slug":   "idmu18-ges-meurthe-et-moselle-poule-d-asp-ste-marie-aux-chenes",
        "url":    f"{BASE}/0054/competitions/idmu18?phase=200000002898335&poule=200000003056772",
    },
]

ARBITRES_HORIZON_JOURS = 14


def _sans_dtstamp(ics: str) -> str:
    return re.sub(r"^DTSTAMP:.*\r?\n", "", ics, flags=re.M)


async def _ajoute_arbitres(matches: list[dict]) -> None:
    """Arbitres uniquement pour les matchs joués ou à venir dans l'horizon."""
    horizon = (date.today() + timedelta(days=ARBITRES_HORIZON_JOURS)).isoformat()
    eligibles = [
        m for m in matches
        if m.get("match_url") and (m.get("joue") or (m.get("date") or "")[:10] <= horizon)
    ]
    sem = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX)) as session:
        async def un(m):
            async with sem:
                try:
                    m["arbitres"] = await fetch_arbitres(session, m["match_url"])
                except Exception as e:
                    print(f"  ⚠️  arbitres indisponibles ({m['match_url']}) : {e}")
        await asyncio.gather(*(un(m) for m in eligibles))


async def traite(cfg: dict, dry_run: bool) -> bool:
    equipe = cfg["equipe"]
    print(f"\n── {equipe}  ({cfg['url'].split('/competitions/')[1].split('?')[0]})")
    try:
        matches, meta = await scrape_poule(cfg["url"], enrich_arbitres=False)
    except Exception as e:
        print(f"  ❌ Lecture impossible : {e} — fichier existant conservé.")
        return False

    mes_matchs = [m for m in matches if equipe in (m["equipe1"], m["equipe2"])]
    if not mes_matchs:
        print(f"  ❌ Aucun match pour « {equipe} » ({len(matches)} matchs dans la poule) — fichier conservé.")
        return False

    await _ajoute_arbitres(mes_matchs)

    comp_nom   = build_calendar_name(meta)
    uid_prefix = meta.get("key", slugify(comp_nom))
    eq_nom     = f"{equipe}  [{meta['slug']} – {meta['region']}" \
                 + (f" – {meta['poule']}" if meta.get("poule") else "") + "]"

    events = [build_vevent(m, comp_nom, uid_prefix, team=equipe) for m in mes_matchs]
    ics    = build_ics(events, eq_nom, f"Matchs de {equipe} – {comp_nom}")
    chemin = os.path.join(OUTPUT_DIR, "teams", f"{cfg['slug']}.ics")

    ancien = ""
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8", newline="") as f:
            ancien = f.read()

    if _sans_dtstamp(ancien) == _sans_dtstamp(ics):
        print(f"  = {len(mes_matchs)} matchs, aucun changement.")
    elif dry_run:
        print(f"  ~ {len(mes_matchs)} matchs, changements détectés (dry-run : rien écrit).")
    else:
        save_ics(ics, chemin)
        print(f"  ✓ {len(mes_matchs)} matchs, fichier mis à jour.")
    return True


async def main() -> int:
    dry_run = "--dry-run" in sys.argv
    ok = True
    for cfg in FOCUS:            # séquentiel : une poule après l'autre
        ok = await traite(cfg, dry_run) and ok
    print("\n✅ Terminé." if ok else "\n⚠️  Terminé avec des poules en échec.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

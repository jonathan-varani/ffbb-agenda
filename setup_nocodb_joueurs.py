"""
Ajoute les colonnes joueur_licence / joueur_naissance à la table
"Contacts Joueurs" de NocoDB, puis remplit les licences et années de naissance.

Sécurité : par défaut le script ne fait qu'un RAPPORT (dry-run). Il n'écrit
réellement dans NocoDB qu'avec l'option --apply.

Usage :
    python setup_nocodb_joueurs.py            # inspection + rapport, aucune écriture
    python setup_nocodb_joueurs.py --apply    # crée les colonnes et écrit les données
"""
import os
import sys
import unicodedata

import requests
from dotenv import load_dotenv

load_dotenv()

NOCODB_API   = "https://app.nocodb.com"
NOCODB_TABLE = "m135lw76cfsqy0a"          # table "Contacts Joueurs"

COL_LICENCE   = "joueur_licence"
COL_NAISSANCE = "joueur_naissance"

# Données relevées sur le trombinoscope FFBB (équipe U15M, ASP STE MARIE AUX CHENES)
# (initiale du nom, prénom, licence, année de naissance)
JOUEURS = [
    ("A", "Louis",    "VT640304", 1964),
    ("B", "Gabriel",  "BC133160", 2013),
    ("D", "Jules",    "BC137136", 2013),
    ("G", "Nolan",    "BC128938", 2012),
    ("G", "Evan",     "BC128194", 2012),
    ("K", "Younès",   "BC134692", 2013),
    ("K", "Tom",      "BC135000", 2013),
    ("K", "Martin",   "BC124583", 2012),
    ("M", "Tiago",    "BC120567", 2012),
    ("R", "Camille",  "BC130875", 2013),
    ("S", "Noah",     "BC138560", 2013),
    ("S", "Maël",     "BC128424", 2012),
    ("V", "Jonathan", "VT880029", 1988),
    ("V", "Léo",      "BC133888", 2013),
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def norm(s: str) -> str:
    """minuscules, sans accents ni espaces superflus."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip().lower()


def read_token() -> str:
    token = os.environ.get("NOCODB_TOKEN")
    if not token:
        print("❌ NOCODB_TOKEN introuvable (à définir dans .env)")
        sys.exit(1)
    return token


def get_table_meta(headers) -> dict:
    res = requests.get(f"{NOCODB_API}/api/v2/meta/tables/{NOCODB_TABLE}",
                       headers=headers, timeout=30)
    res.raise_for_status()
    return res.json()


def get_records(headers) -> list[dict]:
    rows, offset = [], 0
    while True:
        res = requests.get(
            f"{NOCODB_API}/api/v2/tables/{NOCODB_TABLE}/records",
            headers=headers, params={"limit": 100, "offset": offset}, timeout=30,
        )
        res.raise_for_status()
        page = res.json().get("list", [])
        rows.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return rows


def create_column(headers, title: str, uidt: str):
    res = requests.post(
        f"{NOCODB_API}/api/v2/meta/tables/{NOCODB_TABLE}/columns",
        headers=headers, json={"title": title, "uidt": uidt}, timeout=30,
    )
    if not res.ok:
        print(f"   ❌ Création colonne {title} : {res.status_code} {res.text[:200]}")
        return False
    print(f"   ✅ Colonne « {title} » créée.")
    return True


# ── Détection des colonnes de nom ─────────────────────────────────────────────
def guess_name_fields(columns: list[dict]) -> list[str]:
    """Repère les colonnes texte susceptibles de contenir nom / prénom."""
    prefer = ("prenom", "nom", "joueur", "name", "player")
    out = []
    for c in columns:
        t = norm(c.get("title"))
        if any(p in t for p in prefer) and "licence" not in t and "naissance" not in t:
            out.append(c["title"])
    return out


def match_row(rows: list[dict], name_fields: list[str], initiale: str, prenom: str):
    """Cherche la ligne dont le prénom correspond et le nom commence par l'initiale."""
    p, i = norm(prenom), norm(initiale)
    candidates = []

    for r in rows:
        blob = " ".join(norm(r.get(f)) for f in name_fields)
        if not blob.strip():
            continue
        # le prénom doit apparaître comme mot entier
        if p not in blob.split() and p not in blob:
            continue
        # l'initiale du nom doit apparaître (ex. "B." ou nom commençant par B)
        mots = [m for m in blob.replace(".", " ").split() if m and m != p]
        if any(m.startswith(i) for m in mots) or not mots:
            candidates.append(r)

    return candidates


def main():
    apply_mode = "--apply" in sys.argv
    headers = {"xc-token": read_token(), "Content-Type": "application/json"}

    meta    = get_table_meta(headers)
    columns = meta.get("columns", [])
    titles  = [c["title"] for c in columns]

    print(f"Table : {meta.get('title')}")
    print(f"Colonnes existantes : {', '.join(titles)}\n")

    # ── 1. Colonnes ───────────────────────────────────────────────────────────
    todo = [(COL_LICENCE, "SingleLineText"), (COL_NAISSANCE, "Number")]
    for title, uidt in todo:
        if title in titles:
            print(f"→ Colonne « {title} » déjà présente.")
        elif apply_mode:
            create_column(headers, title, uidt)
        else:
            print(f"→ [dry-run] Colonne « {title} » ({uidt}) à créer.")

    # ── 2. Correspondance joueurs ─────────────────────────────────────────────
    rows = get_records(headers)
    name_fields = guess_name_fields(columns)
    print(f"\nLignes dans la table : {len(rows)}")
    print(f"Colonnes de nom utilisées : {', '.join(name_fields) or '(aucune détectée)'}\n")

    if not name_fields:
        print("❌ Aucune colonne de nom détectée — impossible de faire la correspondance.")
        print("   Relance en me donnant le nom exact de la colonne des joueurs.")
        sys.exit(1)

    updates, problemes = [], []
    for initiale, prenom, licence, naissance in JOUEURS:
        found = match_row(rows, name_fields, initiale, prenom)
        label = f"{initiale}. {prenom}"

        if len(found) == 1:
            r = found[0]
            rid = r.get("Id") or r.get("id")
            apercu = " / ".join(str(r.get(f)) for f in name_fields if r.get(f))
            print(f"✅ {label:<14} → {apercu}  [{licence}, {naissance}]")
            updates.append({"Id": rid, COL_LICENCE: licence, COL_NAISSANCE: naissance})
        elif not found:
            print(f"❓ {label:<14} → aucune correspondance")
            problemes.append(label)
        else:
            print(f"⚠️  {label:<14} → {len(found)} correspondances, ambigu")
            problemes.append(label)

    # ── 3. Écriture ───────────────────────────────────────────────────────────
    print()
    if not apply_mode:
        print(f"[dry-run] {len(updates)} ligne(s) seraient mises à jour.")
        print("Relance avec --apply pour écrire réellement dans NocoDB.")
        return

    if not updates:
        print("Rien à écrire.")
        return

    res = requests.patch(
        f"{NOCODB_API}/api/v2/tables/{NOCODB_TABLE}/records",
        headers=headers, json=updates, timeout=60,
    )
    if res.ok:
        print(f"✅ {len(updates)} ligne(s) mise(s) à jour dans NocoDB.")
    else:
        print(f"❌ Échec écriture ({res.status_code}) : {res.text[:400]}")

    if problemes:
        print(f"\n⚠️  À compléter à la main : {', '.join(problemes)}")


if __name__ == "__main__":
    main()

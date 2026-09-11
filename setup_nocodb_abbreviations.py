"""
Crée (si besoin) la table NocoDB "Abreviations Equipes".

Cette table stocke la correspondance nom d'équipe long (tel que scrapé sur
FFBB) -> nom court, utilisée par generate_ics.py pour raccourcir les titres
d'événements calendrier. Si une équipe n'a pas de ligne ici, son nom est
affiché tel quel.

Le token est lu depuis Nocodb/Token_ffbb-agenda.txt et n'est jamais affiché.

Usage :
    python setup_nocodb_abbreviations.py
"""
import os
import sys

import requests

NOCODB_API  = "https://app.nocodb.com"
NOCODB_BASE = "poq54dd1rjvxuki"
TOKEN_FILE  = os.path.join("Nocodb", "Token_ffbb-agenda.txt")

TABLE_TITLE = "Abreviations Equipes"
COLUMNS = [
    {"title": "nom_long",  "uidt": "SingleLineText"},
    {"title": "nom_court", "uidt": "SingleLineText"},
]


def read_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        print(f"❌ Token introuvable : {TOKEN_FILE}")
        sys.exit(1)
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return f.read().strip()


def find_existing(headers) -> str | None:
    res = requests.get(
        f"{NOCODB_API}/api/v2/meta/bases/{NOCODB_BASE}/tables",
        headers=headers, timeout=30,
    )
    res.raise_for_status()
    for t in res.json().get("list", []):
        if t.get("title") == TABLE_TITLE:
            return t.get("id")
    return None


def create_table(headers) -> str:
    body = {
        "title": TABLE_TITLE,
        "table_name": "abreviations_equipes",
        "columns": [
            {"title": "Id", "uidt": "ID"},
            *COLUMNS,
        ],
    }
    res = requests.post(
        f"{NOCODB_API}/api/v2/meta/bases/{NOCODB_BASE}/tables",
        headers=headers, json=body, timeout=30,
    )
    if not res.ok:
        print(f"❌ Création échouée ({res.status_code}) : {res.text[:400]}")
        sys.exit(1)
    return res.json().get("id")


def main():
    headers = {"xc-token": read_token(), "Content-Type": "application/json"}

    table_id = find_existing(headers)
    if table_id:
        print(f"→ Table « {TABLE_TITLE} » déjà existante.")
    else:
        table_id = create_table(headers)
        print(f"✅ Table « {TABLE_TITLE} » créée.")

    print(f"   ID : {table_id}")
    print("\nProchaines étapes :")
    print(f"  1. Ajouter des lignes nom_long / nom_court directement dans NocoDB.")
    print(f"  2. Mettre à jour NOCODB_TABLE_ABBREV dans generate_ics.py avec : {table_id}")
    print(f"  3. Vérifier que le secret GitHub NOCODB_TOKEN est bien utilisé par les workflows scrape-*.yml")


if __name__ == "__main__":
    main()

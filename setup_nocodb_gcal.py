"""
Crée (si besoin) la table NocoDB "Calendriers Google" et met à jour
cloudflare-worker/worker.js avec son ID.

Cette table stocke le mapping équipe → id du vrai calendrier Google, pour
qu'un calendrier ne soit créé qu'une seule fois par équipe.

Le token est lu depuis Nocodb/Token_ffbb-agenda.txt et n'est jamais affiché.

Usage :
    python setup_nocodb_gcal.py
"""
import json
import os
import re
import sys

import requests

NOCODB_API  = "https://app.nocodb.com"
NOCODB_BASE = "poq54dd1rjvxuki"
TOKEN_FILE  = os.path.join("Nocodb", "Token_ffbb-agenda.txt")
WORKER_FILE = os.path.join("cloudflare-worker", "worker.js")

TABLE_TITLE = "Calendriers Google"
COLUMNS = [
    {"title": "fichier",            "uidt": "SingleLineText"},
    {"title": "equipe",             "uidt": "SingleLineText"},
    {"title": "google_calendar_id", "uidt": "SingleLineText"},
    {"title": "created_at",         "uidt": "SingleLineText"},
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
        "table_name": "calendriers_google",
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


def patch_worker(table_id: str):
    if not os.path.exists(WORKER_FILE):
        print(f"⚠️  {WORKER_FILE} introuvable, mise à jour manuelle nécessaire.")
        return
    with open(WORKER_FILE, encoding="utf-8") as f:
        src = f.read()

    new_src, n = re.subn(
        r'const NOCODB_TABLE_GCAL = "[^"]*";',
        f'const NOCODB_TABLE_GCAL = "{table_id}";',
        src,
    )
    if n == 0:
        print("⚠️  Ligne NOCODB_TABLE_GCAL introuvable dans worker.js.")
        return
    if new_src == src:
        print("→ worker.js déjà à jour.")
        return

    with open(WORKER_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(new_src)
    print(f"→ worker.js mis à jour avec l'ID de la table.")


def main():
    headers = {"xc-token": read_token(), "Content-Type": "application/json"}

    table_id = find_existing(headers)
    if table_id:
        print(f"→ Table « {TABLE_TITLE} » déjà existante.")
    else:
        table_id = create_table(headers)
        print(f"✅ Table « {TABLE_TITLE} » créée.")

    print(f"   ID : {table_id}")
    patch_worker(table_id)

    print("\nProchaines étapes :")
    print("  1. Copier cloudflare-worker/worker.js dans le dashboard Cloudflare")
    print("  2. Secret Cloudflare  : GOOGLE_SA_JSON (contenu de service_account.json)")
    print(f"  3. Secrets GitHub     : NOCODB_TOKEN, GOOGLE_SA_JSON, NOCODB_TABLE_GCAL={table_id}")


if __name__ == "__main__":
    main()

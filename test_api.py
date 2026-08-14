import requests
import json

BASE = "https://api.ffbb.app"

# Récupère d'abord la config pour obtenir les tokens
r = requests.get(f"{BASE}/items/configuration", headers={
    "Referer": "https://competitions.ffbb.com/",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
})
config = r.json()["data"]
print("Config complète:")
print(json.dumps(config, indent=2, ensure_ascii=False))

# Essai avec key_dh comme token Bearer
TOKEN = config.get("key_dh", "")
TOKEN_COMP = config.get("key_directus_competitions", "")
print(f"\nToken key_dh     : {TOKEN}")
print(f"Token key_comp   : {TOKEN_COMP}")

HEADERS_TOKEN = {
    "Referer": "https://competitions.ffbb.com/",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Authorization": f"Bearer {TOKEN}",
}

print("\n=== TEST ligues avec Bearer key_dh ===")
r = requests.get(f"{BASE}/items/ligues?limit=5&fields=code,nom,id", headers=HEADERS_TOKEN)
print(f"Status: {r.status_code}")
if r.ok:
    print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:800])
else:
    print(r.text[:200])

# Essai avec key_directus_competitions
HEADERS_TOKEN2 = {**HEADERS_TOKEN, "Authorization": f"Bearer {TOKEN_COMP}"}
print("\n=== TEST ligues avec Bearer key_directus_competitions ===")
r = requests.get(f"{BASE}/items/ligues?limit=5&fields=code,nom,id", headers=HEADERS_TOKEN2)
print(f"Status: {r.status_code}")
if r.ok:
    print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:800])
else:
    print(r.text[:200])

# Essai via ?access_token=
print("\n=== TEST matchs via ?access_token=key_dh ===")
r = requests.get(
    f"{BASE}/items/matchs?access_token={TOKEN}&filter[phase_id][_eq]=200000002897651&limit=3",
    headers={"Referer": "https://competitions.ffbb.com/", "User-Agent": "Mozilla/5.0"}
)
print(f"Status: {r.status_code}")
if r.ok:
    print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:800])
else:
    print(r.text[:200])


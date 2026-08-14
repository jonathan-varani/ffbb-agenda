"""
Extrait les rencontres depuis le JSON double-encodé dans __next_f.
Lance : python find_api.py
"""
import re, json, requests

TARGET_URL = (
    "https://competitions.ffbb.com/ligues/ges/competitions/pnm"
    "?phase=200000002897412&poule=200000003054918&journee=1"
)
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "fr-FR,fr;q=0.9"}

r    = requests.get(TARGET_URL, headers=HEADERS, timeout=30)
html = r.text

# Les données sont dans un script __next_f sous forme de string JS échappée :
# self.__next_f.push([1, "...\"rencontres\":[{\"id\":...}]..."])
# On extrait la chaîne brute du script le plus long contenant "rencontres"
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "html.parser")

target_script = None
for script in soup.find_all("script"):
    txt = script.string or ""
    if "rencontres" in txt:
        target_script = txt
        print(f"Script trouvé : {len(txt)} chars")
        break

if not target_script:
    # Fallback : cherche directement dans le HTML brut
    idx = html.find("rencontres")
    print(f"'rencontres' dans HTML brut à idx={idx}")
    print(html[max(0,idx-50):idx+500])
else:
    # Extrait la string JS : self.__next_f.push([1,"STRING"])
    m = re.search(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', target_script, re.DOTALL)
    if m:
        raw = m.group(1)
        # Décode les escapes JS (\", \\, \n, etc.)
        decoded = raw.encode().decode("unicode_escape", errors="replace")
    else:
        # Peut-être que le script contient juste le JSON directement
        decoded = target_script

    # Cherche le bloc rencontres dans le décodé
    idx = decoded.find("rencontres")
    print(f"'rencontres' trouvé à idx={idx} dans le script décodé")
    print(f"Contexte : {decoded[max(0,idx-30):idx+800]}")

    # Sauvegarde pour analyse
    with open("script_decoded.txt", "w", encoding="utf-8") as f:
        f.write(decoded)
    print("\n→ Sauvegardé dans script_decoded.txt")

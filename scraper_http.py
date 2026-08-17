"""
FFBB Scraper HTTP — sans Playwright, sans navigateur.
Utilise requests + BeautifulSoup sur le HTML SSR (100x plus rapide).

Usage :
    python scraper_http.py "URL_POULE"
    # Ex: python scraper_http.py "https://competitions.ffbb.com/ligues/ges/competitions/pnm?phase=200000002897412&poule=200000003054918"
"""
import asyncio
import json
import re
import ssl
import sys
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup

# Le certificat SSL de competitions.ffbb.com est parfois expiré — on désactive la vérif.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode    = ssl.CERT_NONE

BASE    = "https://competitions.ffbb.com"
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept":          "text/html,application/xhtml+xml",
}
MAX_CONCURRENT = 8   # requêtes parallèles max


# ── Extraction du JSON __next_f ───────────────────────────────────────────────

def _extract_rencontres_array(decoded: str, poule_id: str | None = None) -> list[dict]:
    """
    Extrait le tableau rencontres d'un JSON __next_f décodé.
    Si poule_id est fourni, cherche le tableau de rencontres appartenant à cette poule.
    Le HTML FFBB contient TOUTES les poules — il faut donc cibler la bonne.
    """
    def extract_array_at(text: str, bracket_pos: int) -> list[dict]:
        """Extrait un tableau JSON à partir de la position du '[' d'ouverture."""
        depth = 0
        end   = bracket_pos
        for i, c in enumerate(text[bracket_pos:], bracket_pos):
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            return json.loads(text[bracket_pos:end])
        except json.JSONDecodeError:
            return []

    if poule_id:
        # Cherche "id":"POULE_ID" (avec ou sans espace après :) dans le JSON
        for id_pat in [f'"id":"{poule_id}"', f'"id": "{poule_id}"']:
            idx = decoded.find(id_pat)
            if idx == -1:
                continue
            # Cherche "rencontres":[ après cet ID, dans une fenêtre raisonnable
            window = decoded[idx: idx + 2000]
            ren_m = re.search(r'"rencontres"\s*:\s*(\[)', window)
            if ren_m:
                abs_pos = idx + ren_m.start(1)
                result = extract_array_at(decoded, abs_pos)
                if isinstance(result, list):
                    return result

    # Fallback : première occurrence de "rencontres"
    match = re.search(r'"rencontres"\s*:\s*(\[)', decoded)
    if match:
        result = extract_array_at(decoded, match.start(1))
        if isinstance(result, list):
            return result
    return []


def _decode_next_f_script(txt: str) -> str | None:
    """Décode un script self.__next_f.push([1,"..."]) en JSON lisible."""
    m = re.search(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)\s*$', txt, re.DOTALL)
    if not m:
        return None
    raw = m.group(1)
    try:
        decoded = raw.encode("utf-8").decode("unicode_escape", errors="replace")
        try:
            decoded = decoded.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        return decoded
    except Exception:
        return raw.replace('\\"', '"').replace('\\\\', '\\')


def extract_next_f_json(html: str, poule_id: str | None = None) -> list[dict]:
    """
    Extrait la liste des rencontres depuis les scripts __next_f embarqués dans le HTML SSR.
    poule_id : si fourni, extrait les rencontres de cette poule spécifique.
    Retourne une liste de dicts (format brut FFBB).
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        txt = script.string or ""
        if "rencontres" not in txt:
            continue

        decoded = _decode_next_f_script(txt)
        if decoded is None:
            decoded = txt

        rencontres = _extract_rencontres_array(decoded, poule_id)
        if rencontres:
            return rencontres

    return []


def extract_journee_numbers(html: str) -> list[int]:
    """Extrait les numéros de journées disponibles depuis le HTML."""
    soup = BeautifulSoup(html, "html.parser")
    journees = set()

    # Cherche les options du dropdown journée
    for el in soup.find_all(string=re.compile(r"^J(\d+)$")):
        m = re.match(r"^J(\d+)$", el.strip())
        if m:
            journees.add(int(m.group(1)))

    # Fallback : cherche dans le HTML brut
    if not journees:
        for m in re.finditer(r'"numeroJournee"\s*:\s*"(\d+)"', html):
            journees.add(int(m.group(1)))

    return sorted(journees)


# ── Conversion rencontre brute → format match ─────────────────────────────────

def parse_rencontre(r: dict) -> dict:
    eq1      = r.get("idEngagementEquipe1") or {}
    eq2      = r.get("idEngagementEquipe2") or {}
    salle_d  = r.get("salle") or {}
    carto    = salle_d.get("cartographie") or {}

    # Noms équipes (nom + numéro)
    def equipe_nom(eng: dict) -> str:
        nom = eng.get("nom", "").strip()
        num = eng.get("numeroEquipe", "").strip()
        return f"{nom} - {num}" if num else nom

    equipe1 = equipe_nom(eq1)
    equipe2 = equipe_nom(eq2)

    # Date + heure depuis "2026-10-03T20:30:00"
    date_raw = r.get("date_rencontre", "")
    date     = date_raw[:10]   if len(date_raw) >= 10 else ""
    heure    = date_raw[11:16] if len(date_raw) >= 16 else "00:00"

    # Scores
    s1 = r.get("resultatEquipe1")
    s2 = r.get("resultatEquipe2")
    score = f"{s1}-{s2}" if s1 is not None and s2 is not None else ""

    # Salle
    salle   = salle_d.get("libelle", "").strip()
    adresse = " ".join(filter(None, [
        carto.get("adresse", ""),
        carto.get("ville", ""),
        carto.get("codePostal", ""),
    ])).strip()
    lat = carto.get("latitude")
    lon = carto.get("longitude")
    waze = f"https://waze.com/ul?ll={lat},{lon}&navigate=yes" if lat and lon else ""

    match_url = BASE + (r.get("url_competition") or "")

    return {
        "equipe1":   equipe1,
        "equipe2":   equipe2,
        "date":      date,
        "heure":     heure,
        "score":     score,
        "salle":     salle,
        "adresse":   adresse,
        "waze":      waze,
        "match_url": match_url,
        "joue":      r.get("joue", False),
        "arbitres":  [],   # rempli séparément si besoin
    }


# ── Scrape page arbitres (match detail) ───────────────────────────────────────

async def fetch_arbitres(session: aiohttp.ClientSession, match_url: str) -> list[str]:
    """
    Extrait les arbitres depuis la page match detail (SSR).
    Les arbitres apparaissent dans le HTML sous la forme :
        <element>Arbitre</element>
        <element>PRENOM NOM</element>
    Stratégie 1 : JSON __next_f (clés "officiels" / "arbitres")
    Stratégie 2 : Parsing HTML — cherche les éléments texte "Arbitre"
                  et récupère le texte qui suit
    """
    if not match_url or "/match/" not in match_url:
        return []
    try:
        async with session.get(match_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            html = await resp.text()
    except Exception:
        return []

    soup = BeautifulSoup(html, "html.parser")

    # ── Stratégie 1 : JSON __next_f ──────────────────────────────────────────
    for script in soup.find_all("script"):
        txt = script.string or ""
        if "officiels" not in txt and "arbitre" not in txt.lower():
            continue
        for pat in [r'"officiels"\s*:\s*(\[.*?\])', r'"arbitres"\s*:\s*(\[.*?\])']:
            m = re.search(pat, txt, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    noms = []
                    for item in data:
                        if isinstance(item, dict):
                            n = (item.get("nom") or item.get("name")
                                 or item.get("label") or item.get("prenom_nom") or "")
                            if n:
                                noms.append(n.strip())
                        elif isinstance(item, str):
                            noms.append(item.strip())
                    if noms:
                        return noms
                except Exception:
                    pass

    # ── Stratégie 2 : HTML texte — label "Arbitre" suivi du nom ─────────────
    # Sur la page match, le pattern observé est :
    #   <p|div|span>Arbitre</...>  <p|div|span>Laurent KUBLER</...>
    noms = []
    all_elements = soup.find_all(["p", "div", "span", "li", "dt", "dd"])
    for i, el in enumerate(all_elements):
        txt = el.get_text(strip=True)
        if txt.lower() == "arbitre":
            # Cherche le prochain élément non-vide
            for j in range(i + 1, min(i + 5, len(all_elements))):
                candidate = all_elements[j].get_text(strip=True)
                if candidate and candidate.lower() != "arbitre" and len(candidate) > 3:
                    noms.append(candidate)
                    break

    return noms


# ── Scrape une journée ────────────────────────────────────────────────────────

async def scrape_journee(
    session: aiohttp.ClientSession,
    base_url: str,
    journee: int,
    sem: asyncio.Semaphore,
    enrich_arbitres: bool = False,
    poule_id: str | None = None,
) -> list[dict]:
    url = f"{base_url}&journee={journee}"
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    html = await resp.text()
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  ⚠️  J{journee} erreur après 3 tentatives : {e}")
                    return []
                await asyncio.sleep(2 ** attempt)  # 1s, 2s

    rencontres_raw = extract_next_f_json(html, poule_id=poule_id)
    matches = [parse_rencontre(r) for r in rencontres_raw]

    if enrich_arbitres and matches:
        from datetime import date as _date, timedelta as _td
        horizon = (_date.today() + _td(days=14)).isoformat()
        # Fetch arbitres pour matchs passés ou dans les 14 prochains jours
        eligible = [
            m for m in matches
            if m.get("match_url") and (m.get("joue") or (m.get("date", "") and m["date"][:10] <= horizon))
        ]
        arb_tasks = [fetch_arbitres(session, m["match_url"]) for m in eligible]
        arb_results = await asyncio.gather(*arb_tasks)
        for m, arb in zip(eligible, arb_results):
            m["arbitres"] = arb

    return matches


# ── Scrape une poule complète (toutes journées) ───────────────────────────────

async def scrape_poule(
    base_url: str,
    enrich_arbitres: bool = False,
) -> tuple[list[dict], dict]:
    """
    Scrape toutes les journées d'une poule.
    base_url = URL sans &journee= (ex: ?phase=...&poule=...)
    Retourne (liste_matchs, meta_dict).
    """
    # Nettoyage de l'URL : enlève &journee= si présent
    base_url = re.sub(r"&journee=\d+", "", base_url)

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX)) as session:
        # Charge journée 1 pour découvrir le nombre de journées + la méta
        j1_url = f"{base_url}&journee=1"
        async with sem:
            async with session.get(j1_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                html_j1 = await resp.text()

        journees = extract_journee_numbers(html_j1)
        if not journees:
            journees = list(range(1, 23))   # fallback : 22 journées
        print(f"  {len(journees)} journée(s) détectées : J{journees[0]}–J{journees[-1]}")

        # Méta depuis le HTML
        meta = extract_meta(html_j1, base_url)

        # Extrait le poule_id de l'URL pour cibler la bonne poule dans le JSON
        poule_id_m = re.search(r"poule=(\d+)", base_url)
        poule_id   = poule_id_m.group(1) if poule_id_m else None

        # Scrape toutes les journées en parallèle
        tasks = [
            scrape_journee(session, base_url, j, sem, enrich_arbitres, poule_id=poule_id)
            for j in journees
        ]
        results = await asyncio.gather(*tasks)

    # Déduplique par match_url
    seen     = set()
    all_matches = []
    for batch in results:
        for m in batch:
            key = m["match_url"]
            if key not in seen:
                seen.add(key)
                all_matches.append(m)

    print(f"  {len(all_matches)} matchs uniques")
    return all_matches, meta


# ── Extraction méta (nom poule, championnat, région) ─────────────────────────

def extract_meta(html: str, url: str) -> dict:
    soup  = BeautifulSoup(html, "html.parser")
    title = soup.find("title")
    title_txt = title.get_text(strip=True) if title else ""

    # Extrait phase, poule depuis l'URL
    phase_m  = re.search(r"phase=(\d+)", url)
    poule_m  = re.search(r"poule=(\d+)", url)
    ligueCode = re.search(r"/ligues/(\w+)/", url)
    compCode  = re.search(r"/competitions/([\w-]+)", url)

    # Depuis le JSON __next_f (cherche dans le contenu décodé)
    poule_nom = ""
    slug_nom  = title_txt and re.sub(r"\s*\|.*", "", title_txt).strip() or ""
    region    = ligueCode.group(1).upper() if ligueCode else ""

    for script in soup.find_all("script"):
        txt = script.string or ""
        if not txt or "nom" not in txt:
            continue

        # Décode unicode_escape comme dans extract_next_f_json
        m_raw = re.search(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)\s*$', txt, re.DOTALL)
        if m_raw:
            raw = m_raw.group(1)
            try:
                decoded = raw.encode("utf-8").decode("unicode_escape", errors="replace")
                try:
                    decoded = decoded.encode("latin-1").decode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
            except Exception:
                decoded = txt
        else:
            decoded = txt

        # Cherche "Poule X" ou "Groupe X" en ciblant d'abord le poule_id de l'URL
        if not poule_nom:
            pid = poule_m.group(1) if poule_m else None
            if pid:
                # Cherche "id":"POULE_ID" dans le JSON, puis "nom" dans une fenêtre proche
                for pid_pat in [f'"id":"{pid}"', f'"id": "{pid}"']:
                    idx = decoded.find(pid_pat)
                    if idx >= 0:
                        # "nom" peut être avant ou après l'id dans le JSON
                        window = decoded[max(0, idx - 300): idx + 300]
                        nm = re.search(r'"nom"\s*:\s*"((?:Poule|Groupe)\s+\w+)"', window)
                        if nm:
                            poule_nom = nm.group(1)
                            break
            # Fallback : première occurrence (compétition à une seule poule)
            if not poule_nom:
                pm2 = re.search(r'"nom"\s*:\s*"((?:Poule|Groupe)\s+\w+)"', decoded)
                if pm2:
                    poule_nom = pm2.group(1)

    return {
        "slug":    compCode.group(1).upper() if compCode else "",
        "poule":   poule_nom,
        "region":  region,
        "titre":   slug_nom or title_txt,
        "phase":   phase_m.group(1) if phase_m else "",
        "poule_id": poule_m.group(1) if poule_m else "",
        "key":     f"{compCode.group(1) if compCode else ''}-{region}-{poule_nom}".lower(),
    }


# ── Compatibilité avec generate_ics.py ───────────────────────────────────────

async def scrape_competition(url: str, enrich_details: bool = True) -> tuple[list[dict], dict]:
    """Interface compatible avec l'ancien scraper.py."""
    return await scrape_poule(url, enrich_arbitres=enrich_details)


def build_calendar_name(meta: dict) -> str:
    parts = [meta.get("slug",""), meta.get("region",""), meta.get("poule","")]
    return " – ".join(p for p in parts if p)


# ── Découverte des poules d'une compétition ───────────────────────────────────

async def find_all_poule_urls(url: str) -> list[str]:
    """
    Retourne toutes les URLs de poules d'une compétition.
    Remplace la version Playwright : utilise requests sur le HTML SSR.
    """
    # Base URL sans journee= ni poule=
    base = re.sub(r"[?&]journee=\d+", "", url)
    base = re.sub(r"[?&]poule=\d+", "", base).rstrip("?&")
    sep  = "&" if "?" in base else "?"

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX)) as session:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")

    # 1. <select aria-label="Poules"> (SSR)
    # Les IDs FFBB sont des entiers de 15 chiffres commençant par 2000
    select = soup.find("select", attrs={"aria-label": re.compile(r"(?i)^poules?$")})
    if select:
        all_vals = [opt.get("value", "").strip() for opt in select.find_all("option")]
        # Filtre uniquement les vrais IDs FFBB (≥ 10 chiffres)
        poules = [v for v in all_vals if re.match(r"^\d{10,}$", v)]
        print(f"  ℹ️  <select> valeurs brutes : {all_vals[:8]}")
        if poules:
            poules_uniq = list(dict.fromkeys(poules))  # déduplique en gardant l'ordre
            if len(poules_uniq) < len(poules):
                print(f"  ⚠️  IDs dupliqués dans <select> ({len(poules)}→{len(poules_uniq)} uniques) — SSR placeholder")
            else:
                print(f"  → {len(poules_uniq)} poule(s) via <select>")
                return [f"{base}{sep}poule={p}" for p in poules_uniq]
        elif all_vals:
            print(f"  ⚠️  <select> trouvé mais valeurs courtes : {all_vals[:5]}")

    # 2. Cherche les IDs de poules dans les liens <a href>
    poules = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"poule=(\d+)", a["href"])
        if m:
            poules.add(m.group(1))
    if poules:
        print(f"  → {len(poules)} poule(s) via liens")
        return [f"{base}{sep}poule={p}" for p in sorted(poules)]

    # 3. Cherche dans le JSON __next_f décodé
    #    Pattern attendu : "idPoule":{"id":"200000003054918",...} ou "poule":{"id":...}
    #    ou liste de poules dans les classements : [{"poule":{"id":"...","nom":"Poule A"},...}]
    for script in soup.find_all("script"):
        txt = script.string or ""
        if not txt or "poule" not in txt.lower():
            continue
        m_raw = re.search(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)\s*$', txt, re.DOTALL)
        if not m_raw:
            continue
        raw = m_raw.group(1)
        try:
            decoded = raw.encode("utf-8").decode("unicode_escape", errors="replace")
            try:
                decoded = decoded.encode("latin-1").decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
        except Exception:
            decoded = txt

        # Cherche les IDs de poules dans le JSON décodé
        # Pattern 1 : "idPoule":{"id":"..."}
        for pm in re.finditer(r'"(?:idPoule|poule)"\s*:\s*\{[^}]*"id"\s*:\s*"(\d{10,})"', decoded):
            poules.add(pm.group(1))
        # Pattern 2 : "id":"200000003054918" suivi de "nom":"Poule ..."
        for pm in re.finditer(r'"id"\s*:\s*"(\d{10,})"\s*,[^}]*"nom"\s*:\s*"(?:Poule|Groupe)\s', decoded):
            poules.add(pm.group(1))
        # Pattern 3 : IDs avec "nom":"Poule/Groupe" dans une fenêtre de 200 chars
        for pm in re.finditer(r'"(\d{10,})"', decoded):
            id_pos = pm.start()
            window = decoded[max(0, id_pos - 200): id_pos + 200]
            if re.search(r'"nom"\s*:\s*"(?:Poule|Groupe)\s', window):
                poules.add(pm.group(1))

    if poules:
        print(f"  → {len(poules)} poule(s) via __next_f")
        return [f"{base}{sep}poule={p}" for p in sorted(poules)]

    # 4. Fallback : poule courante uniquement
    current = re.search(r"poule=(\d+)", url)
    if current:
        return [f"{base}{sep}poule={current.group(1)}"]
    return [url]


# ── Découverte des compétitions d'une région ──────────────────────────────────

async def discover_competitions(
    region_url: str,
    exclude: list[str] | None = None,
) -> list[str]:
    """
    Découvre toutes les compétitions d'une page de ligue FFBB.
    Remplace la version Playwright : utilise requests sur le HTML SSR.
    """
    exclude = [kw.lower() for kw in (exclude or ["coupe", "plateau", "cup", "amicale"])]
    # La FFBB nomme systématiquement les compétitions "Amicale" avec un slug
    # du type "17-ami-nmu18" — signal plus fiable que le texte du lien (qui
    # peut être vide/générique si plusieurs <a> pointent vers le même slug,
    # ex: menu mobile dupliqué).
    amicale_slug_re = re.compile(r"/\d+-ami-", re.I)
    print(f"\n  Découverte des compétitions : {region_url}")

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX)) as session:
        async with session.get(region_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            html = await resp.text()

    soup  = BeautifulSoup(html, "html.parser")
    slugs: dict[str, str] = {}   # slug_path → label

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/competitions/" not in href or "/match/" in href:
            continue
        m = re.search(r"(/ligues/[^/]+/competitions/[^/?#]+)", href)
        if not m:
            continue
        slug_path = m.group(1)
        label = a.get_text(strip=True)
        # Garde le label le plus long/informatif si le slug apparaît plusieurs fois
        if slug_path not in slugs or len(label) > len(slugs[slug_path]):
            slugs[slug_path] = label

    print(f"  {len(slugs)} slug(s) trouvé(s) :")
    for sp, lb in slugs.items():
        print(f"    {sp!r}  label={lb!r}")

    # ── Priorité : cherche phase= directement dans les liens de la page région ──
    # Souvent les liens incluent déjà ?phase=XXXX
    result_from_links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        pm = re.search(r"phase=(\d+)", href)
        slug_m = re.search(r"(/ligues/[^/]+/competitions/[^/?#]+)", href)
        if pm and slug_m:
            slug_path = slug_m.group(1)
            label = a.get_text(strip=True)
            low = slug_path.lower() + " " + label.lower()
            if not any(kw in low for kw in exclude) and not amicale_slug_re.search(slug_path):
                url_with_phase = BASE + slug_path + "?phase=" + pm.group(1)
                result_from_links[slug_path] = url_with_phase
            else:
                print(f"  ⏭ Exclu : {label!r} ({slug_path})")

    if result_from_links:
        print(f"  → {len(result_from_links)} compétition(s) avec phase= trouvée(s) directement dans les liens")
        return list(result_from_links.values())

    # Filtrage et résolution des URLs avec phase= (visite chaque comp)
    async def resolve(slug_path: str, label: str) -> str | None:
        low = slug_path.lower() + " " + label.lower()
        if any(kw in low for kw in exclude) or amicale_slug_re.search(slug_path):
            print(f"  ⏭ Exclu : {label!r} ({slug_path})")
            return None
        return await _resolve_phase_url(slug_path)

    sem = asyncio.Semaphore(4)

    async def resolve_safe(slug_path: str, label: str) -> str | None:
        async with sem:
            return await resolve(slug_path, label)

    tasks = [resolve_safe(sp, lb) for sp, lb in slugs.items()]
    results = await asyncio.gather(*tasks)
    urls = [r for r in results if r]

    print(f"  → {len(urls)} compétition(s) résolue(s)")
    return urls


# ── Résolution du ?phase=XXXX d'une compétition (partagée région/national) ────

async def _resolve_phase_url(slug_path: str) -> str | None:
    """
    Résout l'URL "?phase=XXXX" d'une compétition à partir de son slug_path
    (ex: "/competitions/nf1" pour un championnat national, ou
    "/ligues/ges/competitions/pnm" pour une compétition régionale).
    """
    urls_to_try = [
        BASE + slug_path + "?journee=1",
        BASE + slug_path,
    ]
    html = ""
    slug_re = r"(/ligues/[^/]+/competitions/[^/?#]+|/competitions/[^/?#]+)"

    for comp_url in urls_to_try:
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX)) as session:
                async with session.get(comp_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    html = await resp.text()
        except Exception as e:
            print(f"  ⚠️ Erreur fetch {comp_url}: {e}")
            continue

        # 1a. data-phase-test-id="..." (attribut HTML FFBB)
        phase_m = re.search(r'data-phase-test-id="(\d+)"', html)
        if not phase_m:
            # 1b. phase= dans le contenu HTML brut (canonical, liens, etc.)
            phase_m = re.search(r"phase=(\d+)", html)
        if phase_m:
            m2 = re.search(slug_re, slug_path)
            clean_base = BASE + (m2.group(1) if m2 else slug_path)
            result = f"{clean_base}?phase={phase_m.group(1)}"
            print(f"  ✓ {slug_path} → phase={phase_m.group(1)}")
            return result

        # 2. Cherche dans le JSON __next_f décodé
        soup_c = BeautifulSoup(html, "html.parser")
        for script in soup_c.find_all("script"):
            txt = script.string or ""
            if not txt:
                continue
            m_raw = re.search(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)\s*$', txt, re.DOTALL)
            if not m_raw:
                continue
            raw = m_raw.group(1)
            try:
                decoded = raw.encode("utf-8").decode("unicode_escape", errors="replace")
                try:
                    decoded = decoded.encode("latin-1").decode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
            except Exception:
                decoded = txt

            # phase= dans le décodé
            pm = re.search(r"phase=(\d+)", decoded)
            if pm:
                m2 = re.search(slug_re, slug_path)
                clean_base = BASE + (m2.group(1) if m2 else slug_path)
                result = f"{clean_base}?phase={pm.group(1)}"
                print(f"  ✓ {slug_path} → phase={pm.group(1)} (via __next_f)")
                return result

            # Cherche une clé JSON "idPhase" ou "id_phase" ou similaire
            pm2 = re.search(
                r'"(?:idPhase|phaseId|phase_id|id_phase|currentPhase)"\s*:\s*"(\d{10,})"',
                decoded,
            )
            if pm2:
                m2 = re.search(slug_re, slug_path)
                clean_base = BASE + (m2.group(1) if m2 else slug_path)
                result = f"{clean_base}?phase={pm2.group(1)}"
                print(f"  ✓ {slug_path} → phase={pm2.group(1)} (via JSON key)")
                return result

            # Cherche un grand ID FFBB (15 chiffres) dans le contexte de "phase"
            for pm3 in re.finditer(r'"phase[^"]*"\s*:\s*\{[^}]*"id"\s*:\s*"(\d{10,})"', decoded):
                m2 = re.search(slug_re, slug_path)
                clean_base = BASE + (m2.group(1) if m2 else slug_path)
                result = f"{clean_base}?phase={pm3.group(1)}"
                print(f"  ✓ {slug_path} → phase={pm3.group(1)} (via phase.id)")
                return result

    print(f"  ✗ {slug_path} — phase= introuvable")
    return None


# ── Championnats nationaux (liste fixe, hors /ligues/) ────────────────────────
# NF1-3, NFU18 Elite A/B, NFU15 Elite, TPEF, NM1-3, NMU18/U15 Elite
# (championnats FFBB > Nationaux > Championnats, cf. competitions.ffbb.com)
NATIONAL_COMPETITIONS = [
    "nf1", "nf2", "nf3", "nfu18-elite-a", "nfu18-elite-b", "nfu15-elite", "tpef",
    "nm1", "nm2", "nm3", "nmu18-elite", "nmu15-elite",
]


async def discover_national_competitions() -> list[str]:
    """Résout les URLs ?phase=XXXX des championnats nationaux (liste fixe)."""
    print(f"\n  Résolution de {len(NATIONAL_COMPETITIONS)} championnat(s) national(aux)…")
    sem = asyncio.Semaphore(4)

    async def resolve_safe(slug: str) -> str | None:
        async with sem:
            return await _resolve_phase_url(f"/competitions/{slug}")

    results = await asyncio.gather(*(resolve_safe(s) for s in NATIONAL_COMPETITIONS))
    urls = [r for r in results if r]
    print(f"  → {len(urls)} championnat(s) national(aux) résolu(s)")
    return urls


# ── Test CLI ──────────────────────────────────────────────────────────────────

async def _test():
    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://competitions.ffbb.com/ligues/ges/competitions/pnm"
        "?phase=200000002897412&poule=200000003054918"
    )
    print(f"Scraping : {url}")
    t0 = datetime.now()
    matches, meta = await scrape_competition(url, enrich_details=False)
    dt = (datetime.now() - t0).total_seconds()

    print(f"\n✅ {len(matches)} matchs en {dt:.1f}s")
    print(f"Méta : {meta}")
    for m in matches[:3]:
        print(f"\n  {m['equipe1']} vs {m['equipe2']}")
        print(f"  {m['date']} {m['heure']}  score={m['score'] or 'N/A'}")
        print(f"  Salle: {m['salle']} — {m['adresse']}")
        print(f"  Waze: {m['waze']}")

if __name__ == "__main__":
    asyncio.run(_test())

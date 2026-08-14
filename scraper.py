"""
FFBB Scraper
────────────
Scrape une page de compétition FFBB et retourne les matchs enrichis
(salle, adresse, lien Waze, arbitres, lien FFBB).

Usage :
    python scraper.py
    python scraper.py <URL_COMPETITION>
"""
import asyncio
import json
import re
import sys
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# ── URLs de test ───────────────────────────────────────────────────────────────
TEST_URL = (
    "https://competitions.ffbb.com/ligues/ara/competitions/pnf"
    "?phase=200000002897651&poule=200000003055506&journee=1"
)
CLUB_URL = "https://competitions.ffbb.com/ligues/ges/comites/0057/clubs/ges0057030"
BASE_URL = "https://competitions.ffbb.com"

MOIS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}


# ── Playwright helpers ────────────────────────────────────────────────────────
async def fetch_html(url: str, wait_for: str | None = None) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        if wait_for:
            try:
                await page.wait_for_selector(wait_for, timeout=8_000)
            except Exception:
                pass
        html = await page.content()
        await browser.close()
    return html


# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_date_heure(texte: str) -> tuple[str | None, str | None]:
    m = re.search(
        r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août"
        r"|septembre|octobre|novembre|décembre)\s+(202\d)",
        texte, re.IGNORECASE
    )
    # Format court : "19 sept. 15h30"
    if not m:
        m = re.search(
            r"(\d{1,2})\s+(janv?|févr?|mars|avr?|mai|juin|juill?|août"
            r"|sept?|oct?|nov?|déc?)\.?\s+(202\d)",
            texte, re.IGNORECASE
        )
    h = re.search(r"(\d{1,2})[h:](\d{2})", texte)
    if not m:
        return None, None
    jour, mois_str, annee = m.group(1), m.group(2).lower().rstrip("."), m.group(3)
    mois = MOIS.get(mois_str) or next(
        (v for k, v in MOIS.items() if k.startswith(mois_str[:4])), None
    )
    date = f"{annee}-{mois:02d}-{int(jour):02d}" if mois else None
    heure = f"{int(h.group(1)):02d}:{h.group(2)}" if h else None
    return date, heure


def waze_link(adresse: str) -> str:
    encoded = adresse.replace(" ", "+")
    return f"https://waze.com/ul?q={encoded}"


# ── Scrape page détail d'un match ─────────────────────────────────────────────
async def scrape_match_detail(match_url: str) -> dict:
    """Retourne salle, adresse, lien maps, arbitres depuis la page d'un match."""
    try:
        html = await fetch_html(match_url)
    except Exception as e:
        print(f"    ⚠️  Impossible de charger {match_url} : {e}")
        return {}

    soup = BeautifulSoup(html, "html.parser")
    detail = {"match_url": match_url}

    # ── Salle & Adresse ────────────────────────────────────────────────────────
    # Structure page FFBB :  label "Nom" → valeur nom salle
    #                         label "Adresse" → valeur adresse
    # On cherche la section "Salle" puis les labels "Nom" et "Adresse" à l'intérieur.

    def label_value(label_re) -> str | None:
        """Retourne le texte du premier élément frère/enfant après un label."""
        for el in soup.find_all(string=label_re):
            parent = el.parent
            # Essai 1 : sibling direct du parent
            sib = parent.find_next_sibling()
            if sib:
                txt = sib.get_text(strip=True)
                if txt:
                    return txt
            # Essai 2 : sibling du grand-parent
            gp = parent.parent
            if gp:
                sib2 = gp.find_next_sibling()
                if sib2:
                    txt = sib2.get_text(strip=True)
                    if txt:
                        return txt
        return None

    # Nom de la salle — label exact "Nom" dans la section Salle
    # On cherche d'abord dans le contexte de la section "Salle"
    salle = None
    for salle_heading in soup.find_all(string=re.compile(r"^\s*Salle\s*$", re.I)):
        container = salle_heading.find_parent()
        if not container:
            continue
        for nom_el in container.find_all(string=re.compile(r"^\s*Nom\s*$")):
            nom_parent = nom_el.parent
            sib = nom_parent.find_next_sibling()
            if sib:
                txt = sib.get_text(strip=True)
                if txt and len(txt) > 3:
                    salle = txt
                    break
            gp = nom_parent.parent
            if gp and not salle:
                sib2 = gp.find_next_sibling()
                if sib2:
                    txt = sib2.get_text(strip=True)
                    if txt and len(txt) > 3:
                        salle = txt
                        break
        if salle:
            break

    # Fallback : n'importe quel label "Nom" sur la page
    if not salle:
        salle = label_value(re.compile(r"^\s*Nom\s*$"))

    if salle:
        detail["salle"] = salle

    # Adresse
    adresse = None
    for el in soup.find_all(string=re.compile(r"^\s*Adresse\s*$", re.I)):
        parent = el.parent
        sib = parent.find_next_sibling()
        if sib:
            # L'adresse peut contenir un lien Maps — on prend le texte brut
            txt = sib.get_text(strip=True)
            if re.search(r"\d{5}", txt):
                adresse = txt
                break
        gp = parent.parent
        if gp and not adresse:
            sib2 = gp.find_next_sibling()
            if sib2:
                txt = sib2.get_text(strip=True)
                if re.search(r"\d{5}", txt):
                    adresse = txt
                    break

    if adresse:
        detail["adresse"] = adresse
        detail["waze"] = waze_link(adresse)

    # Lien Google Maps
    maps_link = soup.find("a", href=re.compile(r"maps\.google|waze", re.I))
    if maps_link:
        detail["maps_url"] = maps_link["href"]
        if "adresse" not in detail:
            m = re.search(r"\?q=([^&]+)", maps_link["href"])
            if m:
                detail["adresse"] = m.group(1).replace("+", " ").strip()
                detail["waze"] = waze_link(detail["adresse"])

    # Arbitres
    # Mots-clés qui indiquent que le texte N'est PAS un nom d'arbitre
    FAUX_POSITIFS = [
        "arbitre", "désign", "ffbb", "©", "newsletter", "cookie",
        "connexion", "inscription", "contact", "mentions", "politique",
        "facebook", "twitter", "instagram", "youtube", "réseaux",
        "boutique", "billetterie", "partenaire", "sponsor", "ligue",
        "fédération", "saison", "résultat", "classement", "calendrier",
    ]
    arbitres = []
    for el in soup.find_all(string=re.compile(r"arbitre", re.I)):
        parent = el.parent
        for sib in parent.find_next_siblings():
            txt = sib.get_text(strip=True)
            if not txt or len(txt) > 60:
                continue
            if any(kw in txt.lower() for kw in FAUX_POSITIFS):
                continue
            # Exclure tout texte qui ressemble à du JavaScript
            if re.search(r"[(){}\[\];]|__next|\.push|null\b|undefined\b|function\b", txt):
                continue
            # Un nom d'arbitre : lettres uniquement, 2 mots max (PRENOM NOM)
            if re.search(r"[A-Za-zÀ-ÿ]{2,}", txt) and len(txt.split()) <= 5:
                arbitres.append(txt)
            if len(arbitres) >= 3:
                break
    if arbitres:
        detail["arbitres"] = arbitres

    return detail


# ── Extraction des matchs depuis la page calendrier ───────────────────────────
def extract_matches(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    results = []

    for div in soup.find_all("div"):
        direct_links = [
            a for a in div.find_all("a", title=True, recursive=False)
            if a.get("title", "").strip()
        ]
        if len(direct_links) != 2:
            continue

        equipe1 = direct_links[0]["title"].strip()
        equipe2 = direct_links[1]["title"].strip()

        # Remonter pour trouver date/heure
        date, heure = None, None
        parent = div
        for _ in range(15):
            parent = parent.parent
            if parent is None:
                break
            txt = parent.get_text(" ", strip=True)
            date, heure = parse_date_heure(txt)
            if date:
                break

        # Lien vers la page détail du match
        match_href = None
        for a in div.find_all("a", href=True):
            if "/match/" in a["href"]:
                match_href = a["href"]
                break
        # Chercher aussi dans les parents proches
        if not match_href:
            p = div
            for _ in range(5):
                p = p.parent
                if p is None:
                    break
                for a in p.find_all("a", href=True, recursive=False):
                    if "/match/" in a["href"]:
                        match_href = a["href"]
                        break
                if match_href:
                    break

        match_url = None
        if match_href:
            match_url = match_href if match_href.startswith("http") else BASE_URL + match_href

        # Score
        texte_bloc = div.get_text(" ", strip=True)
        scores = re.findall(r"\b(\d{1,3})\s+(\d{1,3})\b", texte_bloc)
        score = None
        for s1, s2 in scores:
            if int(s1) <= 200 and int(s2) <= 200:
                score = f"{s1}-{s2}"
                break

        key = (date, heure, equipe1, equipe2)
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "date":      date,
            "heure":     heure,
            "equipe1":   equipe1,
            "equipe2":   equipe2,
            "score":     score,
            "match_url": match_url,
        })

    return results


# ── Métadonnées de la compétition ─────────────────────────────────────────────
def extract_competition_meta(html: str, url: str) -> dict:
    """Extrait slug, région, poule depuis la page."""
    soup = BeautifulSoup(html, "html.parser")

    # Slug depuis l'URL : /competitions/pnf → PNF
    slug_m = re.search(r"/competitions/([^/?]+)", url)
    slug = slug_m.group(1).upper() if slug_m else "COMPETITION"

    # Région depuis l'URL (code ligue court : ARA, GES, IDF…)
    ligue_m = re.search(r"/ligues/([^/]+)", url)
    region = ligue_m.group(1).upper() if ligue_m else None

    # Paramètres URL
    phase_m = re.search(r"phase=(\d+)", url)
    poule_m = re.search(r"poule=(\d+)", url)

    # Poule : on trouve l'option du <select aria-label="Poules"> dont la value
    # correspond à l'ID de poule dans l'URL — évite de prendre "Poule A" par défaut
    poule = None
    select = soup.find("select", attrs={"aria-label": re.compile(r"(?i)^poules?$")})
    if select and poule_m:
        opt = select.find("option", value=poule_m.group(1))
        if opt:
            pm = re.search(r"(Poule\s*[A-Z0-9]+)", opt.get_text(strip=True), re.I)
            if pm:
                poule = pm.group(1).strip()
    # Fallback : première occurrence dans la page
    if not poule:
        for el in soup.find_all(string=re.compile(r"poule\s*[A-Z]", re.I)):
            pm = re.search(r"(Poule\s*[A-Z0-9]+)", el.strip(), re.I)
            if pm:
                poule = pm.group(1).strip()
                break

    return {
        "slug":     slug,
        "region":   region or "FR",
        "poule":    poule,
        "phase_id": phase_m.group(1) if phase_m else None,
        "poule_id": poule_m.group(1) if poule_m else None,
        "key":      f"{phase_m.group(1)}_{poule_m.group(1)}" if phase_m and poule_m else url,
    }


def build_calendar_name(meta: dict) -> str:
    """Génère le nom du calendrier : PNF – ARA – Poule A"""
    parts = [meta["slug"], meta["region"]]
    if meta.get("poule"):
        parts.append(meta["poule"])
    return " – ".join(parts)


async def find_all_poule_urls(url: str) -> list[str]:
    """
    Détecte toutes les URLs de poules via le <select aria-label="Poules">
    rendu par Playwright.
    """
    base = re.sub(r"[&?]journee=\d+", "", url)
    base = re.sub(r"[&?]poule=\d+", "", base).rstrip("?&")
    sep = "&" if "?" in base else "?"

    html = await fetch_html(url, wait_for="text=CALENDRIER")
    soup = BeautifulSoup(html, "html.parser")

    # Le select des poules a aria-label="Poules"
    select = soup.find("select", attrs={"aria-label": re.compile(r"(?i)^poules?$")})
    if select:
        poules = [
            opt["value"]
            for opt in select.find_all("option")
            if opt.get("value", "").strip().isdigit()
        ]
        if poules:
            print(f"  → {len(poules)} poule(s) trouvée(s) dans le <select>")
            return [f"{base}{sep}poule={p}" for p in poules]

    # Fallback : poule courante uniquement
    current_m = re.search(r"poule=(\d+)", url)
    if current_m:
        return [f"{base}{sep}poule={current_m.group(1)}"]
    return [url]


def find_poule_urls(html: str, base_url: str) -> list[str]:
    """Version synchrone basique (scan HTML). Préférer find_all_poule_urls."""
    base = re.sub(r"[&?]journee=\d+", "", base_url)
    base = re.sub(r"[&?]poule=\d+", "", base).rstrip("?&")
    sep = "&" if "?" in base else "?"
    poules = set(re.findall(r"poule=(\d+)", html))
    if not poules:
        return [base_url]
    return [f"{base}{sep}poule={p}" for p in sorted(poules)]


def find_journee_urls(html: str, base_url: str) -> list[str]:
    """Retourne les URLs de toutes les journées via le <select aria-label='Journées'>."""
    soup = BeautifulSoup(html, "html.parser")
    base = re.sub(r"[&?]journee=\d+", "", base_url).rstrip("?&")
    sep = "&" if "?" in base else "?"

    # Le select des journées a aria-label="Journées"
    select = soup.find("select", attrs={"aria-label": re.compile(r"(?i)^journ.es?$")})
    if select:
        journees = [
            int(opt["value"])
            for opt in select.find_all("option")
            if opt.get("value", "").strip().isdigit()
        ]
        if journees:
            return [f"{base}{sep}journee={j}" for j in sorted(journees)]

    # Fallback : journee= dans les <a href>
    journees_set = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"journee=(\d+)", a["href"])
        if m:
            journees_set.add(int(m.group(1)))

    if journees_set:
        return [f"{base}{sep}journee={j}" for j in sorted(journees_set)]

    return [base_url]


# ── Pipeline principal ────────────────────────────────────────────────────────
async def scrape_competition(url: str, enrich_details: bool = True) -> tuple[list[dict], dict]:
    """
    Scrape toutes les journées d'une URL de compétition.
    Retourne (liste_matchs, meta_competition).
    Si enrich_details=True, visite chaque page match pour salle/adresse/arbitres.
    """
    print(f"  Scraping J1 : {url}")
    html = await fetch_html(url, wait_for="text=CALENDRIER")
    meta = extract_competition_meta(html, url)

    journee_urls = find_journee_urls(html, url)
    print(f"  → {len(journee_urls)} journée(s) détectée(s) | "
          f"{meta['slug']} – {meta['region']}"
          + (f" – {meta['poule']}" if meta.get("poule") else ""))

    # Collecte tous les matchs, toutes journées confondues
    seen: set[tuple] = set()
    all_matches: list[dict] = []

    def add_matches(new_matches):
        for m in new_matches:
            key = (m["date"], m.get("heure"), m["equipe1"], m["equipe2"])
            if key not in seen:
                seen.add(key)
                all_matches.append(m)

    add_matches(extract_matches(html))

    for jurl in journee_urls:
        if jurl == url:
            continue  # déjà scrapée
        print(f"  Scraping : {jurl}")
        html_j = await fetch_html(jurl, wait_for="text=CALENDRIER")
        add_matches(extract_matches(html_j))

    print(f"  → {len(all_matches)} match(s) au total")

    if enrich_details:
        for i, match in enumerate(all_matches):
            if match.get("match_url"):
                print(f"    [{i+1}/{len(all_matches)}] Détails : {match['match_url']}")
                detail = await scrape_match_detail(match["match_url"])
                match.update(detail)
            else:
                print(f"    [{i+1}/{len(all_matches)}] Pas de lien match trouvé")

    return all_matches, meta


async def discover_competitions(region_url: str, exclude: list[str] | None = None) -> list[str]:
    """
    Découvre toutes les URLs de compétitions depuis la page d'une ligue FFBB.

    Stratégie :
    1. Rend la page région avec Playwright, attend que des liens /competitions/ apparaissent.
    2. Collecte les slugs uniques (ex: /ligues/ges/competitions/pnm).
    3. Pour chaque slug sans phase=, navigue vers la page pour obtenir l'URL finale
       (après redirect vers la saison en cours avec phase=).
    4. Filtre les exclusions (coupe, plateau, cup…).

    Retourne une liste d'URLs avec phase= (sans journee=/poule=).
    """
    exclude = [kw.lower() for kw in (exclude or ["coupe", "plateau", "cup"])]
    print(f"\n  Découverte des compétitions : {region_url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── Étape 1 : page région ─────────────────────────────────────────────
        page = await browser.new_page()
        await page.goto(region_url, wait_until="networkidle", timeout=30_000)
        # Attendre que des liens de compétitions apparaissent
        try:
            await page.wait_for_selector("a[href*='/competitions/']", timeout=10_000)
        except Exception:
            pass
        html = await page.content()
        await page.close()

        soup  = BeautifulSoup(html, "html.parser")
        slugs = {}   # slug_path → label

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/competitions/" not in href or "/match/" in href:
                continue
            # Normaliser : garder uniquement /ligues/XXX/competitions/YYY
            m = re.search(r"(/ligues/[^/]+/competitions/[^/?]+)", href)
            if not m:
                continue
            slug_path = m.group(1)
            if slug_path not in slugs:
                slugs[slug_path] = a.get_text(strip=True)

        print(f"  {len(slugs)} slug(s) de compétition trouvé(s)")

        # ── Étape 2 : filtrage + résolution des URLs avec phase= ──────────────
        result = []
        for slug_path, label in slugs.items():
            # Filtre exclusion sur le slug et le label
            low_slug  = slug_path.lower()
            low_label = label.lower()
            if any(kw in low_slug or kw in low_label for kw in exclude):
                print(f"  ⏭ Exclu : {label}")
                continue

            comp_url = BASE_URL + slug_path

            # Si l'URL n'a pas encore phase=, on navigue pour obtenir l'URL finale
            page2 = await browser.new_page()
            try:
                await page2.goto(comp_url, wait_until="networkidle", timeout=30_000)
                final_url = page2.url
            except Exception as e:
                print(f"  ⚠️  Impossible de charger {comp_url} : {e}")
                await page2.close()
                continue
            await page2.close()

            # Nettoyer journee= et poule=
            clean = re.sub(r"[&?]journee=\d+", "", final_url)
            clean = re.sub(r"[&?]poule=\d+",   "", clean).rstrip("?&")

            if "phase=" not in clean:
                print(f"  ⚠️  Pas de phase= pour {label}, ignoré")
                continue

            print(f"  ✓ {label}")
            result.append(clean)

        await browser.close()

    print(f"\n  → {len(result)} compétition(s) retenue(s)")
    return result


async def scrape_club(club_url: str) -> list[str]:
    """Retourne les URLs de compétition des équipes engagées d'un club."""
    print(f"  Club : {club_url}")
    html = await fetch_html(club_url)
    soup = BeautifulSoup(html, "html.parser")
    comp_urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/competitions/" in href and "/equipes/" in href:
            full = href if href.startswith("http") else BASE_URL + href
            if full not in comp_urls:
                comp_urls.append(full)
    print(f"  → {len(comp_urls)} équipe(s) engagée(s)")
    return comp_urls


# ── Point d'entrée standalone ─────────────────────────────────────────────────
async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else TEST_URL
    matches, meta = await scrape_competition(url, enrich_details=True)

    print(f"\n{'─'*60}")
    print(f"RÉSULTATS — {build_calendar_name(meta)}")
    print(f"{'─'*60}")
    print(json.dumps(matches, indent=2, ensure_ascii=False))

    with open("matches.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "matches": matches}, f, indent=2, ensure_ascii=False)
    print("\n  → Sauvegardé dans matches.json")


if __name__ == "__main__":
    asyncio.run(main())

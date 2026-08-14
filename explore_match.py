"""
Exploration de la page détail d'un match FFBB.
Lance : python explore_match.py
"""
import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

MATCH_URL = "https://competitions.ffbb.com/ligues/ara/competitions/pnf/match/200000014583110"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(MATCH_URL, wait_until="networkidle", timeout=30_000)
        html = await page.content()
        await browser.close()

    soup = BeautifulSoup(html, "html.parser")

    print("=" * 60)
    print("LIENS (waze / maps / google)")
    print("=" * 60)
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if any(x in h.lower() for x in ["waze", "maps.google", "goo.gl", "map"]):
            print(f"  {a.get_text(strip=True)[:50]:50s} → {h}")

    print()
    print("=" * 60)
    print("MOTS CLÉS : salle / gymnase / adresse / arbitre")
    print("=" * 60)
    seen = set()
    for kw in ["salle", "gymnase", "arbitre", "adresse", "rue", "avenue", "allée", "stade"]:
        for el in soup.find_all(string=lambda t, k=kw: t and k.lower() in t.lower()):
            txt = el.strip()
            if 3 < len(txt) < 300 and txt not in seen:
                seen.add(txt)
                print(f"  [{kw}] {txt}")

    print()
    print("=" * 60)
    print("TOUS LES TEXTES (5–200 cars)")
    print("=" * 60)
    seen2 = set()
    for el in soup.find_all(True):
        if el.find_all(recursive=False):
            continue
        txt = el.get_text(strip=True)
        if 5 < len(txt) < 200 and txt not in seen2:
            seen2.add(txt)
            print(f"  [{el.name}] {txt}")


asyncio.run(main())

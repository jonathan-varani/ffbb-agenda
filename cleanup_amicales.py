"""
Nettoyage ponctuel : supprime les compétitions "Amicale" déjà scrapées
(fichiers .ics + entrées manifest) suite à l'ajout du filtre d'exclusion
dans discover_competitions() (scraper_http.py).

Usage : python cleanup_amicales.py
"""
import json
import os

MANIFEST = "docs/calendars.json"


def is_amicale(entry: dict) -> bool:
    meta = entry.get("meta", {})
    titre = (meta.get("titre") or "").lower()
    slug  = (entry.get("slug") or "").lower()
    return "amicale" in titre or "amicale" in slug or bool(
        __import__("re").match(r"^\d+-ami-", slug)
    )


def main():
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)

    comp_slugs_to_remove = {
        c["slug"] for c in manifest["calendriers"] if is_amicale(c)
    }
    print(f"Compétitions amicales détectées : {sorted(comp_slugs_to_remove)}")

    removed_files = 0

    # ── Fichiers championnat ──────────────────────────────────────────────
    kept_champs = []
    for c in manifest["calendriers"]:
        if c["slug"] in comp_slugs_to_remove:
            path = os.path.join("docs", c["fichier"])
            if os.path.exists(path):
                os.remove(path)
                removed_files += 1
                print(f"  ✗ supprimé : {path}")
        else:
            kept_champs.append(c)
    manifest["calendriers"] = kept_champs

    # ── Fichiers équipes liés à ces compétitions ───────────────────────────
    kept_teams = []
    for e in manifest["equipes"]:
        if e.get("comp_slug") in comp_slugs_to_remove:
            path = os.path.join("docs", e["fichier"])
            if os.path.exists(path):
                os.remove(path)
                removed_files += 1
                print(f"  ✗ supprimé : {path}")
        else:
            kept_teams.append(e)
    manifest["equipes"] = kept_teams

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"\n✅ {removed_files} fichier(s) .ics supprimé(s).")
    print(f"   Manifest mis à jour : {MANIFEST}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Génère index.html depuis calendars.json.
Usage : python generate_frontend.py
"""
import json
import re
from urllib.parse import quote
from pathlib import Path

# ── Lecture des données ───────────────────────────────────────────────────────
with open("calendars.json", encoding="utf-8") as f:
    db = json.load(f)

# ── Parsing ───────────────────────────────────────────────────────────────────
championships: dict[str, dict] = {}
teams: list[dict] = []

# Pass 1 : championnats
for key, val in db.items():
    if key.startswith("eq_"):
        continue
    nom = val["nom"]
    # "PNF – ARA – Poule A"  ou  "PNF – ARA"
    parts = [p.strip() for p in nom.split("–")]
    championships[key] = {
        "slug":   parts[0] if len(parts) > 0 else "",
        "region": parts[1] if len(parts) > 1 else "",
        "poule":  parts[2] if len(parts) > 2 else "",
        "cal_id": val["calendar_id"],
        "nom":    nom,
    }

# Pass 2 : équipes
for key, val in db.items():
    if not key.startswith("eq_"):
        continue
    nom = val["nom"]
    # "TEAM NAME  [SLUG – REGION]"
    m = re.match(r"^(.+?)\s{2,}\[([^–\]]+?)\s*–\s*([^\]]+)\]", nom)
    if m:
        team_name = m.group(1).strip()
        slug      = m.group(2).strip()
        region    = m.group(3).strip()
    else:
        team_name = nom
        slug = region = ""

    # Récupère la poule depuis le championnat correspondant
    key_parts = key.split("_")  # eq_{phase_id}_{poule_id}_{...}
    champ_key = f"{key_parts[1]}_{key_parts[2]}" if len(key_parts) >= 3 else ""
    poule = championships.get(champ_key, {}).get("poule", "")

    cal_id = val["calendar_id"]
    teams.append({
        "team":   team_name,
        "slug":   slug,
        "region": region,
        "poule":  poule,
        "cal_id": cal_id,
        "ical":   f"https://calendar.google.com/calendar/ical/{quote(cal_id)}/public/basic.ics",
        "webcal": f"webcal://calendar.google.com/calendar/ical/{quote(cal_id)}/public/basic.ics",
        "gcal":   f"https://calendar.google.com/calendar/r?cid={quote(cal_id)}",
    })

teams.sort(key=lambda t: (t["region"], t["slug"], t["poule"], t["team"]))

# ── Construction de l'arborescence région > championnat > poule > équipes ─────
tree: dict[str, dict[str, dict[str, list]]] = {}
for t in teams:
    r = t["region"] or "Autre"
    s = t["slug"]   or "Autre"
    p = t["poule"]  or ""
    tree.setdefault(r, {}).setdefault(s, {}).setdefault(p, []).append(t)

# Données JSON embarquées dans le HTML
data_json = json.dumps({"tree": tree, "teams": teams}, ensure_ascii=False)

# ── Template HTML ─────────────────────────────────────────────────────────────
html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Agendas FFBB — Abonnement calendrier</title>
  <style>
    :root {{
      --orange: #E84E0F;
      --navy:   #1B2A4A;
      --light:  #F5F6FA;
      --gray:   #6B7280;
      --border: #E5E7EB;
      --radius: 12px;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--light);
      color: var(--navy);
      min-height: 100vh;
    }}

    /* ── Header ── */
    header {{
      background: var(--navy);
      color: #fff;
      padding: 18px 20px 16px;
      position: sticky;
      top: 0;
      z-index: 100;
      box-shadow: 0 2px 8px rgba(0,0,0,.25);
    }}
    header h1 {{
      font-size: 1.2rem;
      font-weight: 700;
      letter-spacing: -.3px;
    }}
    header p {{
      font-size: .78rem;
      opacity: .7;
      margin-top: 2px;
    }}
    .header-logo {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .logo-ball {{
      font-size: 1.8rem;
      line-height: 1;
    }}

    /* ── Search ── */
    .search-wrap {{
      padding: 14px 16px 10px;
      background: #fff;
      border-bottom: 1px solid var(--border);
    }}
    .search-wrap input {{
      width: 100%;
      padding: 10px 14px;
      border: 1.5px solid var(--border);
      border-radius: 30px;
      font-size: .95rem;
      outline: none;
      background: var(--light);
      transition: border-color .2s;
    }}
    .search-wrap input:focus {{
      border-color: var(--orange);
      background: #fff;
    }}

    /* ── Filtres région ── */
    .region-tabs {{
      display: flex;
      gap: 8px;
      padding: 12px 16px;
      overflow-x: auto;
      scrollbar-width: none;
      background: #fff;
      border-bottom: 1px solid var(--border);
    }}
    .region-tabs::-webkit-scrollbar {{ display: none; }}
    .region-tab {{
      flex-shrink: 0;
      padding: 6px 16px;
      border-radius: 20px;
      border: 1.5px solid var(--border);
      background: #fff;
      color: var(--navy);
      font-size: .85rem;
      font-weight: 600;
      cursor: pointer;
      transition: all .15s;
    }}
    .region-tab.active {{
      background: var(--orange);
      border-color: var(--orange);
      color: #fff;
    }}

    /* ── Contenu ── */
    .content {{
      padding: 16px;
      max-width: 680px;
      margin: 0 auto;
    }}

    /* ── Championnat ── */
    .champ-block {{
      margin-bottom: 20px;
    }}
    .champ-title {{
      font-size: .75rem;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--gray);
      margin-bottom: 8px;
      padding-left: 4px;
    }}

    /* ── Poule ── */
    .poule-block {{
      background: #fff;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      overflow: hidden;
      margin-bottom: 12px;
    }}
    .poule-header {{
      padding: 12px 16px;
      font-size: .85rem;
      font-weight: 700;
      color: var(--navy);
      background: #F8F9FC;
      border-bottom: 1px solid var(--border);
      letter-spacing: .02em;
    }}
    .poule-header:empty {{ display: none; }}

    /* ── Équipe ── */
    .team-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 13px 16px;
      border-bottom: 1px solid var(--border);
      gap: 12px;
      transition: background .1s;
    }}
    .team-row:last-child {{ border-bottom: none; }}
    .team-row:active {{ background: #F8F9FC; }}
    .team-name {{
      font-size: .9rem;
      font-weight: 600;
      flex: 1;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .btn-subscribe {{
      flex-shrink: 0;
      padding: 8px 16px;
      background: var(--orange);
      color: #fff;
      border: none;
      border-radius: 30px;
      font-size: .82rem;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
      white-space: nowrap;
      transition: opacity .15s;
    }}
    .btn-subscribe:active {{ opacity: .8; }}

    /* ── Vide ── */
    .empty {{
      text-align: center;
      padding: 48px 24px;
      color: var(--gray);
      font-size: .95rem;
    }}
    .empty .icon {{ font-size: 2.5rem; margin-bottom: 12px; }}

    /* ── Modale iOS/Android ── */
    .modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.5);
      z-index: 200;
      align-items: flex-end;
      justify-content: center;
    }}
    .modal-overlay.open {{ display: flex; }}
    .modal {{
      position: relative;
      background: #fff;
      border-radius: 20px 20px 0 0;
      padding: 24px 20px 36px;
      width: 100%;
      max-width: 500px;
      animation: slideUp .2s ease-out;
    }}
    .modal-x {{
      position: absolute;
      top: 14px;
      right: 14px;
      width: 32px;
      height: 32px;
      border: none;
      border-radius: 50%;
      background: var(--light);
      color: var(--navy);
      font-size: 1.1rem;
      line-height: 1;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .modal-x:active {{ background: var(--border); }}
    @keyframes slideUp {{
      from {{ transform: translateY(100%); }}
      to   {{ transform: translateY(0); }}
    }}
    .modal h3 {{
      font-size: 1rem;
      font-weight: 700;
      margin-bottom: 4px;
      padding-right: 40px;
    }}
    .modal .team-subtitle {{
      font-size: .82rem;
      color: var(--gray);
      margin-bottom: 20px;
    }}
    .modal-btn {{
      display: flex;
      align-items: center;
      gap: 12px;
      width: 100%;
      padding: 14px 16px;
      border-radius: 12px;
      border: 1.5px solid var(--border);
      background: #fff;
      margin-bottom: 10px;
      cursor: pointer;
      text-decoration: none;
      color: var(--navy);
      font-size: .9rem;
      font-weight: 600;
      transition: background .15s;
    }}
    .modal-btn:active {{ background: var(--light); }}
    .modal-btn .icon {{ font-size: 1.4rem; }}
    .modal-btn.primary {{
      background: var(--orange);
      border-color: var(--orange);
      color: #fff;
    }}
    .modal-close {{
      display: block;
      width: 100%;
      margin-top: 6px;
      padding: 12px;
      border: 1.5px solid var(--border);
      border-radius: 12px;
      background: #fff;
      font-size: .9rem;
      font-weight: 600;
      color: var(--navy);
      cursor: pointer;
    }}
    .modal-close:active {{ background: var(--light); }}

    /* ── Footer ── */
    footer {{
      text-align: center;
      padding: 24px 16px;
      font-size: .75rem;
      color: var(--gray);
    }}
  </style>
</head>
<body>

<header>
  <div class="header-logo">
    <span class="logo-ball">🏀</span>
    <div>
      <h1>Agendas FFBB</h1>
      <p>Abonnez-vous au calendrier de votre équipe</p>
    </div>
  </div>
</header>

<div class="search-wrap">
  <input type="search" id="search" placeholder="Rechercher une équipe…" autocomplete="off">
</div>

<div class="region-tabs" id="region-tabs">
  <button class="region-tab active" data-region="">Toutes</button>
</div>

<div class="content" id="content"></div>

<div class="modal-overlay" id="modal" onclick="closeModal(event)">
  <div class="modal">
    <button class="modal-x" aria-label="Fermer" onclick="document.getElementById('modal').classList.remove('open')">✕</button>
    <h3 id="modal-team"></h3>
    <div class="team-subtitle" id="modal-subtitle"></div>
    <a id="modal-primary" class="modal-btn primary" href="#">
      <span class="icon" id="modal-primary-icon"></span>
      <span id="modal-primary-label"></span>
    </a>
    <a id="modal-secondary" class="modal-btn" href="#" target="_blank">
      <span class="icon">🌐</span>
      <span>Ouvrir dans Google Agenda</span>
    </a>
    <a id="modal-ical" class="modal-btn" href="#" download>
      <span class="icon">📁</span>
      <span>Télécharger le fichier .ics</span>
    </a>
    <button class="modal-close" onclick="document.getElementById('modal').classList.remove('open')">
      Fermer
    </button>
  </div>
</div>

<footer>
  Données issues de <a href="https://competitions.ffbb.com" target="_blank">competitions.ffbb.com</a><br>
  Mise à jour automatique · Projet non officiel
</footer>

<script>
const DATA = {data_json};

const isIOS     = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
const isAndroid = /Android/.test(navigator.userAgent);

let currentRegion = "";
let currentSearch = "";

// ── Tabs région ──────────────────────────────────────────────────────────────
const regions = Object.keys(DATA.tree).sort();
const tabsEl  = document.getElementById("region-tabs");
regions.forEach(r => {{
  const btn = document.createElement("button");
  btn.className   = "region-tab";
  btn.dataset.region = r;
  btn.textContent = r;
  btn.onclick = () => setRegion(r);
  tabsEl.appendChild(btn);
}});

function setRegion(r) {{
  currentRegion = r;
  document.querySelectorAll(".region-tab").forEach(b =>
    b.classList.toggle("active", b.dataset.region === r)
  );
  render();
}}

// ── Recherche ────────────────────────────────────────────────────────────────
document.getElementById("search").addEventListener("input", e => {{
  currentSearch = e.target.value.trim().toLowerCase();
  render();
}});

// ── Rendu ────────────────────────────────────────────────────────────────────
function render() {{
  const content = document.getElementById("content");
  const tree    = DATA.tree;
  let html      = "";
  let total     = 0;

  const regionsToShow = currentRegion ? [currentRegion] : regions;

  regionsToShow.forEach(region => {{
    const champs = tree[region];
    if (!champs) return;

    Object.entries(champs).sort().forEach(([slug, poules]) => {{
      let champHtml = "";

      Object.entries(poules).sort().forEach(([poule, teamList]) => {{
        const filtered = teamList.filter(t =>
          !currentSearch || t.team.toLowerCase().includes(currentSearch)
        );
        if (!filtered.length) return;
        total += filtered.length;

        const pouleTitle = poule
          ? `<div class="poule-header">${{slug}} – ${{region}} – ${{poule}}</div>`
          : `<div class="poule-header">${{slug}} – ${{region}}</div>`;

        const rows = filtered.map(t => `
          <div class="team-row">
            <span class="team-name">${{t.team}}</span>
            <button class="btn-subscribe"
              data-team="${{esc(t.team)}}"
              data-slug="${{esc(t.slug)}}"
              data-poule="${{esc(t.poule)}}"
              data-webcal="${{esc(t.webcal)}}"
              data-gcal="${{esc(t.gcal)}}"
              data-ical="${{esc(t.ical)}}"
              onclick="openModal(this)">
              📅 S'abonner
            </button>
          </div>`).join("");

        champHtml += `<div class="poule-block">${{pouleTitle}}${{rows}}</div>`;
      }});

      if (champHtml) {{
        const label = currentRegion ? slug : `${{slug}} – ${{region}}`;
        html += `<div class="champ-block">
          <div class="champ-title">${{label}}</div>
          ${{champHtml}}
        </div>`;
      }}
    }});
  }});

  if (!total) {{
    html = `<div class="empty">
      <div class="icon">🔍</div>
      Aucune équipe trouvée
    </div>`;
  }}

  content.innerHTML = html;
}}

function esc(s) {{
  return (s || "").replace(/"/g, "&quot;");
}}

// ── Modale abonnement ────────────────────────────────────────────────────────
function openModal(btn) {{
  const team   = btn.dataset.team;
  const slug   = btn.dataset.slug;
  const poule  = btn.dataset.poule;
  const webcal = btn.dataset.webcal;
  const gcal   = btn.dataset.gcal;
  const ical   = btn.dataset.ical;

  document.getElementById("modal-team").textContent    = team;
  document.getElementById("modal-subtitle").textContent = poule ? `${{slug}} – ${{poule}}` : slug;
  document.getElementById("modal-ical").href           = ical;
  document.getElementById("modal-secondary").href      = gcal;

  const primary      = document.getElementById("modal-primary");
  const primaryIcon  = document.getElementById("modal-primary-icon");
  const primaryLabel = document.getElementById("modal-primary-label");

  if (isIOS) {{
    primary.href        = webcal;
    primaryIcon.textContent  = "";
    primaryLabel.textContent = "Ajouter à Calendrier iPhone";
  }} else if (isAndroid) {{
    primary.href        = gcal;
    primaryIcon.textContent  = "🤖";
    primaryLabel.textContent = "Ajouter à Google Agenda";
  }} else {{
    primary.href        = webcal;
    primaryIcon.textContent  = "🖥️";
    primaryLabel.textContent = "Abonnement via webcal://";
  }}

  document.getElementById("modal").classList.add("open");
}}

function closeModal(e) {{
  if (e.target === document.getElementById("modal")) {{
    document.getElementById("modal").classList.remove("open");
  }}
}}

// ── Init ─────────────────────────────────────────────────────────────────────
render();
</script>
</body>
</html>"""

# ── Écriture ──────────────────────────────────────────────────────────────────
out = Path("index.html")
out.write_text(html, encoding="utf-8")
print(f"✓ {out} généré ({len(teams)} équipes, {len(championships)} championnats)")

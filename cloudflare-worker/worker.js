/**
 * FFBB Agenda — Cloudflare Worker
 * Test VARAI
 *
 * POST /subscribe  → enregistre dans NocoDB + envoie email avec lien tokenisé
 * GET  /sub        → valide token, marque utilisé, redirige vers webcal://
 *
 * Variables d'environnement (secrets) à configurer dans Cloudflare :
 *   NOCODB_TOKEN   — clé API NocoDB
 *   BREVO_KEY      — clé API Brevo
 *   WORKER_URL     — URL publique de ce worker (ex: https://ffbb.mon-user.workers.dev)
 */

const NOCODB_API   = "https://app.nocodb.com";
const NOCODB_BASE  = "poq54dd1rjvxuki";   // v1 uniquement
const NOCODB_TABLE = "myrqkg2uylp17q9";   // table ID (utilisé par v1 et v2)
const NOCODB_TABLE_CONTACTS = "m135lw76cfsqy0a"; // table "Contacts Joueurs"
// Table "Calendriers Google" : fichier | equipe | google_calendar_id | created_at
// ⚠️ À remplacer par l'ID réel de la table une fois créée dans NocoDB.
const NOCODB_TABLE_GCAL = "mecxgoidr0xyqw5";
const PAGES_BASE   = "https://basket.varai.fr";
const SENDER_EMAIL = "jonathan.varani@varai.fr";
const SENDER_NAME  = "Agendas FFBB";

// ── Helpers ──────────────────────────────────────────────────────────────────

function uuid() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function cors() {
  return {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function icsFullUrl(fichier) {
  return `${PAGES_BASE}/${fichier}`;
}

// GitHub Pages sert les .ics en "text/calendar" sans charset=utf-8, ce qui fait
// que certains clients (Google Agenda Android) mésinterprètent les caractères
// UTF-8 (emoji, accents) du nom d'agenda (X-WR-CALNAME). On proxifie via ce
// worker pour forcer explicitement le charset sur les liens d'abonnement.
function icsProxyUrl(fichier, env) {
  return `${env.WORKER_URL}/ics/${fichier}`;
}

async function handleIcsProxy(path) {
  const fichier = path.replace(/^\/ics\//, "");
  const upstream = await fetch(`${PAGES_BASE}/${fichier}`);
  if (!upstream.ok) {
    return new Response("Not found", { status: upstream.status });
  }
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type":  "text/calendar; charset=utf-8",
      "Cache-Control": "public, max-age=300",
      ...cors(),
    },
  });
}


// ══════════════════════════════════════════════════════════════════════════════
// Google Calendar — création à la demande
// ══════════════════════════════════════════════════════════════════════════════
//
// Pourquoi : sur Android, un abonnement à une URL .ics externe est ajouté au
// compte mais reste invisible tant que l'utilisateur ne l'active pas à la main.
// Un lien "cid=<vrai id de calendrier Google>" s'affiche lui immédiatement.
// On crée donc un vrai calendrier Google, mais UNIQUEMENT au premier abonnement
// d'une équipe (créer les ~1600 calendriers d'avance dépasserait les limites
// opérationnelles de Google sur la création de calendriers).
//
// Secrets Cloudflare nécessaires :
//   GOOGLE_SA_JSON — contenu complet du service_account.json (compte de service)

const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";
const GOOGLE_CAL_API   = "https://www.googleapis.com/calendar/v3";
const GOOGLE_SCOPE     = "https://www.googleapis.com/auth/calendar";

function b64url(bytes) {
  let bin = "";
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (const b of arr) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToDer(pem) {
  const b64 = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s+/g, "");
  const bin = atob(b64);
  const der = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) der[i] = bin.charCodeAt(i);
  return der.buffer;
}

/** Signe un JWT RS256 et l'échange contre un access_token Google. */
async function getGoogleAccessToken(env) {
  const sa = JSON.parse(env.GOOGLE_SA_JSON);
  const now = Math.floor(Date.now() / 1000);

  const header  = { alg: "RS256", typ: "JWT" };
  const payload = {
    iss:   sa.client_email,
    scope: GOOGLE_SCOPE,
    aud:   GOOGLE_TOKEN_URL,
    iat:   now,
    exp:   now + 3600,
  };

  const enc = new TextEncoder();
  const unsigned =
    b64url(enc.encode(JSON.stringify(header))) + "." +
    b64url(enc.encode(JSON.stringify(payload)));

  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToDer(sa.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc.encode(unsigned));
  const jwt = unsigned + "." + b64url(sig);

  const res = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion:  jwt,
    }),
  });
  if (!res.ok) throw new Error("Google OAuth : " + (await res.text()));
  return (await res.json()).access_token;
}

// ── Parsing ICS ───────────────────────────────────────────────────────────────

function icsUnescape(s) {
  return s
    .replace(/\\n/gi, "\n")
    .replace(/\\,/g, ",")
    .replace(/\\;/g, ";")
    .replace(/\\\\/g, "\\");
}

/** Déplie les lignes repliées (RFC 5545) puis extrait les VEVENT. */
function parseIcs(text) {
  const unfolded = text.replace(/\r\n[ \t]/g, "").replace(/\n[ \t]/g, "");
  const lines = unfolded.split(/\r?\n/);

  const events = [];
  let cur = null;
  for (const line of lines) {
    if (line === "BEGIN:VEVENT") { cur = {}; continue; }
    if (line === "END:VEVENT")   { if (cur) events.push(cur); cur = null; continue; }
    if (!cur) continue;

    const idx = line.indexOf(":");
    if (idx < 0) continue;
    const rawKey = line.slice(0, idx);
    const value  = line.slice(idx + 1);
    const key    = rawKey.split(";")[0].toUpperCase();

    if (key === "UID")         cur.uid = value.trim();
    else if (key === "SUMMARY")     cur.summary = icsUnescape(value);
    else if (key === "DESCRIPTION") cur.description = icsUnescape(value);
    else if (key === "LOCATION")    cur.location = icsUnescape(value);
    else if (key === "DTSTART")     cur.start = value.trim();
    else if (key === "DTEND")       cur.end = value.trim();
  }
  return events;
}

/** "20261004T153000" → "2026-10-04T15:30:00" (heure locale Europe/Paris). */
function icsDateToRfc3339(v) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$/.exec(v);
  if (!m) return null;
  return `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`;
}

// ── Mapping équipe → google_calendar_id (NocoDB) ──────────────────────────────

async function findGoogleCalendarId(fichier, env) {
  const url =
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE_GCAL}` +
    `?where=(fichier,eq,${encodeURIComponent(fichier)})&limit=1`;
  const res = await fetch(url, { headers: { "xc-token": env.NOCODB_TOKEN } });
  if (!res.ok) return null;
  const data = await res.json();
  const row  = (data.list ?? data.records ?? [])[0];
  return row ? row.google_calendar_id : null;
}

async function saveGoogleCalendarId(fichier, equipe, calendarId, env) {
  await fetch(
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE_GCAL}`,
    {
      method: "POST",
      headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({
        fichier,
        equipe,
        google_calendar_id: calendarId,
        created_at: new Date().toISOString(),
      }),
    },
  );
}

// ── Création du calendrier + injection des matchs ─────────────────────────────

/**
 * Crée un vrai calendrier Google public pour une équipe et y importe les matchs
 * lus depuis le .ics statique. Retourne l'id du calendrier.
 */
async function createGoogleCalendar(row, env) {
  const token = await getGoogleAccessToken(env);
  const auth  = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };

  const equipe   = row.equipe || "Équipe";
  const compNom  = row.comp_nom ? ` — ${row.comp_nom}` : "";

  // 1. Créer le calendrier
  const createRes = await fetch(`${GOOGLE_CAL_API}/calendars`, {
    method: "POST",
    headers: auth,
    body: JSON.stringify({
      summary:     `🏀 ${equipe}${compNom}`,
      description: `Matchs de ${equipe}. Données FFBB — projet non officiel.`,
      timeZone:    "Europe/Paris",
    }),
  });
  if (!createRes.ok) throw new Error("Création calendrier : " + (await createRes.text()));
  const calendarId = (await createRes.json()).id;

  // 2. Rendre public (lecture pour tout le monde) — indispensable pour cid=
  await fetch(`${GOOGLE_CAL_API}/calendars/${encodeURIComponent(calendarId)}/acl`, {
    method: "POST",
    headers: auth,
    body: JSON.stringify({ role: "reader", scope: { type: "default" } }),
  });

  // 3. Importer les matchs depuis le .ics statique déjà généré
  const icsRes = await fetch(icsFullUrl(row.fichier));
  if (icsRes.ok) {
    const events = parseIcs(await icsRes.text())
      .map(ev => ({
        ev,
        start: icsDateToRfc3339(ev.start || ""),
        end:   icsDateToRfc3339(ev.end || ""),
      }))
      .filter(x => x.start && x.end);

    // events.import conserve l'UID d'origine → la synchro ultérieure peut
    // retrouver et mettre à jour chaque match sans créer de doublon.
    const importOne = ({ ev, start, end }) =>
      fetch(`${GOOGLE_CAL_API}/calendars/${encodeURIComponent(calendarId)}/events/import`, {
        method: "POST",
        headers: auth,
        body: JSON.stringify({
          iCalUID:     ev.uid,
          summary:     ev.summary || "Match",
          description: ev.description || "",
          location:    ev.location || "",
          start: { dateTime: start, timeZone: "Europe/Paris" },
          end:   { dateTime: end,   timeZone: "Europe/Paris" },
        }),
      }).catch(() => null);   // un match raté ne doit pas casser l'abonnement

    // Par lots de 8 : l'utilisateur attend la réponse, on évite ~25 allers-retours
    // séquentiels sans pour autant saturer l'API Google.
    for (let i = 0; i < events.length; i += 8) {
      await Promise.all(events.slice(i, i + 8).map(importOne));
    }
  }

  await saveGoogleCalendarId(row.fichier, equipe, calendarId, env);
  return calendarId;
}

/** Retourne l'id du calendrier Google de l'équipe, en le créant si besoin. */
async function getOrCreateGoogleCalendar(row, env) {
  if (!env.GOOGLE_SA_JSON || !NOCODB_TABLE_GCAL) return null;
  try {
    const existing = await findGoogleCalendarId(row.fichier, env);
    if (existing) return existing;
    return await createGoogleCalendar(row, env);
  } catch (e) {
    // En cas d'échec on ne casse pas l'abonnement : la page retombera sur le
    // lien .ics classique.
    console.error("Google Calendar :", e.message);
    return null;
  }
}


// ── POST /subscribe ───────────────────────────────────────────────────────────

async function handleSubscribe(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: "JSON invalide" }, 400); }

  const { email, equipe, comp_nom, fichier, device } = body;
  if (!email || !equipe || !fichier) {
    return json({ error: "Champs manquants : email, equipe, fichier" }, 400);
  }

  const token = uuid();
  const now   = new Date().toISOString();

  // ── Enregistrement NocoDB ─────────────────────────────────────────────────
  const noco = await fetch(
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE}`,
    {
      method: "POST",
      headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ email, equipe, comp_nom, fichier, device, token, token_used: false, subscribed_at: now }),
    }
  );
  if (!noco.ok) {
    const err = await noco.text();
    return json({ error: "NocoDB : " + err }, 500);
  }

  // ── Email Brevo ───────────────────────────────────────────────────────────
  const tokenLink = `${env.WORKER_URL}/sub?token=${token}`;
  const compSuffix = comp_nom ? ` — ${comp_nom}` : "";

  // Contenu volontairement sobre (pas de gros logo/en-tête, un seul bouton
  // discret) : le style "campagne marketing" chargé est un signal fort pour
  // le tri automatique de Gmail vers l'onglet Promotions.
  const emailHtml = `
  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
              max-width:480px;margin:0 auto;padding:24px;color:#1B2A4A;font-size:.95rem;line-height:1.5">
    <p style="margin-bottom:12px">Bonjour,</p>
    <p style="margin-bottom:20px">
      Voici le lien pour vous abonner au calendrier de
      <strong>${equipe}</strong>${compSuffix} :
    </p>

    <p style="margin-bottom:20px">
      <a href="${tokenLink}"
         style="display:inline-block;background:#E84E0F;color:#fff;
                text-decoration:none;padding:12px 24px;border-radius:8px;
                font-weight:600;font-size:.95rem">
        S'abonner au calendrier
      </a>
    </p>

    <p style="margin-bottom:16px;color:#6B7280">
      Ouvrez ce lien depuis votre téléphone pour ajouter le calendrier
      directement dans votre application Agenda. Les mises à jour (horaires,
      scores, arbitres) apparaîtront ensuite automatiquement — ce lien n'est
      valable qu'une seule fois.
    </p>

    <p style="margin-bottom:0;color:#6B7280">
      Bon match,<br>Agendas FFBB
    </p>

    <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0">
    <p style="font-size:.72rem;color:#9CA3AF">
      Données issues de competitions.ffbb.com · Projet non officiel
    </p>
  </div>`;

  const emailText =
`Bonjour,

Voici le lien pour vous abonner au calendrier de ${equipe}${compSuffix} :
${tokenLink}

Ouvrez-le depuis votre téléphone pour ajouter le calendrier directement dans
votre application Agenda. Les mises à jour (horaires, scores, arbitres)
apparaîtront ensuite automatiquement — ce lien n'est valable qu'une seule fois.

Bon match,
Agendas FFBB

Données issues de competitions.ffbb.com · Projet non officiel`;

  const brevo = await fetch("https://api.brevo.com/v3/smtp/email", {
    method: "POST",
    headers: { "api-key": env.BREVO_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      sender:      { name: SENDER_NAME, email: SENDER_EMAIL },
      to:          [{ email }],
      replyTo:     { email: SENDER_EMAIL },
      subject:     `Votre abonnement au calendrier — ${equipe}`,
      htmlContent: emailHtml,
      textContent: emailText,
      // Désactive le wrapper de tracking Brevo (sendibt2.com) qui casse le lien sur Android
      // et évite les pixels/liens de tracking qui font pencher Gmail vers "Promotions".
      trackClicks: false,
      trackOpens:  false,
      // Un en-tête List-Unsubscribe (même en mailto) est un signal positif pour
      // les filtres anti-spam : son absence est typique des envois non légitimes.
      headers: {
        "List-Unsubscribe": `<mailto:${SENDER_EMAIL}?subject=Desabonnement>`,
      },
    }),
  });
  if (!brevo.ok) {
    const err = await brevo.text();
    return json({ error: "Brevo : " + err }, 500);
  }

  return json({ ok: true });
}

// ── POST /feedback ────────────────────────────────────────────────────────────

async function handleFeedback(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: "JSON invalide" }, 400); }

  const { message, email: userEmail } = body;
  if (!message || message.trim().length < 5) {
    return json({ error: "Message trop court" }, 400);
  }

  const replyLine = userEmail ? `<p><strong>Email de l'utilisateur :</strong> ${userEmail}</p>` : "<p><em>Aucun email fourni</em></p>";

  const emailHtml = `
  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
              max-width:480px;margin:0 auto;padding:32px 24px;color:#1B2A4A">
    <h2 style="margin-bottom:16px">🐛 Nouveau signalement — Agendas FFBB</h2>
    ${replyLine}
    <div style="background:#F5F6FA;border-radius:8px;padding:16px;margin-top:16px;
                font-size:.9rem;white-space:pre-wrap">${message.trim()}</div>
    <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0">
    <p style="font-size:.72rem;color:#9CA3AF">Envoyé depuis Agendas FFBB</p>
  </div>`;

  const brevo = await fetch("https://api.brevo.com/v3/smtp/email", {
    method: "POST",
    headers: { "api-key": env.BREVO_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      sender:      { name: "Agendas FFBB", email: SENDER_EMAIL },
      to:          [{ email: "jonathan.varani@varai.fr" }],
      replyTo:     userEmail ? { email: userEmail } : undefined,
      subject:     "🐛 Signalement Agendas FFBB",
      htmlContent: emailHtml,
    }),
  });
  if (!brevo.ok) {
    const err = await brevo.text();
    return json({ error: "Brevo : " + err }, 500);
  }

  return json({ ok: true });
}

// ── POST /contact-parents ─────────────────────────────────────────────────────

async function handleContactParents(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: "JSON invalide" }, 400); }

  const {
    joueur_nom, joueur_prenom, joueur_telephone,
    pere_nom, pere_prenom, pere_telephone,
    mere_nom, mere_prenom, mere_telephone,
  } = body;

  if (!joueur_nom || !joueur_prenom || !joueur_telephone) {
    return json({ error: "Champs manquants : nom, prénom et téléphone du joueur" }, 400);
  }

  const noco = await fetch(
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE_CONTACTS}`,
    {
      method: "POST",
      headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({
        joueur_nom, joueur_prenom, joueur_telephone,
        pere_nom, pere_prenom, pere_telephone,
        mere_nom, mere_prenom, mere_telephone,
        submitted_at: new Date().toISOString(),
      }),
    }
  );
  if (!noco.ok) {
    const err = await noco.text();
    return json({ error: "NocoDB : " + err }, 500);
  }

  return json({ ok: true });
}

// ── GET /sub?token=xxx ────────────────────────────────────────────────────────

async function handleToken(request, env) {
  const url   = new URL(request.url);
  const token = url.searchParams.get("token");
  const debug = url.searchParams.get("debug") === "1";   // ?debug=1 pour diagnostiquer

  if (!token) return html("<h2>Token manquant.</h2>", 400);

  // ── Recherche NocoDB (v1) ─────────────────────────────────────────────────
  const searchUrl =
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE}` +
    `?where=(token,eq,${encodeURIComponent(token)})&limit=1`;

  let searchRes, data;
  try {
    searchRes = await fetch(searchUrl, { headers: { "xc-token": env.NOCODB_TOKEN } });
    data      = await searchRes.json();
  } catch (e) {
    return html(`<h2>Erreur NocoDB</h2><pre>${e.message}</pre>`, 500);
  }

  if (debug) {
    return new Response(JSON.stringify({ searchUrl, status: searchRes.status, data }, null, 2), {
      headers: { "Content-Type": "application/json" },
    });
  }

  // NocoDB v1 renvoie { list: [...] } ; v2 pourrait renvoyer { records: [...] }
  const rows = data.list ?? data.records ?? [];
  const row  = rows[0];

  if (!row) {
    return html("<h2>🔗 Lien invalide ou expiré.</h2>" +
      `<p><a href="${PAGES_BASE}">Retour au calendrier</a></p>`, 404);
  }

  if (row.token_used) {
    return html(
      `<h2>🔒 Ce lien a déjà été utilisé.</h2>
       <p>Retournez sur <a href="${PAGES_BASE}">Agendas FFBB</a>
       pour obtenir un nouveau lien.</p>`, 410);
  }

  // ── Marquer utilisé (row.Id = NocoDB v1, row.id = v2) ───────────────────
  const rowId = row.Id ?? row.id;
  if (rowId) {
    await fetch(
      `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE}/${rowId}`,
      {
        method: "PATCH",
        headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
        body: JSON.stringify({ token_used: true, token_used_at: new Date().toISOString() }),
      }
    );
  }

  // ── Page d'abonnement ───────────────────────────────────────────────────────
  // Un clic utilisateur direct est indispensable : un redirect 302 vers webcal://
  // donne une page blanche dans un navigateur mobile.
  const httpsUrl    = icsFullUrl(row.fichier);                          // lien direct (téléchargement navigateur)
  const webcalUrl   = icsProxyUrl(row.fichier, env).replace(/^https?:\/\//, "webcal://"); // abonnement (charset correct)
  const equipeLabel = row.equipe || "votre équipe";

  // Android : un vrai calendrier Google (cid=<id google>) s'affiche tout de
  // suite, contrairement à un abonnement .ics externe qui reste invisible tant
  // qu'il n'est pas activé à la main. Créé au premier abonnement de l'équipe.
  // La création (+ import des matchs) peut prendre plusieurs secondes : on ne
  // l'attend pas ici pour ne pas laisser la page blanche, elle se fait en JS
  // via /gcal une fois la page affichée (lien webcal en attendant, déjà valide).
  const googleUrl = `https://calendar.google.com/calendar/render?cid=${encodeURIComponent(webcalUrl)}`;
  const gcalQuery = `fichier=${encodeURIComponent(row.fichier)}&equipe=${encodeURIComponent(row.equipe || "")}&comp_nom=${encodeURIComponent(row.comp_nom || "")}`;

  return new Response(`<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Agendas FFBB — Abonnement</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #F5F6FA; color: #1B2A4A;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; padding: 20px; }
    .card { background: #fff; border-radius: 16px; padding: 32px 24px;
            max-width: 400px; width: 100%; text-align: center;
            box-shadow: 0 4px 20px rgba(0,0,0,.08); }
    h1 { font-size: 1.15rem; margin: 14px 0 6px; }
    .sub { font-size: .88rem; color: #6B7280; margin-bottom: 24px; line-height: 1.5; }
    a.btn { display: block; color: #fff; text-decoration: none; padding: 15px;
            border-radius: 12px; font-weight: 700; font-size: 1rem; margin-bottom: 10px; }
    .ios { background: #E84E0F; }
    .android { background: #1A73E8; }
    .steps { text-align: left; background: #F5F6FA; border-radius: 10px;
             padding: 14px 16px; font-size: .8rem; color: #4B5563;
             line-height: 1.6; margin-top: 14px; }
    .steps strong { color: #1B2A4A; }
    .alt { font-size: .75rem; color: #9CA3AF; margin-top: 16px; }
    .alt a { color: #9CA3AF; }
    .hidden { display: none; }
    .loading { font-size: .8rem; color: #6B7280; margin: -2px 0 10px; }
  </style>
</head>
<body>
  <div class="card">
    <div style="font-size:3rem">🏀</div>
    <h1>Abonnement prêt !</h1>
    <p class="sub">Calendrier de <strong>${equipeLabel}</strong></p>

    <!-- iOS / Mac -->
    <div id="block-ios" class="hidden">
      <a href="${webcalUrl}" class="btn ios">📅 Ajouter à mon calendrier</a>
      <div class="steps">
        Votre application <strong>Calendrier</strong> va s'ouvrir.
        Appuyez sur <strong>S'abonner</strong> puis <strong>Terminé</strong>.
      </div>
    </div>

    <!-- Android -->
    <div id="block-android" class="hidden">
      <a href="${googleUrl}" class="btn android google-btn">📅 Ajouter à Google Agenda</a>
      <p class="loading google-loading hidden">⏳ Préparation de votre calendrier Google…</p>
      <div class="steps">
        <strong>Sur Android</strong>, l'abonnement passe par votre compte Google :<br>
        1. La page Google Agenda s'ouvre → appuyez sur <strong>Ajouter</strong><br>
        2. Le calendrier apparaît ensuite <strong>automatiquement</strong> dans l'app Agenda de votre téléphone<br>
        3. Comptez jusqu'à quelques heures pour la première synchronisation
      </div>
    </div>

    <!-- Desktop / inconnu : les deux -->
    <div id="block-both" class="hidden">
      <a href="${webcalUrl}" class="btn ios">📱 iPhone / iPad / Mac</a>
      <a href="${googleUrl}" class="btn android google-btn">🤖 Android / Google Agenda</a>
      <p class="loading google-loading hidden">⏳ Préparation de votre calendrier Google…</p>
    </div>

    <p class="alt">
      Lien direct : <a href="${httpsUrl}">${httpsUrl}</a>
    </p>
  </div>

  <script>
    var ua = navigator.userAgent || "";
    var isIOS = /iPad|iPhone|iPod/.test(ua) ||
                (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    var isMac = /Macintosh/.test(ua);
    var isAndroid = /Android/.test(ua);
    var id = isAndroid ? "block-android" : (isIOS || isMac) ? "block-ios" : "block-both";
    document.getElementById(id).classList.remove("hidden");

    // Le lien webcal:// ci-dessus fonctionne déjà : on tente juste d'obtenir le
    // vrai calendrier Google (affichage immédiat côté Android) en tâche de fond,
    // sans jamais bloquer l'affichage de la page.
    if (id === "block-android" || id === "block-both") {
      var btns    = document.querySelectorAll("#" + id + " .google-btn");
      var loaders = document.querySelectorAll("#" + id + " .google-loading");
      loaders.forEach(function (l) { l.classList.remove("hidden"); });
      fetch("/gcal?${gcalQuery}")
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.url) {
            btns.forEach(function (b) { b.href = data.url; });
          }
        })
        .catch(function () { /* on garde le lien webcal:// déjà en place */ })
        .finally(function () { loaders.forEach(function (l) { l.classList.add("hidden"); }); });
    }
  </script>
</body>
</html>`, { headers: { "Content-Type": "text/html;charset=UTF-8" } });
}

// ── GET /gcal?fichier=...&equipe=...&comp_nom=... ──────────────────────────────
// Appelé en tâche de fond par la page d'abonnement (jamais par l'utilisateur
// directement) : crée le vrai calendrier Google si besoin, sans bloquer le
// rendu de /sub. Ne prend aucune donnée sensible en entrée.

async function handleGcal(request, env) {
  const url     = new URL(request.url);
  const fichier = url.searchParams.get("fichier");
  const equipe  = url.searchParams.get("equipe") || "";
  const compNom = url.searchParams.get("comp_nom") || "";

  if (!fichier) return json({ gcalId: null, url: null }, 400);

  const gcalId = await getOrCreateGoogleCalendar({ fichier, equipe, comp_nom: compNom }, env);
  // Google attend le cid encodé en base64 (pas juste URL-encodé) sur
  // /calendar/u/0 — le format /r?cid=<id brut> ne s'ajoute pas côté Android.
  const addUrl = gcalId
    ? `https://calendar.google.com/calendar/u/0?cid=${encodeURIComponent(btoa(gcalId))}`
    : null;
  return json({ gcalId, url: addUrl });
}

// ── Helpers réponse ───────────────────────────────────────────────────────────

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...cors() },
  });
}

function html(content, status = 200) {
  return new Response(
    `<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
     <meta name="viewport" content="width=device-width,initial-scale=1">
     <title>Agendas FFBB</title>
     <style>body{font-family:-apple-system,sans-serif;text-align:center;
     padding:60px 20px;color:#1B2A4A}a{color:#E84E0F}</style></head>
     <body><div style="font-size:2rem">🏀</div>${content}</body></html>`,
    { status, headers: { "Content-Type": "text/html;charset=UTF-8" } }
  );
}

// ── Entry point ───────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    const path = pathname.replace(/\/+/g, "/"); // normalise // → /

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors() });
    }
    if (request.method === "POST" && path === "/subscribe") {
      return handleSubscribe(request, env);
    }
    if (request.method === "POST" && path === "/feedback") {
      return handleFeedback(request, env);
    }
    if (request.method === "POST" && path === "/contact-parents") {
      return handleContactParents(request, env);
    }
    if (request.method === "GET" && path === "/sub") {
      return handleToken(request, env);
    }
    if (request.method === "GET" && path === "/gcal") {
      return handleGcal(request, env);
    }
    if (request.method === "GET" && path.startsWith("/ics/")) {
      return handleIcsProxy(path);
    }

    return new Response("Not found", { status: 404 });
  },
};

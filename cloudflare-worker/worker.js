/**
 * FFBB Agenda — Cloudflare Worker
 * Test VARAI
 *
 * POST /subscribe  → enregistre dans NocoDB, retourne directement les liens
 *                     d'abonnement (iOS/Android) + envoie un email de
 *                     remerciement en tâche de fond (pas de confirmation).
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
/**
 * URL publique de ce worker. On la déduit de la requête entrante plutôt que de
 * dépendre du secret WORKER_URL : s'il n'est pas défini dans Cloudflare, on
 * générait des liens "undefined/..." (page DNS_PROBE_FINISHED_NXDOMAIN côté
 * mobile). Le secret reste prioritaire s'il est présent.
 */
function workerOrigin(request, env) {
  const fromEnv = (env && env.WORKER_URL || "").trim();
  if (/^https?:\/\//.test(fromEnv)) return fromEnv.replace(/\/+$/, "");
  return new URL(request.url).origin;
}

function icsProxyUrl(fichier, request, env) {
  return `${workerOrigin(request, env)}/ics/${fichier}`;
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

async function findGoogleCalendarRows(fichier, env) {
  const url =
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE_GCAL}` +
    `?where=(fichier,eq,${encodeURIComponent(fichier)})`;
  const res = await fetch(url, { headers: { "xc-token": env.NOCODB_TOKEN } });
  if (!res.ok) return [];
  const data = await res.json();
  return data.list ?? data.records ?? [];
}

/** Pose un jalon (google_calendar_id vide) et retourne son Id NocoDB. */
async function reserveGoogleCalendarRow(fichier, equipe, env) {
  const res = await fetch(
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE_GCAL}`,
    {
      method: "POST",
      headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({
        fichier,
        equipe,
        google_calendar_id: "",
        created_at: new Date().toISOString(),
      }),
    },
  );
  if (!res.ok) return null;
  const created = await res.json().catch(() => ({}));
  return created.Id ?? created.id ?? null;
}

async function deleteGoogleCalendarRow(rowId, env) {
  await fetch(
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE_GCAL}/${rowId}`,
    { method: "DELETE", headers: { "xc-token": env.NOCODB_TOKEN } },
  ).catch(() => null);
}

/** Attend qu'une autre requête ait fini de créer le calendrier (~30s max). */
async function waitForGoogleCalendarId(fichier, env) {
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 500));
    const rows  = await findGoogleCalendarRows(fichier, env);
    const ready = rows.find(r => r.google_calendar_id);
    if (ready) return ready.google_calendar_id;
  }
  return null;
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

  return calendarId;
}

/**
 * Retourne l'id du calendrier Google de l'équipe, en le créant si besoin.
 *
 * Le worker peut recevoir deux appels concurrents pour la même équipe
 * (tâche de fond de /subscribe + polling /gcal du front) : sans verrou, les
 * deux passaient le test "aucune ligne trouvée" avant que l'un des deux
 * n'ait fini d'écrire, créant deux calendriers Google en double. La
 * contrainte unique NocoDB n'est pas disponible sur ce plan ; on gère donc
 * la course "après coup" — chaque candidat pose un jalon (ligne avec
 * google_calendar_id vide), puis relit toutes les lignes de l'équipe : la
 * plus ancienne (Id le plus petit) gagne et crée le calendrier, les autres
 * suppriment leur jalon et attendent son résultat.
 */
async function getOrCreateGoogleCalendar(row, env) {
  if (!env.GOOGLE_SA_JSON || !NOCODB_TABLE_GCAL) return null;
  try {
    let rows  = await findGoogleCalendarRows(row.fichier, env);
    let ready = rows.find(r => r.google_calendar_id);
    if (ready) return ready.google_calendar_id;

    let myId = null;
    if (rows.length === 0) {
      myId  = await reserveGoogleCalendarRow(row.fichier, row.equipe, env);
      rows  = await findGoogleCalendarRows(row.fichier, env);
      ready = rows.find(r => r.google_calendar_id);
      if (ready) {
        if (myId) await deleteGoogleCalendarRow(myId, env);
        return ready.google_calendar_id;
      }
    }

    const winnerId = Math.min(...rows.map(r => r.Id ?? r.id));

    if (myId !== winnerId) {
      // On a perdu la course (ou une réservation existait déjà avant nous).
      if (myId) await deleteGoogleCalendarRow(myId, env);
      return await waitForGoogleCalendarId(row.fichier, env);
    }

    // On a gagné la course : seul ce worker crée le calendrier.
    const calendarId = await createGoogleCalendar(row, env);
    await fetch(
      `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE_GCAL}/${winnerId}`,
      {
        method: "PATCH",
        headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
        body: JSON.stringify({ google_calendar_id: calendarId }),
      },
    );
    return calendarId;
  } catch (e) {
    // En cas d'échec on ne casse pas l'abonnement : la page retombera sur le
    // lien .ics classique.
    console.error("Google Calendar :", e.message);
    return null;
  }
}


// ── POST /subscribe ───────────────────────────────────────────────────────────

/**
 * Envoie l'email de remerciement (pas de lien d'abonnement : l'utilisateur y a
 * déjà accès directement) puis note la date d'envoi sur la ligne NocoDB
 * correspondante. Appelé en tâche de fond, ne doit jamais faire échouer
 * l'abonnement lui-même.
 */
async function sendThankYouEmail(rowId, email, equipe, compNom, env) {
  const compSuffix = compNom ? ` — ${compNom}` : "";

  // Contenu volontairement sobre (pas de gros logo/en-tête) : le style
  // "campagne marketing" chargé est un signal fort pour le tri automatique
  // de Gmail vers l'onglet Promotions.
  const emailHtml = `
  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
              max-width:480px;margin:0 auto;padding:24px;color:#1B2A4A;font-size:.95rem;line-height:1.5">
    <p style="margin-bottom:12px">Bonjour,</p>
    <p style="margin-bottom:20px">
      Merci ! Vous êtes bien abonné(e) au calendrier de
      <strong>${equipe}</strong>${compSuffix}. Les mises à jour (horaires,
      scores, arbitres) apparaîtront désormais automatiquement dans votre
      application Agenda.
    </p>

    <p style="margin-bottom:16px;color:#6B7280">
      Un souci avec votre abonnement ? Répondez directement à cet email,
      nous reviendrons vers vous.
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

Merci ! Vous êtes bien abonné(e) au calendrier de ${equipe}${compSuffix}.
Les mises à jour (horaires, scores, arbitres) apparaîtront désormais
automatiquement dans votre application Agenda.

Un souci avec votre abonnement ? Répondez directement à cet email, nous
reviendrons vers vous.

Bon match,
Agendas FFBB

Données issues de competitions.ffbb.com · Projet non officiel`;

  try {
    const brevo = await fetch("https://api.brevo.com/v3/smtp/email", {
      method: "POST",
      headers: { "api-key": env.BREVO_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({
        sender:      { name: SENDER_NAME, email: SENDER_EMAIL },
        to:          [{ email }],
        replyTo:     { email: SENDER_EMAIL },
        subject:     `Confirmation de votre abonnement — ${equipe}`,
        htmlContent: emailHtml,
        textContent: emailText,
        trackClicks: false,
        trackOpens:  false,
        headers: {
          "List-Unsubscribe": `<mailto:${SENDER_EMAIL}?subject=Desabonnement>`,
        },
      }),
    });
    if (!brevo.ok) {
      console.error("Brevo (remerciement) :", await brevo.text());
      return;
    }
  } catch (e) {
    console.error("Brevo (remerciement) :", e.message);
    return;
  }

  if (!rowId) return;
  await fetch(
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE}/${rowId}`,
    {
      method: "PATCH",
      headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ thankyou_sent_at: new Date().toISOString() }),
    }
  ).catch(e => console.error("NocoDB (thankyou_sent_at) :", e.message));
}

async function handleSubscribe(request, env, ctx) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: "JSON invalide" }, 400); }

  const { email, equipe, comp_nom, fichier, device } = body;
  if (!email || !equipe || !fichier) {
    return json({ error: "Champs manquants : email, equipe, fichier" }, 400);
  }

  const now = new Date().toISOString();

  // ── Enregistrement NocoDB ─────────────────────────────────────────────────
  const noco = await fetch(
    `${NOCODB_API}/api/v1/db/data/noco/${NOCODB_BASE}/${NOCODB_TABLE}`,
    {
      method: "POST",
      headers: { "xc-token": env.NOCODB_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ email, equipe, comp_nom, fichier, device, subscribed_at: now }),
    }
  );
  if (!noco.ok) {
    const err = await noco.text();
    return json({ error: "NocoDB : " + err }, 500);
  }
  const createdRow = await noco.json().catch(() => ({}));
  const rowId = createdRow.Id ?? createdRow.id ?? null;

  // ── Email de remerciement, en tâche de fond ───────────────────────────────
  // Le calendrier Google n'est PAS lancé ici : le front (docs/index.html)
  // appelle /gcal juste après cette réponse pour le créer à la demande. Le
  // lancer aussi depuis /subscribe créait une course avec cet appel — les
  // deux se lançaient au même instant, et la logique anti-doublon fait
  // alors attendre l'un des deux le temps que l'autre termine, ce qui
  // rallonge l'attente pour rien dans le cas courant (un seul abonné).
  if (ctx && typeof ctx.waitUntil === "function") {
    ctx.waitUntil(sendThankYouEmail(rowId, email, equipe, comp_nom, env));
  } else {
    await sendThankYouEmail(rowId, email, equipe, comp_nom, env);
  }

  // ── Liens d'abonnement, retournés directement (pas d'étape de confirmation) ─
  const httpsUrl  = icsFullUrl(fichier);
  const webcalUrl = icsProxyUrl(fichier, request, env).replace(/^https?:\/\//, "webcal://");
  const googleUrl = `https://calendar.google.com/calendar/render?cid=${encodeURIComponent(webcalUrl)}`;

  return json({ ok: true, equipe, comp_nom: comp_nom || "", fichier, httpsUrl, webcalUrl, googleUrl });
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


// ── GET /gcal?fichier=...&equipe=...&comp_nom=... ──────────────────────────────
// Appelé en tâche de fond par le front (jamais par l'utilisateur directement) :
// crée le vrai calendrier Google si besoin, sans bloquer la réponse de
// /subscribe. Ne prend aucune donnée sensible en entrée.

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

// ── Entry point ───────────────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    const path = pathname.replace(/\/+/g, "/"); // normalise // → /

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors() });
    }
    if (request.method === "POST" && path === "/subscribe") {
      return handleSubscribe(request, env, ctx);
    }
    if (request.method === "POST" && path === "/feedback") {
      return handleFeedback(request, env);
    }
    if (request.method === "POST" && path === "/contact-parents") {
      return handleContactParents(request, env);
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

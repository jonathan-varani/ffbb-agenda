/**
 * FFBB Agenda — Cloudflare Worker
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
const PAGES_BASE   = "https://jonathan-varani.github.io/ffbb-agenda";
const SENDER_EMAIL = "jonathan.varani@gmail.com";   // à remplacer par noreply@varai.fr quand le mail sera configuré
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

function subUrl(fichier, device) {
  const full = icsFullUrl(fichier);
  return full.replace(/^https?:\/\//, "webcal://");
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
  const icsUrl     = icsFullUrl(fichier);
  const webcalLink = icsUrl.replace(/^https?:\/\//, "webcal://");

  const emailHtml = `
  <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
              max-width:480px;margin:0 auto;padding:32px 24px;color:#1B2A4A">
    <div style="text-align:center;margin-bottom:28px">
      <div style="font-size:2.8rem">🏀</div>
      <h1 style="font-size:1.25rem;font-weight:700;margin-top:8px">Agendas FFBB</h1>
    </div>

    <p style="margin-bottom:12px">Bonjour,</p>
    <p style="margin-bottom:20px">
      Voici votre lien d'abonnement au calendrier de
      <strong>${equipe}</strong>${comp_nom ? ` — ${comp_nom}` : ""}.
    </p>

    <div style="text-align:center;margin:28px 0">
      <p style="font-size:.85rem;color:#6B7280;margin-bottom:12px">
        Ouvre ce lien depuis ton téléphone 📱
      </p>
      <a href="${webcalLink}"
         style="display:inline-block;background:#E84E0F;color:#fff;
                text-decoration:none;padding:14px 32px;border-radius:10px;
                font-weight:700;font-size:1rem">
        📅 S'abonner au calendrier
      </a>
    </div>

    <div style="background:#F5F6FA;border-radius:8px;padding:14px 16px;
                font-size:.82rem;color:#6B7280;margin-bottom:24px">
      Les mises à jour (horaires, scores, arbitres) apparaîtront
      <strong>automatiquement</strong> dans votre application Calendrier.
    </div>

    <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0">
    <p style="font-size:.72rem;color:#9CA3AF;text-align:center">
      Données issues de competitions.ffbb.com · Projet non officiel
    </p>
  </div>`;

  const brevo = await fetch("https://api.brevo.com/v3/smtp/email", {
    method: "POST",
    headers: { "api-key": env.BREVO_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      sender:      { name: SENDER_NAME, email: SENDER_EMAIL },
      to:          [{ email }],
      subject:     `📅 Votre abonnement FFBB — ${equipe}`,
      htmlContent: emailHtml,
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
      to:          [{ email: "jonathan.varani@gmail.com" }],
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

  // ── Redirection ────────────────────────────────────────────────────────────
  const dest = subUrl(row.fichier, row.device);
  return Response.redirect(dest, 302);
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
    if (request.method === "GET" && path === "/sub") {
      return handleToken(request, env);
    }

    return new Response("Not found", { status: 404 });
  },
};

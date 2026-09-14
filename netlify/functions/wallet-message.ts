import { Handler } from "@netlify/functions";

const SUPABASE_URL = process.env.SUPABASE_URL?.replace(/\/$/, "");
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY;

export const handler: Handler = async (event) => {
  const headers = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Content-Type": "application/json",
  };

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers, body: "" };
  }

  const token = event.queryStringParameters?.session || event.queryStringParameters?.token;
  if (!token) {
    return { statusCode: 400, headers, body: JSON.stringify({ ok: false, error: "Token de session manquant" }) };
  }

  if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) {
    return { statusCode: 500, headers, body: JSON.stringify({ ok: false, error: "Configuration Supabase manquante" }) };
  }

  try {
    const sbHeaders = {
      apikey: SUPABASE_SERVICE_KEY,
      Authorization: `Bearer ${SUPABASE_SERVICE_KEY}`,
      "Content-Type": "application/json",
    };

    const res = await fetch(`${SUPABASE_URL}/rest/v1/wallet_sessions?token=eq.${encodeURIComponent(token)}`, {
      headers: sbHeaders,
    });
    const sessions = await res.json();
    if (!sessions || sessions.length === 0) {
      return { statusCode: 404, headers, body: JSON.stringify({ ok: false, error: "Session introuvable ou expirée" }) };
    }

    const session = sessions[0];
    const message = `NFT Market Wallet Verification\nDiscord User ID: ${session.discord_user_id}\nNonce: ${session.nonce}`;

    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        ok: true,
        nonce: session.nonce,
        discord_user_id: session.discord_user_id,
        message,
        used: session.used,
        expiresAt: session.expires_at,
      }),
    };
  } catch (err: any) {
    return { statusCode: 500, headers, body: JSON.stringify({ ok: false, error: err.message }) };
  }
};

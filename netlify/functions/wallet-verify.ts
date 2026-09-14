import { Handler } from "@netlify/functions";
import nacl from "tweetnacl";
import bs58 from "bs58";

// Clé secrète Supabase service_role stockée dans les variables secrètes Netlify (JAMAIS dans le frontend)
const SUPABASE_URL = process.env.SUPABASE_URL?.replace(/\/$/, "");
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY;

export const handler: Handler = async (event) => {
  // CORS Headers pour autoriser les requêtes du navigateur
  const headers = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Content-Type": "application/json",
  };

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers, body: "" };
  }

  if (event.httpMethod !== "POST") {
    return { statusCode: 405, headers, body: JSON.stringify({ error: "Method not allowed" }) };
  }

  try {
    const body = JSON.parse(event.body || "{}");
    const sessionToken = body.session || body.token;
    const publicKey = body.publicKey || body.public_key;
    const signature = body.signature;

    if (!sessionToken || !publicKey || !signature) {
      return {
        statusCode: 400,
        headers,
        body: JSON.stringify({ ok: false, error: "Paramètres session, publicKey et signature requis." }),
      };
    }

    if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) {
      return {
        statusCode: 500,
        headers,
        body: JSON.stringify({ ok: false, error: "Configuration Supabase manquante sur le serveur Netlify." }),
      };
    }

    const sbHeaders = {
      apikey: SUPABASE_SERVICE_KEY,
      Authorization: `Bearer ${SUPABASE_SERVICE_KEY}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
    };

    // 1. Récupérer la session de vérification depuis Supabase
    const sessionRes = await fetch(`${SUPABASE_URL}/rest/v1/wallet_sessions?token=eq.${encodeURIComponent(sessionToken)}`, {
      headers: sbHeaders,
    });
    const sessions = await sessionRes.json();
    if (!sessions || sessions.length === 0) {
      return { statusCode: 404, headers, body: JSON.stringify({ ok: false, error: "Session introuvable ou expirée." }) };
    }

    const session = sessions[0];
    if (session.used) {
      return { statusCode: 400, headers, body: JSON.stringify({ ok: false, error: "Ce lien de vérification a déjà été utilisé." }) };
    }

    if (new Date(session.expires_at).getTime() < Date.now()) {
      return { statusCode: 400, headers, body: JSON.stringify({ ok: false, error: "Ce lien a expiré. Relancez /wallet sur Discord." }) };
    }

    // 2. Reconstruire le message cryptographique attendu (EXACTEMENT identique à main.py)
    const expectedMessage = `NFT Market Wallet Verification\nDiscord User ID: ${session.discord_user_id}\nNonce: ${session.nonce}`;
    const messageBytes = new TextEncoder().encode(expectedMessage);

    // 3. Décodage de la clé publique Solana et de la signature
    const publicKeyBytes = bs58.decode(publicKey);
    let signatureBytes: Uint8Array;
    try {
      signatureBytes = bs58.decode(signature);
      if (signatureBytes.length !== 64) {
        signatureBytes = Uint8Array.from(Buffer.from(signature, "base64"));
      }
    } catch {
      signatureBytes = Uint8Array.from(Buffer.from(signature, "base64"));
    }

    if (publicKeyBytes.length !== 32 || signatureBytes.length !== 64) {
      return { statusCode: 400, headers, body: JSON.stringify({ ok: false, error: "Format de clé ou de signature Solana invalide." }) };
    }

    // 4. Vérification cryptographique Ed25519 pure (aucun tiers requis, 0 fuite de secret)
    const isValid = nacl.sign.detached.verify(messageBytes, signatureBytes, publicKeyBytes);
    if (!isValid) {
      return { statusCode: 400, headers, body: JSON.stringify({ ok: false, error: "La signature cryptographique fournie est invalide." }) };
    }

    // 5. Enregistrer l'association dans Supabase (on_conflict explicite + vérification du statut HTTP)
    //    FIX: le code précédent n'attendait pas et ne vérifiait pas cette écriture, donc une erreur
    //    Supabase silencieuse laissait passer un "succès" côté frontend sans rien enregistrer.
    const assocRes = await fetch(
      `${SUPABASE_URL}/rest/v1/wallet_associations?on_conflict=discord_user_id`,
      {
        method: "POST",
        headers: { ...sbHeaders, Prefer: "resolution=merge-duplicates,return=representation" },
        body: JSON.stringify({
          discord_user_id: session.discord_user_id,
          public_key: publicKey,
          verified_at: new Date().toISOString(),
        }),
      }
    );
    if (!assocRes.ok) {
      const errText = await assocRes.text();
      return {
        statusCode: 500,
        headers,
        body: JSON.stringify({ ok: false, error: "Échec de l'enregistrement Supabase: " + errText }),
      };
    }

    // 6. Marquer la session comme utilisée (également vérifié désormais)
    const usedRes = await fetch(`${SUPABASE_URL}/rest/v1/wallet_sessions?token=eq.${encodeURIComponent(sessionToken)}`, {
      method: "PATCH",
      headers: sbHeaders,
      body: JSON.stringify({ used: true }),
    });
    if (!usedRes.ok) {
      const errText = await usedRes.text();
      return {
        statusCode: 500,
        headers,
        body: JSON.stringify({ ok: false, error: "Échec de la clôture de session: " + errText }),
      };
    }

    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        ok: true,
        message: "Portefeuille Solana vérifié avec succès !",
        publicKey,
        discordUserId: session.discord_user_id,
      }),
    };
  } catch (err: any) {
    return {
      statusCode: 500,
      headers,
      body: JSON.stringify({ error: err.message || "Erreur interne" }),
    };
  }
};

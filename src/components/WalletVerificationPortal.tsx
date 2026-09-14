import React, { useState, useEffect } from 'react';
import { ShieldCheck, Wallet, AlertCircle, CheckCircle2, Lock, RefreshCw, KeyRound, ExternalLink } from 'lucide-react';
import { verifySolanaSignature, shortKey } from '../lib/solana-verify';
import type { VerifiedWalletRecord } from '../types';

interface PhantomProvider {
  isPhantom?: boolean;
  publicKey?: { toString(): string };
  connect(options?: { onlyIfTrusted?: boolean }): Promise<{ publicKey: { toString(): string } }>;
  signMessage(message: Uint8Array, display?: string): Promise<{ signature: Uint8Array }>;
}

declare global {
  interface Window {
    solana?: PhantomProvider;
    phantom?: { solana?: PhantomProvider };
  }
}

export const WalletVerificationPortal: React.FC = () => {
  const [sessionToken, setSessionToken] = useState<string>('');
  const [discordId, setDiscordId] = useState<string>('');
  const [nonce, setNonce] = useState<string>('');
  const [expiresAt, setExpiresAt] = useState<string>('');
  const [loading, setLoading] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<{ text: string; type: 'info' | 'success' | 'error' | '' }>({ text: '', type: '' });
  const [verifiedRecord, setVerifiedRecord] = useState<VerifiedWalletRecord | null>(null);

  // Initialize from URL params if present (like from Discord /wallet)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const session = params.get('session');
    if (session) {
      setSessionToken(session);
      // Fetch or simulate session message retrieval
      fetchSessionData(session);
    } else {
      // Default playground session
      generateNewSession('102938475610293847');
    }
  }, []);

  const generateNewSession = (initialDiscordId: string = '102938475610293847') => {
    const randomNonce = Array.from(crypto.getRandomValues(new Uint8Array(18)))
      .map(b => b.toString(36))
      .join('')
      .slice(0, 24);
    const newToken = 'tok_' + Math.random().toString(36).substring(2, 15);
    const exp = new Date(Date.now() + 5 * 60 * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    setSessionToken(newToken);
    setDiscordId(initialDiscordId);
    setNonce(randomNonce);
    setExpiresAt(exp);
    setStatusMessage({ text: 'Nouvelle session de vérification initialisée. Prêt pour signature.', type: 'info' });
  };

  const fetchSessionData = async (token: string) => {
    try {
      setLoading(true);
      const res = await fetch(`/api/wallet/message?session=${encodeURIComponent(token)}`);
      if (res.ok) {
        const data = await res.json();
        const lines = data.message.split('\n');
        const dId = lines[1]?.replace('Discord User ID: ', '').trim() || '';
        const nnce = lines[2]?.replace('Nonce: ', '').trim() || '';
        setDiscordId(dId);
        setNonce(nnce);
        setExpiresAt(new Date(data.expiresAt).toLocaleTimeString());
        setStatusMessage({ text: 'Message sécurisé chargé depuis le serveur.', type: 'info' });
      } else {
        // Fallback for standalone demo if backend aiohttp isn't on the same port
        generateNewSession('DiscordUser_' + token.slice(0, 6));
      }
    } catch {
      generateNewSession('DiscordUser_' + token.slice(0, 6));
    } finally {
      setLoading(false);
    }
  };

  const messageToSign = `NFT Market Wallet Verification\nDiscord User ID: ${discordId || '[DISCORD_ID]'}\nNonce: ${nonce || '[NONCE]'}`;

  const getProvider = (): PhantomProvider | null => {
    if (typeof window !== 'undefined') {
      if (window.phantom?.solana?.isPhantom) {
        return window.phantom.solana;
      }
      if (window.solana?.isPhantom) {
        return window.solana;
      }
      if (window.solana) {
        return window.solana;
      }
    }
    return null;
  };

  const handleConnectAndSign = async () => {
    const provider = getProvider();

    if (!provider) {
      setStatusMessage({
        text: 'Portefeuille Solana non détecté. Veuillez installer l\'extension Phantom ou Solflare dans votre navigateur.',
        type: 'error',
      });
      return;
    }

    try {
      setLoading(true);
      setStatusMessage({ text: 'Connexion au portefeuille Solana...', type: 'info' });

      const resp = await provider.connect();
      const pubKey = resp.publicKey?.toString() || provider.publicKey?.toString();
      if (!pubKey) {
        throw new Error("L'adresse publique du portefeuille n'a pas pu être récupérée.");
      }

      setStatusMessage({ text: `Portefeuille connecté (${shortKey(pubKey)}). En attente de la signature cryptographique...`, type: 'info' });

      const encodedMessage = new TextEncoder().encode(messageToSign);
      const signedData = await provider.signMessage(encodedMessage, 'utf8');

      const signatureBytes = signedData.signature || (signedData as unknown as Uint8Array);

      // Perform real cryptographic verification
      setStatusMessage({ text: 'Vérification cryptographique Ed25519 de la signature...', type: 'info' });
      const isValid = verifySolanaSignature(messageToSign, signatureBytes, pubKey);

      if (!isValid) {
        throw new Error('La signature cryptographique fournie est invalide ou ne correspond pas à la clé publique.');
      }

      // Encode signature to base64
      let binary = '';
      for (let i = 0; i < signatureBytes.length; i++) {
        binary += String.fromCharCode(signatureBytes[i]);
      }
      const signatureB64 = btoa(binary);

      const record: VerifiedWalletRecord = {
        discordUserId: discordId,
        publicKey: pubKey,
        verifiedAt: new Date().toISOString(),
        signature: signatureB64,
        algorithm: 'ed25519',
      };

      setVerifiedRecord(record);
      setStatusMessage({
        text: '✅ Portefeuille Solana vérifié avec succès par signature cryptographique !',
        type: 'success',
      });

      // Also try posting to backend if reachable
      try {
        await fetch('/api/wallet/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session: sessionToken,
            publicKey: pubKey,
            signature: signatureB64,
          }),
        });
      } catch {
        // Backend optional for client preview verification
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Échec de la signature ou annulation par l\'utilisateur.';
      setStatusMessage({ text: msg, type: 'error' });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 md:p-8 shadow-xl text-slate-100">
      <div className="flex items-center justify-between gap-4 pb-6 border-b border-slate-800">
        <div className="flex items-center gap-3">
          <div className="p-3 bg-indigo-500/10 border border-indigo-500/30 rounded-xl text-indigo-400">
            <ShieldCheck className="w-6 h-6" />
          </div>
          <div>
            <h2 className="text-xl font-bold text-white tracking-tight flex items-center gap-2">
              Vérification Portefeuille Solana
              <span className="text-xs px-2.5 py-0.5 rounded-full bg-indigo-950/80 border border-indigo-500/40 text-indigo-300 font-medium">
                Cryptographique
              </span>
            </h2>
            <p className="text-sm text-slate-400">
              Preuve de possession d'adresse Solana sans transfert ni divulgation de clé.
            </p>
          </div>
        </div>

        <button
          onClick={() => generateNewSession(discordId || '102938475610293847')}
          className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-white px-3 py-2 rounded-lg bg-slate-800/80 hover:bg-slate-800 transition"
          title="Générer un nouveau challenge"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Nouveau Nonce
        </button>
      </div>

      {/* Safety Alert */}
      <div className="my-5 p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs flex items-start gap-3">
        <AlertCircle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
        <div>
          <span className="font-semibold block mb-0.5">Règle absolue de sécurité :</span>
          NFT Market ne vous demandera <strong>JAMAIS</strong> votre seed phrase, votre clé privée ou votre mot de passe de wallet. La vérification s'effectue exclusivement par signature d'un message unique à usage unique (Ed25519).
        </div>
      </div>

      {/* Session Details */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
        <div className="p-4 rounded-xl bg-slate-950/60 border border-slate-800/80">
          <div className="text-xs text-slate-400 font-medium mb-1">Identifiant Discord cible</div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={discordId}
              onChange={(e) => setDiscordId(e.target.value)}
              placeholder="Ex: 349827419823749823"
              className="bg-transparent border border-slate-700/60 rounded-lg px-2.5 py-1.5 text-sm text-white font-mono w-full focus:outline-none focus:border-indigo-500"
            />
          </div>
          <span className="text-[11px] text-slate-500 mt-1 block">Fourni automatiquement via la commande /wallet</span>
        </div>

        <div className="p-4 rounded-xl bg-slate-950/60 border border-slate-800/80 flex flex-col justify-between">
          <div>
            <div className="text-xs text-slate-400 font-medium mb-1">Nonce unique (Anti-Replay)</div>
            <div className="font-mono text-sm text-indigo-300 truncate">
              {nonce || 'Génération...'}
            </div>
          </div>
          <div className="text-[11px] text-slate-500 flex items-center justify-between mt-2 pt-2 border-t border-slate-800/60">
            <span>Expiration : {expiresAt || '5 min'}</span>
            <span className="text-emerald-400 flex items-center gap-1 font-medium">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" /> Actif
            </span>
          </div>
        </div>
      </div>

      {/* Message to sign */}
      <div className="mb-6">
        <label className="text-xs font-semibold text-slate-300 block mb-2 flex items-center gap-1.5">
          <Lock className="w-3.5 h-3.5 text-indigo-400" />
          Message cryptographique officiel à signer :
        </label>
        <pre className="p-4 rounded-xl bg-slate-950 border border-slate-800 font-mono text-xs text-indigo-200/90 whitespace-pre-wrap leading-relaxed select-all">
          {messageToSign}
        </pre>
      </div>

      {/* Action Button */}
      <div className="flex flex-col sm:flex-row gap-3">
        <button
          onClick={handleConnectAndSign}
          disabled={loading}
          className="flex-1 flex items-center justify-center gap-2.5 py-3.5 px-6 rounded-xl bg-indigo-600 hover:bg-indigo-500 active:bg-indigo-700 disabled:opacity-50 text-white font-semibold shadow-lg shadow-indigo-600/25 transition cursor-pointer"
        >
          <Wallet className="w-5 h-5" />
          {loading ? 'Traitement en cours...' : 'Connecter et signer avec Phantom / Solana'}
        </button>

        <a
          href="https://phantom.app/"
          target="_blank"
          rel="noreferrer"
          className="flex items-center justify-center gap-1.5 py-3.5 px-4 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition"
        >
          Installer Phantom
          <ExternalLink className="w-3.5 h-3.5" />
        </a>
      </div>

      {/* Status feedback */}
      {statusMessage.text && (
        <div
          className={`mt-5 p-3.5 rounded-xl border text-sm flex items-center gap-2.5 ${
            statusMessage.type === 'success'
              ? 'bg-emerald-950/40 border-emerald-500/40 text-emerald-200'
              : statusMessage.type === 'error'
              ? 'bg-rose-950/40 border-rose-500/40 text-rose-200'
              : 'bg-slate-800/60 border-slate-700/60 text-slate-300'
          }`}
        >
          {statusMessage.type === 'success' ? (
            <CheckCircle2 className="w-5 h-5 text-emerald-400 shrink-0" />
          ) : statusMessage.type === 'error' ? (
            <AlertCircle className="w-5 h-5 text-rose-400 shrink-0" />
          ) : (
            <RefreshCw className="w-5 h-5 text-indigo-400 shrink-0 animate-spin" />
          )}
          <span>{statusMessage.text}</span>
        </div>
      )}

      {/* Verified Record Details */}
      {verifiedRecord && (
        <div className="mt-6 p-5 rounded-xl bg-emerald-950/30 border border-emerald-500/30">
          <div className="flex items-center gap-2 text-emerald-400 font-semibold mb-3">
            <KeyRound className="w-4 h-4" />
            Certificat de vérification Ed25519
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
            <div>
              <span className="text-slate-400 block mb-0.5">Clé publique Solana :</span>
              <span className="font-mono text-white bg-slate-900/90 px-2 py-1 rounded border border-slate-800 block truncate select-all">
                {verifiedRecord.publicKey}
              </span>
            </div>
            <div>
              <span className="text-slate-400 block mb-0.5">Discord User ID associé :</span>
              <span className="font-mono text-white bg-slate-900/90 px-2 py-1 rounded border border-slate-800 block select-all">
                {verifiedRecord.discordUserId}
              </span>
            </div>
            <div className="md:col-span-2">
              <span className="text-slate-400 block mb-0.5">Signature Ed25519 (Base64) :</span>
              <span className="font-mono text-slate-300 bg-slate-900/90 px-2 py-1 rounded border border-slate-800 block truncate select-all">
                {verifiedRecord.signature}
              </span>
            </div>
          </div>
          <p className="text-[11px] text-emerald-300/80 mt-3">
            Ce portefeuille est désormais certifié comme étant sous votre contrôle direct. Vous pouvez retourner sur Discord et taper <code className="bg-emerald-950 px-1 py-0.5 rounded text-emerald-200">/wallet-status</code> pour confirmer.
          </p>
        </div>
      )}
    </div>
  );
};

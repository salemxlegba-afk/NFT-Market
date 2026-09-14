import React from 'react';
import { Layers, CheckCircle2, CircleDashed, ArrowRight } from 'lucide-react';

const STEPS = [
  {
    step: '1',
    title: 'Vérification réelle du wallet Solana',
    desc: 'Signature cryptographique Ed25519 côté serveur/client avec Phantom/Solflare, sans jamais demander de clé privée.',
    status: 'completed',
  },
  {
    step: '2',
    title: 'NFT Market Pass & Possession On-Chain',
    desc: 'Contrat/Mint du Pass officiel et interdiction de simulation : vérification on-chain réelle de solde/possession via RPC Solana.',
    status: 'next',
  },
  {
    step: '3',
    title: 'Accès automatique Discord privé & Révocation',
    desc: 'Attribution automatique du rôle Discord exclusif et retrait instantané dès transfert ou vente du Pass.',
    status: 'pending',
  },
  {
    step: '4',
    title: 'Recherche réelle d\'acheteurs et vendeurs NFT',
    desc: 'Scan on-chain des détenteurs, distinction entre acheteur confirmé (fonds prouvés), offre active et acheteur potentiel.',
    status: 'pending',
  },
  {
    step: '5',
    title: 'Transactions directes & Commissions on-chain',
    desc: 'Escrow / transaction directe atomique peer-to-peer avec prélèvement de commission on-chain sans que le projet n\'avance de liquidité.',
    status: 'pending',
  },
];

export const RoadmapView: React.FC = () => {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 md:p-8 shadow-xl text-slate-100">
      <div className="flex items-center gap-3 pb-6 border-b border-slate-800">
        <div className="p-3 bg-indigo-500/10 border border-indigo-500/30 rounded-xl text-indigo-400">
          <Layers className="w-6 h-6" />
        </div>
        <div>
          <h2 className="text-xl font-bold text-white tracking-tight">Feuille de Route On-Chain</h2>
          <p className="text-sm text-slate-400">
            Déploiement progressif des briques blockchain sans simulation ni faux solde.
          </p>
        </div>
      </div>

      <div className="mt-6 space-y-3">
        {STEPS.map((item, idx) => (
          <div
            key={item.step}
            className={`p-4 rounded-xl border transition flex items-start gap-3.5 ${
              item.status === 'completed'
                ? 'bg-emerald-950/20 border-emerald-500/30'
                : item.status === 'next'
                ? 'bg-indigo-950/30 border-indigo-500/40 ring-1 ring-indigo-500/30'
                : 'bg-slate-950/40 border-slate-800/80 opacity-70'
            }`}
          >
            <div className="mt-0.5 shrink-0">
              {item.status === 'completed' ? (
                <CheckCircle2 className="w-5 h-5 text-emerald-400" />
              ) : item.status === 'next' ? (
                <div className="w-5 h-5 rounded-full border-2 border-indigo-400 flex items-center justify-center">
                  <div className="w-2 h-2 rounded-full bg-indigo-400 animate-ping" />
                </div>
              ) : (
                <CircleDashed className="w-5 h-5 text-slate-600" />
              )}
            </div>

            <div className="flex-1">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-mono font-bold text-slate-400">Étape {item.step}</span>
                <span className="font-semibold text-sm text-white">{item.title}</span>
                {item.status === 'completed' && (
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-950 border border-emerald-500/40 text-emerald-300 font-medium ml-auto">
                    Terminé
                  </span>
                )}
                {item.status === 'next' && (
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-950 border border-indigo-500/50 text-indigo-300 font-medium ml-auto flex items-center gap-1">
                    Prochaine étape <ArrowRight className="w-3 h-3" />
                  </span>
                )}
              </div>
              <p className="text-xs text-slate-400 leading-relaxed">{item.desc}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

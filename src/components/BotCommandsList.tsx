import React from 'react';
import { Terminal, Shield, ArrowRightLeft, HelpCircle } from 'lucide-react';
import type { BotCommandInfo } from '../types';

const COMMANDS: BotCommandInfo[] = [
  {
    command: '/wallet',
    description: 'Envoie un lien privé avec bouton pour connecter Phantom et signer la preuve Ed25519.',
    scope: 'DM / Serveur (Éphémère)',
    category: 'Wallet',
  },
  {
    command: '/wallet-status',
    description: 'Affiche si votre wallet est vérifié, son adresse publique raccourcie et la date de validation.',
    scope: 'Serveur (Éphémère)',
    category: 'Wallet',
  },
  {
    command: '/wallet-remove',
    description: 'Dissocie immédiatement votre adresse Solana de votre compte Discord.',
    scope: 'Serveur (Éphémère)',
    category: 'Wallet',
  },
  {
    command: '/sell',
    description: 'Ouvre le formulaire pour inscrire un NFT à vendre (collection, mint optionnel, prix SOL, traits).',
    scope: 'Serveur uniquement',
    category: 'Market',
  },
  {
    command: '/buy',
    description: 'Ouvre le formulaire pour rechercher un NFT (collection, mint optionnel, budget max SOL, traits).',
    scope: 'Serveur uniquement',
    category: 'Market',
  },
  {
    command: '/matches',
    description: 'Déclenche une réconciliation et liste vos matchs privés avec lien vers les salons deal-*.',
    scope: 'Serveur (Éphémère)',
    category: 'Market',
  },
  {
    command: '/cancel',
    description: 'Annule votre offre d\'achat, votre offre de vente ou les deux.',
    scope: 'Serveur uniquement',
    category: 'Market',
  },
  {
    command: '/help',
    description: 'Présente la liste des commandes et le fonctionnement du marché privé.',
    scope: 'Global',
    category: 'Utility',
  },
  {
    command: '/ping',
    description: 'Vérifie la latence de réponse du bot Discord.',
    scope: 'Global',
    category: 'Utility',
  },
];

export const BotCommandsList: React.FC = () => {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 md:p-8 shadow-xl text-slate-100">
      <div className="flex items-center gap-3 pb-6 border-b border-slate-800">
        <div className="p-3 bg-indigo-500/10 border border-indigo-500/30 rounded-xl text-indigo-400">
          <Terminal className="w-6 h-6" />
        </div>
        <div>
          <h2 className="text-xl font-bold text-white tracking-tight">Commandes Discord Actives</h2>
          <p className="text-sm text-slate-400">
            Toutes les fonctionnalités existantes conservées et synchronisées dans <code className="text-indigo-300 font-mono">main.py</code>.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5 mt-6">
        {COMMANDS.map((cmd) => (
          <div
            key={cmd.command}
            className="p-4 rounded-xl bg-slate-950/70 border border-slate-800/80 hover:border-slate-700 transition"
          >
            <div className="flex items-center justify-between gap-2 mb-2">
              <span className="font-mono text-sm font-bold text-indigo-300 bg-indigo-950/60 px-2 py-0.5 rounded border border-indigo-500/30">
                {cmd.command}
              </span>
              <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700/60">
                {cmd.category}
              </span>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed min-h-[36px]">
              {cmd.description}
            </p>
            <div className="text-[11px] text-slate-500 mt-2.5 pt-2 border-t border-slate-800/60 flex items-center gap-1">
              <span>Portée :</span>
              <span className="text-slate-400 font-medium">{cmd.scope}</span>
            </div>
          </div>
        ))}
      </div>

      {/* Matching & Deal Channel Info */}
      <div className="mt-6 p-4 rounded-xl bg-slate-950/50 border border-slate-800 text-xs text-slate-400 flex flex-col md:flex-row items-start md:items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-slate-300">
          <ArrowRightLeft className="w-4 h-4 text-indigo-400" />
          <span>Salons privés : Catégorie <strong>🔐 PRIVATE MARKET</strong> &bull; Salons temporaires <strong>deal-[id]</strong> (TTL 24h)</span>
        </div>
        <div className="flex items-center gap-2 text-slate-300">
          <Shield className="w-4 h-4 text-emerald-400" />
          <span>Isolation stricte des permissions vendeur/acheteur</span>
        </div>
      </div>
    </div>
  );
};

import React, { useState } from 'react';
import { WalletVerificationPortal } from './components/WalletVerificationPortal';
import { BotCommandsList } from './components/BotCommandsList';
import { RoadmapView } from './components/RoadmapView';
import { ShieldCheck, Terminal, Layers, ExternalLink } from 'lucide-react';

export default function App() {
  const [activeTab, setActiveTab] = useState<'verify' | 'commands' | 'roadmap'>('verify');

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col antialiased selection:bg-indigo-500 selection:text-white">
      {/* Header */}
      <header className="border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-md sticky top-0 z-40">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center font-bold text-white shadow-md shadow-indigo-500/20">
              NM
            </div>
            <div>
              <span className="font-bold text-base tracking-tight text-white flex items-center gap-2">
                NFT Market
                <span className="text-[11px] font-mono px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 border border-slate-700">
                  Solana Verified
                </span>
              </span>
            </div>
          </div>

          <nav className="flex items-center gap-1 sm:gap-2">
            <button
              onClick={() => setActiveTab('verify')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                activeTab === 'verify'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-white hover:bg-slate-800/60'
              }`}
            >
              <ShieldCheck className="w-4 h-4" />
              <span className="hidden sm:inline">Vérification</span> Wallet
            </button>

            <button
              onClick={() => setActiveTab('commands')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                activeTab === 'commands'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-white hover:bg-slate-800/60'
              }`}
            >
              <Terminal className="w-4 h-4" />
              Commandes Bot
            </button>

            <button
              onClick={() => setActiveTab('roadmap')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                activeTab === 'roadmap'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-white hover:bg-slate-800/60'
              }`}
            >
              <Layers className="w-4 h-4" />
              Feuille de route
            </button>
          </nav>
        </div>
      </header>

      {/* Main Container */}
      <main className="flex-1 max-w-6xl w-full mx-auto px-4 sm:px-6 py-8">
        {activeTab === 'verify' && <WalletVerificationPortal />}
        {activeTab === 'commands' && <BotCommandsList />}
        {activeTab === 'roadmap' && <RoadmapView />}
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-800/60 bg-slate-900/30 py-6 text-center text-xs text-slate-500">
        <div className="max-w-6xl mx-auto px-4 flex flex-col sm:flex-row items-center justify-between gap-3">
          <div>NFT Market &bull; Protocole de vérification cryptographique Ed25519 & Discord Matching Bot</div>
          <div className="flex items-center gap-4 text-slate-400">
            <span>Aucune simulation blockchain</span>
            <span>&bull;</span>
            <span className="text-emerald-400 font-medium">Clés privées strictement préservées</span>
          </div>
        </div>
      </footer>
    </div>
  );
}

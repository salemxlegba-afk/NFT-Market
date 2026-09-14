-- ==============================================================================
-- SCHEMA SUPABASE POUR NFT MARKET BOT (PostgreSQL 15+)
-- ==============================================================================
-- Ce fichier initialise toutes les tables, index et politiques RLS nécessaires
-- pour stocker les annonces (/sell, /buy), les matches (/matches),
-- les sessions de vérification de wallet et les adresses Solana vérifiées.
-- ==============================================================================

-- 1. Table des annonces de marché (Listings Achat / Vente)
CREATE TABLE IF NOT EXISTS public.market_listings (
    listing_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('sell', 'buy')),
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    collection TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'SOL',
    amount NUMERIC(18, 4) NOT NULL CHECK (amount > 0),
    details TEXT NOT NULL DEFAULT '',
    mint_address TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index pour accélérer le matching et les filtres par serveur / collection
CREATE INDEX IF NOT EXISTS idx_market_listings_match 
    ON public.market_listings (guild_id, collection, kind, active, amount);

CREATE INDEX IF NOT EXISTS idx_market_listings_user 
    ON public.market_listings (guild_id, user_id, active);

-- 2. Table des matches (Deals privés entre acheteur et vendeur)
CREATE TABLE IF NOT EXISTS public.market_matches (
    match_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    seller_listing_id TEXT NOT NULL REFERENCES public.market_listings(listing_id) ON DELETE CASCADE,
    buyer_listing_id TEXT NOT NULL REFERENCES public.market_listings(listing_id) ON DELETE CASCADE,
    channel_id BIGINT,
    channel_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_market_matches_guild 
    ON public.market_matches (guild_id, created_at DESC);

-- 3. Table des sessions de vérification de wallet Solana
CREATE TABLE IF NOT EXISTS public.wallet_sessions (
    token TEXT PRIMARY KEY,
    discord_user_id BIGINT NOT NULL,
    nonce TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    used BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_wallet_sessions_user 
    ON public.wallet_sessions (discord_user_id, expires_at);

-- 4. Table des associations de wallets Solana vérifiés (Cryptographiquement via Ed25519)
CREATE TABLE IF NOT EXISTS public.wallet_associations (
    discord_user_id BIGINT PRIMARY KEY,
    public_key TEXT NOT NULL UNIQUE,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallet_associations_pubkey 
    ON public.wallet_associations (public_key);

-- ==============================================================================
-- SÉCURITÉ ROW LEVEL SECURITY (RLS)
-- ==============================================================================
-- La clé de service du bot (service_role) contourne RLS et a un accès complet sécurisé.
-- La clé anonyme (anon) côté frontend Netlify n'a accès qu'en lecture limitée
-- ou via des fonctions serverless dédiées.

ALTER TABLE public.market_listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_matches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wallet_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wallet_associations ENABLE ROW LEVEL SECURITY;

-- Politique de lecture publique des annonces actives (utile pour une vitrine web future)
CREATE POLICY "Public read active listings" 
    ON public.market_listings FOR SELECT 
    USING (active = true);

-- Les sessions et wallets sont protégés : seuls le bot et la fonction serverless
-- (utilisant la clé secrète service_role) peuvent écrire ou lire.
CREATE POLICY "Service role full access on market_listings" 
    ON public.market_listings FOR ALL 
    USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access on market_matches" 
    ON public.market_matches FOR ALL 
    USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access on wallet_sessions" 
    ON public.wallet_sessions FOR ALL 
    USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access on wallet_associations" 
    ON public.wallet_associations FOR ALL 
    USING (auth.role() = 'service_role');

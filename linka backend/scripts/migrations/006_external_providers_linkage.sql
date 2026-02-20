-- ========================================================
-- Migration 006: External Provider Linkage & KYC Tracking
-- Supports: MTN, Airtel, Bank accounts, unified balance
-- ========================================================

-- ---------- external_accounts ----------
-- Links user wallets to external providers (MTN, Airtel, banks)
CREATE TABLE IF NOT EXISTS public.external_accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    provider_type   TEXT NOT NULL CHECK (provider_type IN ('mtn','airtel','zamtel','bank','other')),
    provider_name   TEXT NOT NULL,
    account_identifier TEXT NOT NULL,  -- phone number or account number
    account_name    TEXT,
    currency        TEXT NOT NULL DEFAULT 'ZMW',
    is_verified     BOOLEAN NOT NULL DEFAULT false,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_synced_at  TIMESTAMPTZ,
    cached_balance  NUMERIC(15,2),     -- cache external balance for aggregation
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, provider_type, account_identifier)
);

CREATE INDEX IF NOT EXISTS idx_ext_accounts_user     ON public.external_accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_ext_accounts_provider ON public.external_accounts(provider_type);
CREATE INDEX IF NOT EXISTS idx_ext_accounts_active   ON public.external_accounts(is_active) WHERE is_active = true;

-- ---------- kyc_verifications ----------
-- Track Bank of Zambia KYC compliance levels
CREATE TABLE IF NOT EXISTS public.kyc_verifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    kyc_level       INTEGER NOT NULL DEFAULT 1 CHECK (kyc_level BETWEEN 1 AND 3),
    verification_method TEXT NOT NULL CHECK (verification_method IN ('smile_id','manual','partner_api')),
    verification_ref TEXT,           -- external verification reference
    verification_data JSONB,         -- documents, images, etc.
    verified_by     UUID REFERENCES public.user_profiles(id),
    verified_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,     -- KYC may need renewal
    status          TEXT NOT NULL DEFAULT 'pending' 
                        CHECK (status IN ('pending','approved','rejected','expired')),
    rejection_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kyc_user   ON public.kyc_verifications(user_id);
CREATE INDEX IF NOT EXISTS idx_kyc_status ON public.kyc_verifications(status);
CREATE INDEX IF NOT EXISTS idx_kyc_level  ON public.kyc_verifications(kyc_level);

-- ---------- provider_transactions ----------
-- Log all interactions with external providers for audit/reconciliation
CREATE TABLE IF NOT EXISTS public.provider_transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_account_id UUID REFERENCES public.external_accounts(id) ON DELETE SET NULL,
    user_id             UUID NOT NULL REFERENCES public.user_profiles(id),
    wallet_tx_id        UUID REFERENCES public.wallet_transactions(id),
    provider_type       TEXT NOT NULL,
    provider_ref        TEXT NOT NULL,
    operation_type      TEXT NOT NULL CHECK (operation_type IN ('deposit','withdrawal','balance_check','verify')),
    amount              NUMERIC(15,2),
    currency            TEXT DEFAULT 'ZMW',
    status              TEXT NOT NULL DEFAULT 'initiated'
                            CHECK (status IN ('initiated','processing','completed','failed','timeout')),
    request_payload     JSONB,
    response_payload    JSONB,
    error_message       TEXT,
    retry_count         INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_prov_tx_user     ON public.provider_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_prov_tx_external ON public.provider_transactions(external_account_id);
CREATE INDEX IF NOT EXISTS idx_prov_tx_status   ON public.provider_transactions(status);
CREATE INDEX IF NOT EXISTS idx_prov_tx_ref      ON public.provider_transactions(provider_ref);
CREATE INDEX IF NOT EXISTS idx_prov_tx_created  ON public.provider_transactions(created_at);

-- ---------- balance_snapshots ----------
-- Periodic snapshot of total balance (internal + external) for analytics
CREATE TABLE IF NOT EXISTS public.balance_snapshots (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    snapshot_date       DATE NOT NULL DEFAULT CURRENT_DATE,
    internal_balance    NUMERIC(15,2) NOT NULL,
    external_balance    NUMERIC(15,2) DEFAULT 0,
    total_balance       NUMERIC(15,2) NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'ZMW',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_user ON public.balance_snapshots(user_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON public.balance_snapshots(snapshot_date);

-- ---------- RLS ----------
ALTER TABLE public.external_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.kyc_verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.provider_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.balance_snapshots ENABLE ROW LEVEL SECURITY;

-- external_accounts: owner or admin
CREATE POLICY "ext_accounts_owner_select"
    ON public.external_accounts FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "ext_accounts_admin_select"
    ON public.external_accounts FOR SELECT
    USING (EXISTS (SELECT 1 FROM public.user_profiles WHERE id = auth.uid() AND role = 'admin'));

-- kyc_verifications: owner or admin
CREATE POLICY "kyc_owner_select"
    ON public.kyc_verifications FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "kyc_admin_all"
    ON public.kyc_verifications FOR ALL
    USING (EXISTS (SELECT 1 FROM public.user_profiles WHERE id = auth.uid() AND role = 'admin'));

-- provider_transactions: owner or admin
CREATE POLICY "prov_tx_owner_select"
    ON public.provider_transactions FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "prov_tx_admin_select"
    ON public.provider_transactions FOR SELECT
    USING (EXISTS (SELECT 1 FROM public.user_profiles WHERE id = auth.uid() AND role = 'admin'));

-- balance_snapshots: owner or admin
CREATE POLICY "snapshots_owner_select"
    ON public.balance_snapshots FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "snapshots_admin_select"
    ON public.balance_snapshots FOR SELECT
    USING (EXISTS (SELECT 1 FROM public.user_profiles WHERE id = auth.uid() AND role = 'admin'));

-- Service role can insert/update
CREATE POLICY "ext_accounts_service"
    ON public.external_accounts FOR ALL
    USING (true);

CREATE POLICY "kyc_service"
    ON public.kyc_verifications FOR ALL
    USING (true);

CREATE POLICY "prov_tx_service"
    ON public.provider_transactions FOR ALL
    USING (true);

CREATE POLICY "snapshots_service"
    ON public.balance_snapshots FOR ALL
    USING (true);

-- ---------- auto-updated_at ----------
CREATE TRIGGER update_external_accounts_updated_at
    BEFORE UPDATE ON public.external_accounts
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER update_kyc_verifications_updated_at
    BEFORE UPDATE ON public.kyc_verifications
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

-- ---------- RPC: get_unified_balance ----------
-- Returns internal + all external accounts' cached balances
CREATE OR REPLACE FUNCTION public.get_unified_balance(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_internal NUMERIC;
    v_external NUMERIC;
    v_accounts JSONB;
BEGIN
    -- Internal wallet balance
    SELECT COALESCE(balance, 0) INTO v_internal
    FROM wallets
    WHERE user_id = p_user_id AND status = 'active';

    -- Sum of external cached balances
    SELECT COALESCE(SUM(cached_balance), 0) INTO v_external
    FROM external_accounts
    WHERE user_id = p_user_id AND is_active = true;

    -- Build account details
    SELECT json_agg(json_build_object(
        'provider_type', provider_type,
        'provider_name', provider_name,
        'account_identifier', account_identifier,
        'balance', cached_balance,
        'last_synced', last_synced_at
    )) INTO v_accounts
    FROM external_accounts
    WHERE user_id = p_user_id AND is_active = true;

    RETURN jsonb_build_object(
        'internal_balance', v_internal,
        'external_balance', v_external,
        'total_balance', v_internal + v_external,
        'currency', 'ZMW',
        'external_accounts', COALESCE(v_accounts, '[]'::json)
    );
END;
$$;

-- ---------- RPC: check_kyc_level ----------
-- Returns current KYC level for a user
CREATE OR REPLACE FUNCTION public.check_kyc_level(p_user_id UUID)
RETURNS INTEGER
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(MAX(kyc_level), 1)
    FROM kyc_verifications
    WHERE user_id = p_user_id
      AND status = 'approved'
      AND (expires_at IS NULL OR expires_at > NOW());
$$;

-- ---------- RPC: update_external_balance_cache ----------
-- Update cached balance for an external account
CREATE OR REPLACE FUNCTION public.update_external_balance_cache(
    p_account_id UUID,
    p_balance NUMERIC
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE external_accounts
    SET cached_balance = p_balance,
        last_synced_at = NOW()
    WHERE id = p_account_id;
    
    RETURN FOUND;
END;
$$;

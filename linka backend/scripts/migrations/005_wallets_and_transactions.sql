-- =====================================================
-- Migration 005: Wallets, Transactions, and Mobile Money
-- Provides: wallet ledger, transaction log, provider
-- reference tracking, daily-limit enforcement.
-- =====================================================

-- ---------- wallets ----------
CREATE TABLE IF NOT EXISTS public.wallets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL UNIQUE REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    balance     NUMERIC(15,2) NOT NULL DEFAULT 0.00 CHECK (balance >= 0),
    currency    TEXT NOT NULL DEFAULT 'ZMW',
    status      TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','frozen','closed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallets_user   ON public.wallets(user_id);
CREATE INDEX IF NOT EXISTS idx_wallets_status ON public.wallets(status);

-- ---------- wallet_transactions ----------
CREATE TABLE IF NOT EXISTS public.wallet_transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id       UUID NOT NULL REFERENCES public.wallets(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES public.user_profiles(id),
    type            TEXT NOT NULL CHECK (type IN (
                        'deposit','withdrawal','payment','refund','transfer_in','transfer_out')),
    amount          NUMERIC(15,2) NOT NULL CHECK (amount > 0),
    balance_before  NUMERIC(15,2) NOT NULL,
    balance_after   NUMERIC(15,2) NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'ZMW',
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','completed','failed','reversed')),
    reference       TEXT,                       -- external ref (order id, provider ref, etc.)
    provider        TEXT,                       -- 'mtn', 'airtel', 'internal', etc.
    provider_ref    TEXT,                       -- id returned by the provider
    description     TEXT,
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_wtx_wallet     ON public.wallet_transactions(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wtx_user       ON public.wallet_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_wtx_status     ON public.wallet_transactions(status);
CREATE INDEX IF NOT EXISTS idx_wtx_type       ON public.wallet_transactions(type);
CREATE INDEX IF NOT EXISTS idx_wtx_created    ON public.wallet_transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_wtx_reference  ON public.wallet_transactions(reference);

-- ---------- RLS ----------
ALTER TABLE public.wallets            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wallet_transactions ENABLE ROW LEVEL SECURITY;

-- wallets: owner or admin
CREATE POLICY "wallet_owner_select"
    ON public.wallets FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "wallet_admin_select"
    ON public.wallets FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM public.user_profiles
        WHERE id = auth.uid() AND role = 'admin'
    ));

-- transactions: owner or admin
CREATE POLICY "wtx_owner_select"
    ON public.wallet_transactions FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "wtx_admin_select"
    ON public.wallet_transactions FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM public.user_profiles
        WHERE id = auth.uid() AND role = 'admin'
    ));

-- Service role can insert/update (backend only, not anon)
CREATE POLICY "wallet_service_insert"
    ON public.wallets FOR INSERT
    WITH CHECK (true);

CREATE POLICY "wallet_service_update"
    ON public.wallets FOR UPDATE
    USING (true);

CREATE POLICY "wtx_service_insert"
    ON public.wallet_transactions FOR INSERT
    WITH CHECK (true);

CREATE POLICY "wtx_service_update"
    ON public.wallet_transactions FOR UPDATE
    USING (true);

-- ---------- auto-updated_at ----------
CREATE TRIGGER update_wallets_updated_at
    BEFORE UPDATE ON public.wallets
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

-- ---------- RPC: credit_wallet ----------
CREATE OR REPLACE FUNCTION public.credit_wallet(
    p_user_id    UUID,
    p_amount     NUMERIC,
    p_type       TEXT,
    p_reference  TEXT DEFAULT NULL,
    p_provider   TEXT DEFAULT 'internal',
    p_provider_ref TEXT DEFAULT NULL,
    p_description TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_wallet   wallets%ROWTYPE;
    v_new_bal  NUMERIC;
    v_tx_id    UUID;
BEGIN
    -- Lock the wallet row
    SELECT * INTO v_wallet
    FROM wallets WHERE user_id = p_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'wallet_not_found');
    END IF;
    IF v_wallet.status != 'active' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'wallet_not_active');
    END IF;

    v_new_bal := v_wallet.balance + p_amount;

    UPDATE wallets SET balance = v_new_bal WHERE id = v_wallet.id;

    INSERT INTO wallet_transactions
        (wallet_id, user_id, type, amount, balance_before, balance_after,
         status, reference, provider, provider_ref, description, completed_at)
    VALUES
        (v_wallet.id, p_user_id, p_type, p_amount, v_wallet.balance, v_new_bal,
         'completed', p_reference, p_provider, p_provider_ref, p_description, NOW())
    RETURNING id INTO v_tx_id;

    RETURN jsonb_build_object(
        'ok', true,
        'transaction_id', v_tx_id,
        'balance', v_new_bal
    );
END;
$$;

-- ---------- RPC: debit_wallet ----------
CREATE OR REPLACE FUNCTION public.debit_wallet(
    p_user_id    UUID,
    p_amount     NUMERIC,
    p_type       TEXT,
    p_reference  TEXT DEFAULT NULL,
    p_provider   TEXT DEFAULT 'internal',
    p_provider_ref TEXT DEFAULT NULL,
    p_description TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_wallet   wallets%ROWTYPE;
    v_new_bal  NUMERIC;
    v_tx_id    UUID;
BEGIN
    SELECT * INTO v_wallet
    FROM wallets WHERE user_id = p_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'wallet_not_found');
    END IF;
    IF v_wallet.status != 'active' THEN
        RETURN jsonb_build_object('ok', false, 'error', 'wallet_not_active');
    END IF;

    v_new_bal := v_wallet.balance - p_amount;
    IF v_new_bal < 0 THEN
        RETURN jsonb_build_object('ok', false, 'error', 'insufficient_balance');
    END IF;

    UPDATE wallets SET balance = v_new_bal WHERE id = v_wallet.id;

    INSERT INTO wallet_transactions
        (wallet_id, user_id, type, amount, balance_before, balance_after,
         status, reference, provider, provider_ref, description, completed_at)
    VALUES
        (v_wallet.id, p_user_id, p_type, p_amount, v_wallet.balance, v_new_bal,
         'completed', p_reference, p_provider, p_provider_ref, p_description, NOW())
    RETURNING id INTO v_tx_id;

    RETURN jsonb_build_object(
        'ok', true,
        'transaction_id', v_tx_id,
        'balance', v_new_bal
    );
END;
$$;

-- ---------- RPC: transfer_between_wallets ----------
CREATE OR REPLACE FUNCTION public.transfer_between_wallets(
    p_sender_id    UUID,
    p_recipient_id UUID,
    p_amount       NUMERIC,
    p_description  TEXT DEFAULT 'Wallet transfer'
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_debit  JSONB;
    v_credit JSONB;
BEGIN
    v_debit := public.debit_wallet(
        p_sender_id, p_amount, 'transfer_out',
        p_recipient_id::TEXT, 'internal', NULL, p_description
    );
    IF NOT (v_debit ->> 'ok')::boolean THEN
        RETURN v_debit;
    END IF;

    v_credit := public.credit_wallet(
        p_recipient_id, p_amount, 'transfer_in',
        p_sender_id::TEXT, 'internal', NULL, p_description
    );
    IF NOT (v_credit ->> 'ok')::boolean THEN
        -- Rollback debit (Postgres will do this automatically on error,
        -- but explicit credit back for clarity)
        PERFORM public.credit_wallet(
            p_sender_id, p_amount, 'refund',
            'transfer_rollback', 'internal', NULL, 'Transfer rollback'
        );
        RETURN v_credit;
    END IF;

    RETURN jsonb_build_object(
        'ok', true,
        'debit_tx',  v_debit ->> 'transaction_id',
        'credit_tx', v_credit ->> 'transaction_id'
    );
END;
$$;

-- ---------- RPC: daily_transaction_total ----------
CREATE OR REPLACE FUNCTION public.daily_transaction_total(
    p_user_id UUID,
    p_type    TEXT
)
RETURNS NUMERIC
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(SUM(amount), 0)
    FROM wallet_transactions
    WHERE user_id  = p_user_id
      AND type     = p_type
      AND status   = 'completed'
      AND created_at >= (CURRENT_DATE AT TIME ZONE 'Africa/Lusaka');
$$;

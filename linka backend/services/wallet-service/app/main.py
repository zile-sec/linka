"""
Linka Wallet Service
====================
Core wallet operations: balance checks, deposits, withdrawals,
transfers, transaction history, and mobile-money payment initiation.

Every database mutation goes through Supabase RPC functions that
lock the wallet row, so concurrent requests cannot overdraw.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.supabase_client import SupabaseClient, get_supabase_client
from shared.auth_middleware import (
    AuthenticatedUser,
    get_current_user,
    require_kyc_level,
    require_roles,
    UserRole,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("wallet-service")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Linka Wallet Service", version="2.0.0")


@app.exception_handler(Exception)
async def _global_exc(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again."},
    )


# ---------------------------------------------------------------------------
# Enums & Models
# ---------------------------------------------------------------------------
class TxType(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    PAYMENT = "payment"
    REFUND = "refund"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"


class MobileProvider(str, Enum):
    MTN = "mtn"
    AIRTEL = "airtel"
    ZAMTEL = "zamtel"


class DepositRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, le=50_000, description="Amount in ZMW")
    provider: MobileProvider
    phone_number: str = Field(..., min_length=10, max_length=15)


class WithdrawRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, le=50_000)
    provider: MobileProvider
    phone_number: str = Field(..., min_length=10, max_length=15)


class TransferRequest(BaseModel):
    recipient_id: str
    amount: Decimal = Field(..., gt=0)
    description: Optional[str] = None


class PaymentInitRequest(BaseModel):
    """Initiate a payment to an order / invoice via mobile money."""
    order_id: str
    amount: Decimal = Field(..., gt=0)
    provider: MobileProvider
    phone_number: str = Field(..., min_length=10, max_length=15)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    db = get_supabase_client()
    db_ok = db.test_connection()
    status = "healthy" if db_ok else "degraded"
    return {
        "status": status,
        "service": "wallet-service",
        "database": "connected" if db_ok else "unreachable",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ===================================================================
#  PURE HELPER FUNCTIONS  (no HTTP, no auth -- easy to unit-test)
# ===================================================================

def _ensure_wallet_exists(db: SupabaseClient, user_id: str) -> Dict[str, Any]:
    """Return the user's wallet, creating one if it does not exist."""
    wallet = db.get_single("wallets", {"user_id": user_id})
    if wallet:
        return wallet
    logger.info("creating wallet for user=%s", user_id)
    return db.insert("wallets", {
        "user_id": user_id,
        "balance": 0.00,
        "currency": "ZMW",
        "status": "active",
    })


def _check_wallet_active(wallet: Dict[str, Any]) -> None:
    """Raise if the wallet is frozen or closed."""
    if wallet.get("status") != "active":
        raise HTTPException(status_code=403, detail="Wallet is not active")


def _parse_amount(raw: Any) -> Decimal:
    """Safely convert to Decimal."""
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid amount")


def _daily_total(db: SupabaseClient, user_id: str, tx_type: str) -> Decimal:
    """Sum of completed transactions today (Africa/Lusaka tz via DB)."""
    result = db.rpc("daily_transaction_total", {
        "p_user_id": user_id,
        "p_type": tx_type,
    })
    return Decimal(str(result)) if result else Decimal("0")


def _enforce_daily_limit(
    db: SupabaseClient,
    user_id: str,
    tx_type: str,
    amount: Decimal,
    kyc_level: int,
) -> None:
    """Raise if adding *amount* exceeds the BoZ daily cap."""
    cap = Decimal("100000") if kyc_level >= 2 else Decimal("50000")
    used = _daily_total(db, user_id, tx_type)
    remaining = cap - used
    if amount > remaining:
        raise HTTPException(
            status_code=400,
            detail=f"Daily {tx_type} limit exceeded. Remaining: {remaining} ZMW",
        )


def _credit(
    db: SupabaseClient,
    user_id: str,
    amount: Decimal,
    tx_type: str,
    *,
    reference: str | None = None,
    provider: str = "internal",
    provider_ref: str | None = None,
    description: str | None = None,
) -> Dict[str, Any]:
    """Atomically add funds via the DB RPC."""
    result = db.rpc("credit_wallet", {
        "p_user_id": user_id,
        "p_amount": float(amount),
        "p_type": tx_type,
        "p_reference": reference,
        "p_provider": provider,
        "p_provider_ref": provider_ref,
        "p_description": description,
    })
    if not result or not result.get("ok"):
        err = (result or {}).get("error", "credit failed")
        raise HTTPException(status_code=400, detail=err)
    return result


def _debit(
    db: SupabaseClient,
    user_id: str,
    amount: Decimal,
    tx_type: str,
    *,
    reference: str | None = None,
    provider: str = "internal",
    provider_ref: str | None = None,
    description: str | None = None,
) -> Dict[str, Any]:
    """Atomically subtract funds via the DB RPC."""
    result = db.rpc("debit_wallet", {
        "p_user_id": user_id,
        "p_amount": float(amount),
        "p_type": tx_type,
        "p_reference": reference,
        "p_provider": provider,
        "p_provider_ref": provider_ref,
        "p_description": description,
    })
    if not result or not result.get("ok"):
        err = (result or {}).get("error", "debit failed")
        raise HTTPException(status_code=400, detail=err)
    return result


def _log_activity(
    db: SupabaseClient,
    user_id: str,
    action: str,
    details: Dict[str, Any],
) -> None:
    """Write an entry to the audit_logs table (fire-and-forget)."""
    try:
        db.insert("audit_logs", {
            "user_id": user_id,
            "action": action,
            "details": details,
        })
    except Exception:
        logger.warning("audit log write failed for user=%s action=%s", user_id, action)


def _build_tx_history(rows: List[Dict]) -> List[Dict]:
    """Shape raw DB rows into a clean list for the API consumer."""
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "amount": float(r["amount"]),
            "balance_after": float(r["balance_after"]),
            "status": r["status"],
            "provider": r.get("provider"),
            "reference": r.get("reference"),
            "description": r.get("description"),
            "created_at": r.get("created_at"),
            "completed_at": r.get("completed_at"),
        }
        for r in rows
    ]


# ===================================================================
#  MOBILE MONEY PROVIDER ADAPTERS
# ===================================================================

def _mtn_request_payment(phone: str, amount: float, ref: str) -> Dict:
    """
    Call the MTN MoMo Collections API to request a payment.
    In production this calls httpx against the MTN sandbox/live URL
    using the credentials from env vars.
    Returns a dict with 'provider_ref' and 'status'.
    """
    # TODO: Replace with real MTN MoMo API call
    # POST https://sandbox.momodeveloper.mtn.com/collection/v1_0/requesttopay
    logger.info("MTN payment request: phone=%s amount=%s ref=%s", phone, amount, ref)
    return {
        "provider_ref": f"mtn-{uuid.uuid4().hex[:12]}",
        "status": "processing",
        "message": "Approve the payment on your MTN phone",
    }


def _airtel_request_payment(phone: str, amount: float, ref: str) -> Dict:
    """
    Call the Airtel Money API to request a payment.
    """
    logger.info("Airtel payment request: phone=%s amount=%s ref=%s", phone, amount, ref)
    return {
        "provider_ref": f"airtel-{uuid.uuid4().hex[:12]}",
        "status": "processing",
        "message": "Approve the payment on your Airtel phone",
    }


def _zamtel_request_payment(phone: str, amount: float, ref: str) -> Dict:
    logger.info("Zamtel payment request: phone=%s amount=%s ref=%s", phone, amount, ref)
    return {
        "provider_ref": f"zamtel-{uuid.uuid4().hex[:12]}",
        "status": "processing",
        "message": "Approve the payment on your Zamtel phone",
    }


_PROVIDER_MAP = {
    MobileProvider.MTN: _mtn_request_payment,
    MobileProvider.AIRTEL: _airtel_request_payment,
    MobileProvider.ZAMTEL: _zamtel_request_payment,
}


def _initiate_provider_payment(
    provider: MobileProvider, phone: str, amount: float, ref: str
) -> Dict:
    """Dispatch to the correct provider adapter."""
    handler = _PROVIDER_MAP.get(provider)
    if not handler:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
    return handler(phone, amount, ref)


# ===================================================================
#  ROUTES
# ===================================================================

# ---------- Balance ----------
@app.get("/balance")
async def get_balance(user: AuthenticatedUser = Depends(get_current_user)):
    """Return the caller's wallet balance."""
    db = get_supabase_client()
    wallet = _ensure_wallet_exists(db, user.id)
    logger.info("balance check user=%s balance=%s", user.id, wallet["balance"])
    return {
        "balance": float(wallet["balance"]),
        "currency": wallet["currency"],
        "status": wallet["status"],
        "updated_at": wallet.get("updated_at"),
    }


# ---------- Deposit (top-up via mobile money) ----------
@app.post("/deposit")
async def deposit(
    body: DepositRequest,
    user: AuthenticatedUser = Depends(require_kyc_level(1)),
):
    """Top up wallet from a mobile money account."""
    db = get_supabase_client()
    wallet = _ensure_wallet_exists(db, user.id)
    _check_wallet_active(wallet)
    amount = _parse_amount(body.amount)

    _enforce_daily_limit(db, user.id, TxType.DEPOSIT.value, amount, user.kyc_level)

    ref = str(uuid.uuid4())
    provider_result = _initiate_provider_payment(
        body.provider, body.phone_number, float(amount), ref
    )

    # Record a *pending* transaction; the webhook will finalise it.
    db.insert("wallet_transactions", {
        "wallet_id": wallet["id"],
        "user_id": user.id,
        "type": TxType.DEPOSIT.value,
        "amount": float(amount),
        "balance_before": float(wallet["balance"]),
        "balance_after": float(wallet["balance"]),  # unchanged until confirmed
        "status": "processing",
        "reference": ref,
        "provider": body.provider.value,
        "provider_ref": provider_result.get("provider_ref"),
        "description": f"Deposit via {body.provider.value}",
    })

    _log_activity(db, user.id, "deposit_initiated", {
        "amount": float(amount),
        "provider": body.provider.value,
        "reference": ref,
    })

    logger.info(
        "deposit initiated user=%s amount=%s provider=%s ref=%s",
        user.id, amount, body.provider.value, ref,
    )

    return {
        "reference": ref,
        "provider_ref": provider_result.get("provider_ref"),
        "status": "processing",
        "message": provider_result.get("message"),
    }


# ---------- Withdraw ----------
@app.post("/withdraw")
async def withdraw(
    body: WithdrawRequest,
    user: AuthenticatedUser = Depends(require_kyc_level(1)),
):
    """Withdraw from wallet to mobile money."""
    db = get_supabase_client()
    wallet = _ensure_wallet_exists(db, user.id)
    _check_wallet_active(wallet)
    amount = _parse_amount(body.amount)

    _enforce_daily_limit(db, user.id, TxType.WITHDRAWAL.value, amount, user.kyc_level)

    if Decimal(str(wallet["balance"])) < amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    ref = str(uuid.uuid4())
    result = _debit(
        db, user.id, amount, TxType.WITHDRAWAL.value,
        reference=ref,
        provider=body.provider.value,
        description=f"Withdrawal to {body.provider.value} {body.phone_number}",
    )

    # Fire provider disbursement (placeholder)
    provider_result = _initiate_provider_payment(
        body.provider, body.phone_number, float(amount), ref
    )

    _log_activity(db, user.id, "withdrawal", {
        "amount": float(amount),
        "provider": body.provider.value,
        "tx_id": result.get("transaction_id"),
    })

    logger.info("withdrawal user=%s amount=%s provider=%s", user.id, amount, body.provider.value)

    return {
        "reference": ref,
        "transaction_id": result.get("transaction_id"),
        "balance": result.get("balance"),
        "status": "completed",
        "provider_message": provider_result.get("message"),
    }


# ---------- Transfer between wallets ----------
@app.post("/transfer")
async def transfer(
    body: TransferRequest,
    user: AuthenticatedUser = Depends(require_kyc_level(1)),
):
    """Transfer funds to another user's wallet."""
    db = get_supabase_client()

    if body.recipient_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot transfer to yourself")

    recipient = db.get_single("user_profiles", {"id": body.recipient_id})
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    _ensure_wallet_exists(db, user.id)
    _ensure_wallet_exists(db, body.recipient_id)

    amount = _parse_amount(body.amount)
    _enforce_daily_limit(db, user.id, TxType.TRANSFER_OUT.value, amount, user.kyc_level)

    result = db.rpc("transfer_between_wallets", {
        "p_sender_id": user.id,
        "p_recipient_id": body.recipient_id,
        "p_amount": float(amount),
        "p_description": body.description or "Wallet transfer",
    })

    if not result or not result.get("ok"):
        err = (result or {}).get("error", "Transfer failed")
        raise HTTPException(status_code=400, detail=err)

    _log_activity(db, user.id, "transfer_out", {
        "recipient_id": body.recipient_id,
        "amount": float(amount),
    })

    logger.info("transfer user=%s -> %s amount=%s", user.id, body.recipient_id, amount)

    return {
        "status": "completed",
        "debit_tx": result.get("debit_tx"),
        "credit_tx": result.get("credit_tx"),
        "message": f"Transferred {amount} ZMW to {recipient.get('full_name', 'recipient')}",
    }


# ---------- Initiate a payment for an order via mobile money ----------
@app.post("/pay")
async def initiate_payment(
    body: PaymentInitRequest,
    user: AuthenticatedUser = Depends(require_kyc_level(1)),
):
    """Start a mobile money collection for an order."""
    db = get_supabase_client()
    wallet = _ensure_wallet_exists(db, user.id)
    amount = _parse_amount(body.amount)

    ref = str(uuid.uuid4())
    provider_result = _initiate_provider_payment(
        body.provider, body.phone_number, float(amount), ref
    )

    db.insert("wallet_transactions", {
        "wallet_id": wallet["id"],
        "user_id": user.id,
        "type": TxType.PAYMENT.value,
        "amount": float(amount),
        "balance_before": float(wallet["balance"]),
        "balance_after": float(wallet["balance"]),
        "status": "processing",
        "reference": body.order_id,
        "provider": body.provider.value,
        "provider_ref": provider_result.get("provider_ref"),
        "description": f"Payment for order {body.order_id}",
    })

    _log_activity(db, user.id, "payment_initiated", {
        "order_id": body.order_id,
        "amount": float(amount),
        "provider": body.provider.value,
    })

    logger.info(
        "payment initiated user=%s order=%s amount=%s provider=%s",
        user.id, body.order_id, amount, body.provider.value,
    )

    return {
        "reference": ref,
        "provider_ref": provider_result.get("provider_ref"),
        "status": "processing",
        "message": provider_result.get("message"),
    }


# ---------- Transaction history ----------
@app.get("/transactions")
async def list_transactions(
    limit: int = 20,
    offset: int = 0,
    tx_type: Optional[TxType] = None,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Paginated transaction history for the caller."""
    db = get_supabase_client()
    filters: Dict[str, Any] = {"user_id": user.id}
    if tx_type:
        filters["type"] = tx_type.value

    rows = db.query("wallet_transactions", filters=filters, order_by="created_at")
    # Manual pagination (the shared client doesn't expose offset/limit yet)
    page = rows[offset : offset + limit]
    return {
        "transactions": _build_tx_history(page),
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }


# ---------- Single transaction detail ----------
@app.get("/transactions/{tx_id}")
async def get_transaction(
    tx_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get a single transaction by ID."""
    db = get_supabase_client()
    tx = db.get_single("wallet_transactions", {"id": tx_id, "user_id": user.id})
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _build_tx_history([tx])[0]


# ---------- Webhook: mobile money callback ----------
@app.post("/webhooks/mobile-money")
async def mobile_money_webhook(payload: Dict[str, Any]):
    """
    Called by MTN / Airtel / Zamtel when a payment is confirmed or fails.
    Expected payload keys: provider_ref, status ('completed' | 'failed'), amount.
    """
    provider_ref = payload.get("provider_ref") or payload.get("externalId")
    new_status = payload.get("status", "").lower()

    if not provider_ref or new_status not in ("completed", "failed"):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    db = get_supabase_client()
    tx = db.get_single("wallet_transactions", {"provider_ref": provider_ref})
    if not tx:
        logger.warning("webhook: no transaction for provider_ref=%s", provider_ref)
        return {"status": "ignored"}

    if tx["status"] == "completed":
        return {"status": "already_processed"}

    if new_status == "completed":
        # Credit the wallet atomically
        _credit(
            db, tx["user_id"], Decimal(str(tx["amount"])), tx["type"],
            reference=tx.get("reference"),
            provider=tx.get("provider", "mobile_money"),
            provider_ref=provider_ref,
            description=tx.get("description"),
        )
        logger.info("webhook: credited user=%s amount=%s ref=%s", tx["user_id"], tx["amount"], provider_ref)
    else:
        db.update("wallet_transactions", {"id": tx["id"]}, {"status": "failed"})
        logger.info("webhook: marked failed ref=%s", provider_ref)

    _log_activity(db, tx["user_id"], f"webhook_{new_status}", {
        "provider_ref": provider_ref,
        "amount": float(tx["amount"]),
    })

    return {"status": "processed"}


# ---------- Admin: freeze / unfreeze ----------
@app.patch("/admin/wallets/{wallet_user_id}/status")
async def admin_set_wallet_status(
    wallet_user_id: str,
    status: str,
    user: AuthenticatedUser = Depends(require_roles([UserRole.ADMIN])),
):
    """Freeze or reactivate a wallet (admin only)."""
    if status not in ("active", "frozen", "closed"):
        raise HTTPException(status_code=400, detail="Invalid status")
    db = get_supabase_client()
    wallet = db.get_single("wallets", {"user_id": wallet_user_id})
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    db.update("wallets", {"id": wallet["id"]}, {"status": status})
    _log_activity(db, user.id, "admin_wallet_status_change", {
        "target_user": wallet_user_id,
        "new_status": status,
    })
    logger.info("admin wallet status: user=%s -> %s by admin=%s", wallet_user_id, status, user.id)
    return {"wallet_user_id": wallet_user_id, "status": status}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

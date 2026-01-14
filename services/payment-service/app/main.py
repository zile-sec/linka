from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
from decimal import Decimal
import os
from datetime import datetime
import uuid

from shared.supabase_client import SupabaseClient, get_supabase_client
from shared.auth_middleware import get_current_user, require_kyc_level, require_roles

app = FastAPI(title="Linka Payment Service")
security = HTTPBearer()

# Initialize Supabase client
supabase = SupabaseClient()

# ============== Enums ==============
class PaymentMethod(str, Enum):
    MOBILE_MONEY = "mobile_money"
    BANK_TRANSFER = "bank_transfer"
    WALLET = "wallet"
    CARD = "card"

class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"

class TransactionType(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    PAYMENT = "payment"
    REFUND = "refund"
    TRANSFER = "transfer"

# ============== Pydantic Models ==============
class PaymentRequest(BaseModel):
    order_id: str
    amount: Decimal = Field(..., gt=0)
    currency: str = "ZMW"
    payment_method: PaymentMethod
    mobile_number: Optional[str] = None
    bank_account: Optional[str] = None
    metadata: Optional[dict] = None

class WalletTopUpRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, le=50000)  # BoZ limit
    payment_method: PaymentMethod
    mobile_number: Optional[str] = None

class WalletTransferRequest(BaseModel):
    recipient_id: str
    amount: Decimal = Field(..., gt=0)
    description: Optional[str] = None

class RefundRequest(BaseModel):
    payment_id: str
    amount: Optional[Decimal] = None  # Partial refund
    reason: str

# ============== Health Check ==============
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "payment-service", "timestamp": datetime.utcnow().isoformat()}

# ============== Payment Processing ==============
@app.post("/payments/process")
async def process_payment(
    request: PaymentRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Process a payment for an order with BoZ compliance"""
    user = await get_current_user(credentials.credentials)
    profile = await require_kyc_level(user["id"], level=1)
    
    # Verify order exists and belongs to user
    order = await supabase.get_single("orders", {"id": request.order_id, "user_id": user["id"]})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "completed":
        raise HTTPException(status_code=400, detail="Order already paid")
    
    # Create payment record
    payment_data = {
        "id": str(uuid.uuid4()),
        "order_id": request.order_id,
        "user_id": user["id"],
        "amount": float(request.amount),
        "currency": request.currency,
        "payment_method": request.payment_method.value,
        "status": PaymentStatus.PENDING.value,
        "mobile_number": request.mobile_number,
        "metadata": request.metadata or {}
    }
    
    payment = await supabase.insert("payments", payment_data)
    
    # Process based on payment method
    if request.payment_method == PaymentMethod.WALLET:
        result = await _process_wallet_payment(user["id"], payment["id"], request.amount)
    elif request.payment_method == PaymentMethod.MOBILE_MONEY:
        result = await _process_mobile_money(payment["id"], request.mobile_number, request.amount)
    else:
        result = {"status": "pending", "message": "Payment method processing initiated"}
    
    # Update payment status
    await supabase.update("payments", {"id": payment["id"]}, {"status": result.get("status", "processing")})
    
    # Audit log
    background_tasks.add_task(
        _log_audit, user["id"], "payment_initiated", 
        {"payment_id": payment["id"], "amount": float(request.amount), "method": request.payment_method.value}
    )
    
    return {
        "payment_id": payment["id"],
        "status": result.get("status"),
        "message": result.get("message"),
        "next_action": result.get("next_action")
    }

async def _process_wallet_payment(user_id: str, payment_id: str, amount: Decimal) -> dict:
    """Process payment from user's wallet"""
    # Get wallet balance
    wallet = await supabase.get_single("wallets", {"user_id": user_id})
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    
    if Decimal(str(wallet["balance"])) < amount:
        return {"status": "failed", "message": "Insufficient wallet balance"}
    
    # Deduct from wallet using RPC for atomic operation
    result = await supabase.rpc("deduct_wallet_balance", {
        "p_user_id": user_id,
        "p_amount": float(amount),
        "p_reference": payment_id,
        "p_description": f"Payment for order"
    })
    
    if result.get("success"):
        return {"status": "completed", "message": "Payment completed from wallet"}
    return {"status": "failed", "message": result.get("error", "Wallet deduction failed")}

async def _process_mobile_money(payment_id: str, mobile_number: str, amount: Decimal) -> dict:
    """Initiate mobile money payment (MTN/Airtel Zambia)"""
    # Integration with mobile money providers would go here
    # For now, return pending status for async processing
    return {
        "status": "processing",
        "message": "Mobile money payment initiated. Check your phone for confirmation.",
        "next_action": "confirm_mobile_payment"
    }

# ============== Wallet Operations ==============
@app.get("/wallets/balance")
async def get_wallet_balance(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get user's wallet balance"""
    user = await get_current_user(credentials.credentials)
    
    wallet = await supabase.get_single("wallets", {"user_id": user["id"]})
    if not wallet:
        # Create wallet if doesn't exist
        wallet = await supabase.insert("wallets", {
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "balance": 0.00,
            "currency": "ZMW",
            "status": "active"
        })
    
    return {
        "balance": wallet["balance"],
        "currency": wallet["currency"],
        "status": wallet["status"],
        "updated_at": wallet.get("updated_at")
    }

@app.post("/wallets/topup")
async def topup_wallet(
    request: WalletTopUpRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Top up wallet with BoZ transaction limits"""
    user = await get_current_user(credentials.credentials)
    profile = await require_kyc_level(user["id"], level=1)
    
    # Check daily transaction limits (BoZ compliance)
    daily_total = await _get_daily_transaction_total(user["id"], TransactionType.DEPOSIT)
    daily_limit = Decimal("100000") if profile.get("kyc_level", 0) >= 2 else Decimal("50000")
    
    if daily_total + request.amount > daily_limit:
        raise HTTPException(
            status_code=400, 
            detail=f"Daily limit exceeded. Remaining: {daily_limit - daily_total} ZMW"
        )
    
    # Create transaction record
    transaction_data = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "type": TransactionType.DEPOSIT.value,
        "amount": float(request.amount),
        "currency": "ZMW",
        "status": "pending",
        "payment_method": request.payment_method.value,
        "mobile_number": request.mobile_number
    }
    
    transaction = await supabase.insert("wallet_transactions", transaction_data)
    
    # Process top-up based on method
    if request.payment_method == PaymentMethod.MOBILE_MONEY:
        result = await _process_mobile_money_topup(transaction["id"], request.mobile_number, request.amount)
    else:
        result = {"status": "pending", "message": "Top-up initiated"}
    
    background_tasks.add_task(
        _log_audit, user["id"], "wallet_topup_initiated",
        {"transaction_id": transaction["id"], "amount": float(request.amount)}
    )
    
    return {
        "transaction_id": transaction["id"],
        "status": result.get("status"),
        "message": result.get("message")
    }

@app.post("/wallets/transfer")
async def transfer_funds(
    request: WalletTransferRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Transfer funds between wallets"""
    user = await get_current_user(credentials.credentials)
    await require_kyc_level(user["id"], level=1)
    
    if request.recipient_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot transfer to yourself")
    
    # Verify recipient exists
    recipient = await supabase.get_single("user_profiles", {"id": request.recipient_id})
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")
    
    # Atomic transfer using RPC
    result = await supabase.rpc("transfer_wallet_funds", {
        "p_sender_id": user["id"],
        "p_recipient_id": request.recipient_id,
        "p_amount": float(request.amount),
        "p_description": request.description or "Wallet transfer"
    })
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Transfer failed"))
    
    background_tasks.add_task(
        _log_audit, user["id"], "wallet_transfer",
        {"recipient_id": request.recipient_id, "amount": float(request.amount)}
    )
    
    return {
        "status": "completed",
        "transaction_id": result.get("transaction_id"),
        "message": f"Transferred {request.amount} ZMW to {recipient.get('full_name', 'recipient')}"
    }

@app.get("/wallets/transactions")
async def get_wallet_transactions(
    limit: int = 20,
    offset: int = 0,
    transaction_type: Optional[TransactionType] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get wallet transaction history"""
    user = await get_current_user(credentials.credentials)
    
    filters = {"user_id": user["id"]}
    if transaction_type:
        filters["type"] = transaction_type.value
    
    transactions = await supabase.query(
        "wallet_transactions",
        filters=filters,
        order_by="created_at",
        ascending=False,
        limit=limit,
        offset=offset
    )
    
    return {"transactions": transactions, "limit": limit, "offset": offset}

# ============== Refunds ==============
@app.post("/payments/refund")
async def process_refund(
    request: RefundRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Process a refund for a payment"""
    user = await get_current_user(credentials.credentials)
    
    # Get original payment
    payment = await supabase.get_single("payments", {"id": request.payment_id})
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Verify ownership or admin role
    if payment["user_id"] != user["id"]:
        await require_roles(user["id"], ["admin", "support"])
    
    if payment["status"] != "completed":
        raise HTTPException(status_code=400, detail="Only completed payments can be refunded")
    
    refund_amount = float(request.amount) if request.amount else payment["amount"]
    
    # Create refund record
    refund_data = {
        "id": str(uuid.uuid4()),
        "original_payment_id": request.payment_id,
        "user_id": payment["user_id"],
        "amount": refund_amount,
        "reason": request.reason,
        "status": "processing",
        "processed_by": user["id"]
    }
    
    refund = await supabase.insert("refunds", refund_data)
    
    # Process refund to wallet
    await supabase.rpc("credit_wallet_balance", {
        "p_user_id": payment["user_id"],
        "p_amount": refund_amount,
        "p_reference": refund["id"],
        "p_description": f"Refund: {request.reason}"
    })
    
    # Update payment status
    await supabase.update("payments", {"id": request.payment_id}, {"status": "refunded"})
    await supabase.update("refunds", {"id": refund["id"]}, {"status": "completed"})
    
    background_tasks.add_task(
        _log_audit, user["id"], "refund_processed",
        {"refund_id": refund["id"], "payment_id": request.payment_id, "amount": refund_amount}
    )
    
    return {
        "refund_id": refund["id"],
        "status": "completed",
        "amount": refund_amount,
        "message": "Refund processed successfully"
    }

# ============== Payment History ==============
@app.get("/payments/history")
async def get_payment_history(
    limit: int = 20,
    offset: int = 0,
    status: Optional[PaymentStatus] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get user's payment history"""
    user = await get_current_user(credentials.credentials)
    
    filters = {"user_id": user["id"]}
    if status:
        filters["status"] = status.value
    
    payments = await supabase.query(
        "payments",
        filters=filters,
        order_by="created_at",
        ascending=False,
        limit=limit,
        offset=offset
    )
    
    return {"payments": payments, "limit": limit, "offset": offset}

# ============== Webhook Handlers ==============
@app.post("/webhooks/mobile-money")
async def mobile_money_webhook(payload: dict, background_tasks: BackgroundTasks):
    """Handle mobile money provider callbacks"""
    transaction_ref = payload.get("reference")
    status = payload.get("status")
    
    if not transaction_ref:
        raise HTTPException(status_code=400, detail="Missing reference")
    
    # Update transaction status
    if "payment" in transaction_ref:
        await supabase.update("payments", {"id": transaction_ref}, {"status": status})
    else:
        await supabase.update("wallet_transactions", {"id": transaction_ref}, {"status": status})
        
        # If deposit completed, credit wallet
        if status == "completed":
            transaction = await supabase.get_single("wallet_transactions", {"id": transaction_ref})
            if transaction and transaction["type"] == "deposit":
                await supabase.rpc("credit_wallet_balance", {
                    "p_user_id": transaction["user_id"],
                    "p_amount": transaction["amount"],
                    "p_reference": transaction_ref,
                    "p_description": "Mobile money deposit"
                })
    
    return {"status": "processed"}

# ============== Helper Functions ==============
async def _get_daily_transaction_total(user_id: str, transaction_type: TransactionType) -> Decimal:
    """Get total transactions for today (BoZ compliance)"""
    result = await supabase.rpc("get_daily_transaction_total", {
        "p_user_id": user_id,
        "p_type": transaction_type.value
    })
    return Decimal(str(result.get("total", 0)))

async def _process_mobile_money_topup(transaction_id: str, mobile_number: str, amount: Decimal) -> dict:
    """Initiate mobile money top-up"""
    # Integration with MTN/Airtel would go here
    return {
        "status": "processing",
        "message": "Please confirm the payment on your phone"
    }

async def _log_audit(user_id: str, action: str, details: dict):
    """Log audit trail for compliance"""
    await supabase.insert("audit_logs", {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "action": action,
        "details": details,
        "ip_address": None,  # Would be passed from request
        "user_agent": None
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)

    CARD = "card"

class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"

class TransactionType(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    PAYMENT = "payment"
    REFUND = "refund"
    TRANSFER = "transfer"

# ============== Pydantic Models ==============
class PaymentRequest(BaseModel):
    order_id: str
    amount: Decimal = Field(..., gt=0)
    currency: str = "ZMW"
    payment_method: PaymentMethod
    mobile_number: Optional[str] = None
    bank_account: Optional[str] = None
    metadata: Optional[dict] = None

class WalletTopUpRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, le=50000)  # BoZ limit
    payment_method: PaymentMethod
    mobile_number: Optional[str] = None

class WalletTransferRequest(BaseModel):
    recipient_id: str
    amount: Decimal = Field(..., gt=0)
    description: Optional[str] = None

class RefundRequest(BaseModel):
    payment_id: str
    amount: Optional[Decimal] = None  # Partial refund
    reason: str

# ============== Health Check ==============
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "payment-service", "timestamp": datetime.utcnow().isoformat()}

# ============== Payment Processing ==============
@app.post("/payments/process")
async def process_payment(
    request: PaymentRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Process a payment for an order with BoZ compliance"""
    user = await get_current_user(credentials.credentials)
    profile = await require_kyc_level(user["id"], level=1)
    
    # Verify order exists and belongs to user
    order = await supabase.get_single("orders", {"id": request.order_id, "user_id": user["id"]})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "completed":
        raise HTTPException(status_code=400, detail="Order already paid")
    
    # Create payment record
    payment_data = {
        "id": str(uuid.uuid4()),
        "order_id": request.order_id,
        "user_id": user["id"],
        "amount": float(request.amount),
        "currency": request.currency,
        "payment_method": request.payment_method.value,
        "status": PaymentStatus.PENDING.value,
        "mobile_number": request.mobile_number,
        "metadata": request.metadata or {}
    }
    
    payment = await supabase.insert("payments", payment_data)
    
    # Process based on payment method
    if request.payment_method == PaymentMethod.WALLET:
        result = await _process_wallet_payment(user["id"], payment["id"], request.amount)
    elif request.payment_method == PaymentMethod.MOBILE_MONEY:
        result = await _process_mobile_money(payment["id"], request.mobile_number, request.amount)
    else:
        result = {"status": "pending", "message": "Payment method processing initiated"}
    
    # Update payment status
    await supabase.update("payments", {"id": payment["id"]}, {"status": result.get("status", "processing")})
    
    # Audit log
    background_tasks.add_task(
        _log_audit, user["id"], "payment_initiated", 
        {"payment_id": payment["id"], "amount": float(request.amount), "method": request.payment_method.value}
    )
    
    return {
        "payment_id": payment["id"],
        "status": result.get("status"),
        "message": result.get("message"),
        "next_action": result.get("next_action")
    }

async def _process_wallet_payment(user_id: str, payment_id: str, amount: Decimal) -> dict:
    """Process payment from user's wallet"""
    # Get wallet balance
    wallet = await supabase.get_single("wallets", {"user_id": user_id})
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    
    if Decimal(str(wallet["balance"])) < amount:
        return {"status": "failed", "message": "Insufficient wallet balance"}
    
    # Deduct from wallet using RPC for atomic operation
    result = await supabase.rpc("deduct_wallet_balance", {
        "p_user_id": user_id,
        "p_amount": float(amount),
        "p_reference": payment_id,
        "p_description": f"Payment for order"
    })
    
    if result.get("success"):
        return {"status": "completed", "message": "Payment completed from wallet"}
    return {"status": "failed", "message": result.get("error", "Wallet deduction failed")}

async def _process_mobile_money(payment_id: str, mobile_number: str, amount: Decimal) -> dict:
    """Initiate mobile money payment (MTN/Airtel Zambia)"""
    # Integration with mobile money providers would go here
    # For now, return pending status for async processing
    return {
        "status": "processing",
        "message": "Mobile money payment initiated. Check your phone for confirmation.",
        "next_action": "confirm_mobile_payment"
    }

# ============== Wallet Operations ==============
@app.get("/wallets/balance")
async def get_wallet_balance(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get user's wallet balance"""
    user = await get_current_user(credentials.credentials)
    
    wallet = await supabase.get_single("wallets", {"user_id": user["id"]})
    if not wallet:
        # Create wallet if doesn't exist
        wallet = await supabase.insert("wallets", {
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "balance": 0.00,
            "currency": "ZMW",
            "status": "active"
        })
    
    return {
        "balance": wallet["balance"],
        "currency": wallet["currency"],
        "status": wallet["status"],
        "updated_at": wallet.get("updated_at")
    }

@app.post("/wallets/topup")
async def topup_wallet(
    request: WalletTopUpRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Top up wallet with BoZ transaction limits"""
    user = await get_current_user(credentials.credentials)
    profile = await require_kyc_level(user["id"], level=1)
    
    # Check daily transaction limits (BoZ compliance)
    daily_total = await _get_daily_transaction_total(user["id"], TransactionType.DEPOSIT)
    daily_limit = Decimal("100000") if profile.get("kyc_level", 0) >= 2 else Decimal("50000")
    
    if daily_total + request.amount > daily_limit:
        raise HTTPException(
            status_code=400, 
            detail=f"Daily limit exceeded. Remaining: {daily_limit - daily_total} ZMW"
        )
    
    # Create transaction record
    transaction_data = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "type": TransactionType.DEPOSIT.value,
        "amount": float(request.amount),
        "currency": "ZMW",
        "status": "pending",
        "payment_method": request.payment_method.value,
        "mobile_number": request.mobile_number
    }
    
    transaction = await supabase.insert("wallet_transactions", transaction_data)
    
    # Process top-up based on method
    if request.payment_method == PaymentMethod.MOBILE_MONEY:
        result = await _process_mobile_money_topup(transaction["id"], request.mobile_number, request.amount)
    else:
        result = {"status": "pending", "message": "Top-up initiated"}
    
    background_tasks.add_task(
        _log_audit, user["id"], "wallet_topup_initiated",
        {"transaction_id": transaction["id"], "amount": float(request.amount)}
    )
    
    return {
        "transaction_id": transaction["id"],
        "status": result.get("status"),
        "message": result.get("message")
    }

@app.post("/wallets/transfer")
async def transfer_funds(
    request: WalletTransferRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Transfer funds between wallets"""
    user = await get_current_user(credentials.credentials)
    await require_kyc_level(user["id"], level=1)
    
    if request.recipient_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot transfer to yourself")
    
    # Verify recipient exists
    recipient = await supabase.get_single("user_profiles", {"id": request.recipient_id})
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")
    
    # Atomic transfer using RPC
    result = await supabase.rpc("transfer_wallet_funds", {
        "p_sender_id": user["id"],
        "p_recipient_id": request.recipient_id,
        "p_amount": float(request.amount),
        "p_description": request.description or "Wallet transfer"
    })
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Transfer failed"))
    
    background_tasks.add_task(
        _log_audit, user["id"], "wallet_transfer",
        {"recipient_id": request.recipient_id, "amount": float(request.amount)}
    )
    
    return {
        "status": "completed",
        "transaction_id": result.get("transaction_id"),
        "message": f"Transferred {request.amount} ZMW to {recipient.get('full_name', 'recipient')}"
    }

@app.get("/wallets/transactions")
async def get_wallet_transactions(
    limit: int = 20,
    offset: int = 0,
    transaction_type: Optional[TransactionType] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get wallet transaction history"""
    user = await get_current_user(credentials.credentials)
    
    filters = {"user_id": user["id"]}
    if transaction_type:
        filters["type"] = transaction_type.value
    
    transactions = await supabase.query(
        "wallet_transactions",
        filters=filters,
        order_by="created_at",
        ascending=False,
        limit=limit,
        offset=offset
    )
    
    return {"transactions": transactions, "limit": limit, "offset": offset}

# ============== Refunds ==============
@app.post("/payments/refund")
async def process_refund(
    request: RefundRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Process a refund for a payment"""
    user = await get_current_user(credentials.credentials)
    
    # Get original payment
    payment = await supabase.get_single("payments", {"id": request.payment_id})
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Verify ownership or admin role
    if payment["user_id"] != user["id"]:
        await require_roles(user["id"], ["admin", "support"])
    
    if payment["status"] != "completed":
        raise HTTPException(status_code=400, detail="Only completed payments can be refunded")
    
    refund_amount = float(request.amount) if request.amount else payment["amount"]
    
    # Create refund record
    refund_data = {
        "id": str(uuid.uuid4()),
        "original_payment_id": request.payment_id,
        "user_id": payment["user_id"],
        "amount": refund_amount,
        "reason": request.reason,
        "status": "processing",
        "processed_by": user["id"]
    }
    
    refund = await supabase.insert("refunds", refund_data)
    
    # Process refund to wallet
    await supabase.rpc("credit_wallet_balance", {
        "p_user_id": payment["user_id"],
        "p_amount": refund_amount,
        "p_reference": refund["id"],
        "p_description": f"Refund: {request.reason}"
    })
    
    # Update payment status
    await supabase.update("payments", {"id": request.payment_id}, {"status": "refunded"})
    await supabase.update("refunds", {"id": refund["id"]}, {"status": "completed"})
    
    background_tasks.add_task(
        _log_audit, user["id"], "refund_processed",
        {"refund_id": refund["id"], "payment_id": request.payment_id, "amount": refund_amount}
    )
    
    return {
        "refund_id": refund["id"],
        "status": "completed",
        "amount": refund_amount,
        "message": "Refund processed successfully"
    }

# ============== Payment History ==============
@app.get("/payments/history")
async def get_payment_history(
    limit: int = 20,
    offset: int = 0,
    status: Optional[PaymentStatus] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get user's payment history"""
    user = await get_current_user(credentials.credentials)
    
    filters = {"user_id": user["id"]}
    if status:
        filters["status"] = status.value
    
    payments = await supabase.query(
        "payments",
        filters=filters,
        order_by="created_at",
        ascending=False,
        limit=limit,
        offset=offset
    )
    
    return {"payments": payments, "limit": limit, "offset": offset}

# ============== Webhook Handlers ==============
@app.post("/webhooks/mobile-money")
async def mobile_money_webhook(payload: dict, background_tasks: BackgroundTasks):
    """Handle mobile money provider callbacks"""
    transaction_ref = payload.get("reference")
    status = payload.get("status")
    
    if not transaction_ref:
        raise HTTPException(status_code=400, detail="Missing reference")
    
    # Update transaction status
    if "payment" in transaction_ref:
        await supabase.update("payments", {"id": transaction_ref}, {"status": status})
    else:
        await supabase.update("wallet_transactions", {"id": transaction_ref}, {"status": status})
        
        # If deposit completed, credit wallet
        if status == "completed":
            transaction = await supabase.get_single("wallet_transactions", {"id": transaction_ref})
            if transaction and transaction["type"] == "deposit":
                await supabase.rpc("credit_wallet_balance", {
                    "p_user_id": transaction["user_id"],
                    "p_amount": transaction["amount"],
                    "p_reference": transaction_ref,
                    "p_description": "Mobile money deposit"
                })
    
    return {"status": "processed"}

# ============== Helper Functions ==============
async def _get_daily_transaction_total(user_id: str, transaction_type: TransactionType) -> Decimal:
    """Get total transactions for today (BoZ compliance)"""
    result = await supabase.rpc("get_daily_transaction_total", {
        "p_user_id": user_id,
        "p_type": transaction_type.value
    })
    return Decimal(str(result.get("total", 0)))

async def _process_mobile_money_topup(transaction_id: str, mobile_number: str, amount: Decimal) -> dict:
    """Initiate mobile money top-up"""
    # Integration with MTN/Airtel would go here
    return {
        "status": "processing",
        "message": "Please confirm the payment on your phone"
    }

async def _log_audit(user_id: str, action: str, details: dict):
    """Log audit trail for compliance"""
    await supabase.insert("audit_logs", {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "action": action,
        "details": details,
        "ip_address": None,  # Would be passed from request
        "user_agent": None
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)

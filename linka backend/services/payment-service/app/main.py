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

# ============== Digital Receipts ==============
@app.get("/receipts/{receipt_id}")
async def get_receipt(
    receipt_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get digital receipt details"""
    user = await get_current_user(credentials.credentials)
    
    # Use RPC to get full receipt details with line items
    receipt = await supabase.rpc("get_receipt_details", {"p_receipt_id": receipt_id})
    
    if not receipt or len(receipt) == 0:
        raise HTTPException(status_code=404, detail="Receipt not found")
    
    receipt_data = receipt[0]
    
    # Verify access (customer, retailer, or admin)
    receipt_full = await supabase.get_single("receipts", {"id": receipt_id})
    if receipt_full["customer_id"] != user["id"] and receipt_full["retailer_id"] != user["id"]:
        await require_roles(user["id"], ["admin"])
    
    return {
        "receipt": receipt_data,
        "download_url": f"/receipts/{receipt_id}/pdf"
    }

@app.get("/receipts/order/{order_id}")
async def get_receipt_by_order(
    order_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get receipt for a specific order"""
    user = await get_current_user(credentials.credentials)
    
    # Find receipt by order ID
    receipt = await supabase.get_single("receipts", {"order_id": order_id})
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found for this order")
    
    # Verify access
    if receipt["customer_id"] != user["id"] and receipt["retailer_id"] != user["id"]:
        await require_roles(user["id"], ["admin"])
    
    # Get full details
    receipt_details = await supabase.rpc("get_receipt_details", {"p_receipt_id": receipt["id"]})
    
    return {
        "receipt": receipt_details[0] if receipt_details else receipt,
        "download_url": f"/receipts/{receipt['id']}/pdf"
    }

@app.get("/receipts")
async def list_receipts(
    limit: int = 20,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """List receipts for the authenticated user"""
    user = await get_current_user(credentials.credentials)
    
    # Check if admin or regular user
    try:
        await require_roles(user["id"], ["admin"])
        is_admin = True
    except:
        is_admin = False
    
    # Build query based on role
    if is_admin:
        filters = {}
    else:
        # Get both customer and retailer receipts
        customer_receipts = await supabase.query(
            "receipts",
            filters={"customer_id": user["id"]},
            order_by="issued_at",
            ascending=False,
            limit=limit,
            offset=offset
        )
        
        retailer_receipts = await supabase.query(
            "receipts",
            filters={"retailer_id": user["id"]},
            order_by="issued_at",
            ascending=False,
            limit=limit,
            offset=offset
        )
        
        # Combine and sort
        all_receipts = customer_receipts + retailer_receipts
        all_receipts.sort(key=lambda x: x.get("issued_at", ""), reverse=True)
        
        return {
            "receipts": all_receipts[:limit],
            "count": len(all_receipts),
            "limit": limit,
            "offset": offset
        }
    
    receipts = await supabase.query(
        "receipts",
        filters=filters,
        order_by="issued_at",
        ascending=False,
        limit=limit,
        offset=offset
    )
    
    return {"receipts": receipts, "count": len(receipts), "limit": limit, "offset": offset}

@app.get("/receipts/{receipt_id}/pdf")
async def download_receipt_pdf(
    receipt_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Generate and download receipt as PDF"""
    user = await get_current_user(credentials.credentials)
    
    # Get receipt details
    receipt_details = await supabase.rpc("get_receipt_details", {"p_receipt_id": receipt_id})
    
    if not receipt_details or len(receipt_details) == 0:
        raise HTTPException(status_code=404, detail="Receipt not found")
    
    receipt = receipt_details[0]
    
    # Verify access
    receipt_record = await supabase.get_single("receipts", {"id": receipt_id})
    if receipt_record["customer_id"] != user["id"] and receipt_record["retailer_id"] != user["id"]:
        await require_roles(user["id"], ["admin"])
    
    # Generate PDF (simplified - in production use proper PDF library like ReportLab)
    from fastapi.responses import Response
    
    # For now, return HTML that can be printed to PDF
    html_receipt = _generate_receipt_html(receipt)
    
    return Response(
        content=html_receipt,
        media_type="text/html",
        headers={
            "Content-Disposition": f"inline; filename=receipt_{receipt.get('receipt_number', receipt_id)}.html"
        }
    )

@app.post("/receipts/{receipt_id}/email")
async def email_receipt(
    receipt_id: str,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Email receipt to customer"""
    user = await get_current_user(credentials.credentials)
    
    # Get receipt
    receipt = await supabase.get_single("receipts", {"id": receipt_id})
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    
    # Verify access (retailer or admin can email)
    if receipt["retailer_id"] != user["id"]:
        await require_roles(user["id"], ["admin"])
    
    if not receipt.get("customer_email"):
        raise HTTPException(status_code=400, detail="Customer email not available")
    
    # Email in background
    background_tasks.add_task(_send_receipt_email, receipt_id, receipt["customer_email"])
    
    # Mark as emailed
    await supabase.update("receipts", {"id": receipt_id}, {
        "is_emailed": True,
        "emailed_at": datetime.utcnow().isoformat()
    })
    
    return {"status": "email_sent", "email": receipt["customer_email"]}

# ============== Helper Functions for Receipts ==============
def _generate_receipt_html(receipt: dict) -> str:
    """Generate HTML receipt (can be converted to PDF)"""
    line_items = receipt.get("line_items", [])
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Receipt {receipt.get('receipt_number', '')}</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
            .header {{ text-align: center; border-bottom: 2px solid #333; padding-bottom: 20px; }}
            .business-info {{ margin: 20px 0; }}
            .customer-info {{ margin: 20px 0; background: #f5f5f5; padding: 15px; }}
            table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
            th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }}
            th {{ background: #333; color: white; }}
            .totals {{ text-align: right; margin: 20px 0; }}
            .totals div {{ margin: 5px 0; }}
            .total {{ font-size: 1.2em; font-weight: bold; }}
            .footer {{ margin-top: 40px; text-align: center; font-size: 0.9em; color: #666; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>RECEIPT</h1>
            <p><strong>Receipt #:</strong> {receipt.get('receipt_number', 'N/A')}</p>
            <p><strong>Date:</strong> {receipt.get('issued_at', 'N/A')}</p>
            {'<p><strong>TAX INVOICE</strong></p>' if receipt.get('is_tax_invoice') else ''}
        </div>
        
        <div class="business-info">
            <h3>From:</h3>
            <p><strong>{receipt.get('business_name', 'N/A')}</strong></p>
            <p>{receipt.get('business_address', 'N/A')}</p>
            <p>Phone: {receipt.get('business_phone', 'N/A')}</p>
            {f"<p>TPIN: {receipt.get('business_tpin')}</p>" if receipt.get('business_tpin') else ''}
        </div>
        
        <div class="customer-info">
            <h3>To:</h3>
            <p><strong>{receipt.get('customer_name', 'N/A')}</strong></p>
            <p>Phone: {receipt.get('customer_phone', 'N/A')}</p>
            <p>Email: {receipt.get('customer_email', 'N/A')}</p>
            {f"<p>Delivery Address: {receipt.get('delivery_address')}</p>" if receipt.get('delivery_address') else ''}
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>Item</th>
                    <th>SKU</th>
                    <th>Qty</th>
                    <th>Unit Price</th>
                    <th>Total</th>
                </tr>
            </thead>
            <tbody>
    """
    
    for item in line_items:
        html += f"""
                <tr>
                    <td>{item.get('product_name', 'N/A')}</td>
                    <td>{item.get('product_sku', 'N/A')}</td>
                    <td>{item.get('quantity', 0)}</td>
                    <td>ZMW {item.get('unit_price', 0):.2f}</td>
                    <td>ZMW {item.get('line_total', 0):.2f}</td>
                </tr>
        """
    
    html += f"""
            </tbody>
        </table>
        
        <div class="totals">
            <div>Subtotal: ZMW {receipt.get('subtotal', 0):.2f}</div>
            {f"<div>Tax (VAT): ZMW {receipt.get('tax_amount', 0):.2f}</div>" if receipt.get('tax_amount', 0) > 0 else ''}
            {f"<div>Delivery Fee: ZMW {receipt.get('delivery_fee', 0):.2f}</div>" if receipt.get('delivery_fee', 0) > 0 else ''}
            {f"<div>Discount: -ZMW {receipt.get('discount_amount', 0):.2f}</div>" if receipt.get('discount_amount', 0) > 0 else ''}
            <div class="total">Total: ZMW {receipt.get('total_amount', 0):.2f}</div>
            <div>Payment Method: {receipt.get('payment_method', 'N/A').replace('_', ' ').title()}</div>
        </div>
        
        <div class="footer">
            <p>Thank you for your business!</p>
            <p>Order #: {receipt.get('order_number', 'N/A')}</p>
            <p>This is a computer-generated receipt.</p>
        </div>
    </body>
    </html>
    """
    
    return html

async def _send_receipt_email(receipt_id: str, email: str):
    """Send receipt via email (placeholder for email service integration)"""
    # TODO: Integrate with email service (SendGrid, AWS SES, etc.)
    logger.info(f"Sending receipt {receipt_id} to {email}")
    pass

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

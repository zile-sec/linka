from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
from decimal import Decimal
import os
from datetime import datetime, timedelta
import uuid

# Packages are available via PYTHONPATH
from shared.supabase_client import SupabaseClient, get_supabase_client
from shared.auth_middleware import get_current_user, require_roles, require_kyc_level

app = FastAPI(title="Linka Subscription Service")
security = HTTPBearer()

# Initialize Supabase client
supabase = SupabaseClient()

# ============== Enums ==============
class PlanType(str, Enum):
    FREE = "free"
    BASIC = "basic"
    PREMIUM = "premium"
    ENTERPRISE = "enterprise"

class BillingCycle(str, Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"

class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    TRIAL = "trial"
    PAST_DUE = "past_due"

# ============== Pydantic Models ==============
class PlanCreateRequest(BaseModel):
    name: str
    plan_type: PlanType
    description: Optional[str] = None
    monthly_price: Decimal = Field(..., ge=0)
    quarterly_price: Optional[Decimal] = None
    yearly_price: Optional[Decimal] = None
    features: List[str] = []
    max_orders_per_month: Optional[int] = None
    max_products: Optional[int] = None
    commission_rate: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    is_active: bool = True

class SubscribeRequest(BaseModel):
    plan_id: str
    billing_cycle: BillingCycle = BillingCycle.MONTHLY
    payment_method: str = "wallet"
    auto_renew: bool = True

class CancelRequest(BaseModel):
    reason: Optional[str] = None
    immediate: bool = False

# ============== Health Check ==============
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "subscription-service", "timestamp": datetime.utcnow().isoformat()}

# ============== Plans Management ==============
@app.get("/plans")
async def list_plans(
    active_only: bool = True,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """List all subscription plans"""
    await get_current_user(credentials.credentials)
    
    filters = {}
    if active_only:
        filters["is_active"] = True
    
    plans = await supabase.query("subscription_plans", filters=filters, order_by="monthly_price")
    return {"plans": plans}

@app.get("/plans/{plan_id}")
async def get_plan(
    plan_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get plan details"""
    await get_current_user(credentials.credentials)
    
    plan = await supabase.get_single("subscription_plans", {"id": plan_id})
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    
    return {"plan": plan}

@app.post("/plans")
async def create_plan(
    request: PlanCreateRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Create a new subscription plan (admin only)"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["admin"])
    
    plan = await supabase.insert("subscription_plans", {
        "id": str(uuid.uuid4()),
        "name": request.name,
        "plan_type": request.plan_type.value,
        "description": request.description,
        "monthly_price": float(request.monthly_price),
        "quarterly_price": float(request.quarterly_price) if request.quarterly_price else None,
        "yearly_price": float(request.yearly_price) if request.yearly_price else None,
        "features": request.features,
        "max_orders_per_month": request.max_orders_per_month,
        "max_products": request.max_products,
        "commission_rate": float(request.commission_rate),
        "is_active": request.is_active
    })
    
    return {"status": "created", "plan": plan}

@app.put("/plans/{plan_id}")
async def update_plan(
    plan_id: str,
    request: PlanCreateRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Update a subscription plan (admin only)"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["admin"])
    
    plan = await supabase.get_single("subscription_plans", {"id": plan_id})
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    
    updated = await supabase.update("subscription_plans", {"id": plan_id}, {
        "name": request.name,
        "plan_type": request.plan_type.value,
        "description": request.description,
        "monthly_price": float(request.monthly_price),
        "quarterly_price": float(request.quarterly_price) if request.quarterly_price else None,
        "yearly_price": float(request.yearly_price) if request.yearly_price else None,
        "features": request.features,
        "max_orders_per_month": request.max_orders_per_month,
        "max_products": request.max_products,
        "commission_rate": float(request.commission_rate),
        "is_active": request.is_active
    })
    
    return {"status": "updated", "plan": updated}

# ============== Subscriptions ==============
@app.get("/subscriptions/current")
async def get_current_subscription(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get user's current subscription"""
    user = await get_current_user(credentials.credentials)
    
    subscription = await supabase.get_single("subscriptions", {
        "user_id": user["id"],
        "status": "active"
    })
    
    if not subscription:
        return {"subscription": None, "message": "No active subscription"}
    
    # Get plan details
    plan = await supabase.get_single("subscription_plans", {"id": subscription["plan_id"]})
    
    # Get usage stats
    usage = await _get_subscription_usage(user["id"], subscription["id"])
    
    return {
        "subscription": subscription,
        "plan": plan,
        "usage": usage
    }

@app.post("/subscriptions/subscribe")
async def subscribe_to_plan(
    request: SubscribeRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Subscribe to a plan"""
    user = await get_current_user(credentials.credentials)
    await require_kyc_level(user["id"], level=1)
    
    # Check for existing active subscription
    existing = await supabase.get_single("subscriptions", {
        "user_id": user["id"],
        "status": "active"
    })
    
    if existing:
        raise HTTPException(status_code=400, detail="Already have an active subscription. Cancel or upgrade instead.")
    
    # Get plan
    plan = await supabase.get_single("subscription_plans", {"id": request.plan_id})
    if not plan or not plan.get("is_active"):
        raise HTTPException(status_code=404, detail="Plan not found or inactive")
    
    # Calculate price based on billing cycle
    price = _get_cycle_price(plan, request.billing_cycle)
    
    # Process payment
    payment_result = await _process_subscription_payment(user["id"], price, request.payment_method)
    if not payment_result.get("success"):
        raise HTTPException(status_code=400, detail=payment_result.get("error", "Payment failed"))
    
    # Calculate period dates
    start_date = datetime.utcnow()
    end_date = _calculate_end_date(start_date, request.billing_cycle)
    
    # Create subscription
    subscription = await supabase.insert("subscriptions", {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "plan_id": request.plan_id,
        "status": SubscriptionStatus.ACTIVE.value,
        "billing_cycle": request.billing_cycle.value,
        "current_period_start": start_date.isoformat(),
        "current_period_end": end_date.isoformat(),
        "auto_renew": request.auto_renew,
        "payment_method": request.payment_method
    })
    
    # Record billing
    await supabase.insert("subscription_billings", {
        "id": str(uuid.uuid4()),
        "subscription_id": subscription["id"],
        "amount": price,
        "currency": "ZMW",
        "status": "paid",
        "billing_period_start": start_date.isoformat(),
        "billing_period_end": end_date.isoformat(),
        "payment_reference": payment_result.get("reference")
    })
    
    # Update user role
    background_tasks.add_task(_update_user_role, user["id"], plan["plan_type"])
    
    return {
        "status": "subscribed",
        "subscription": subscription,
        "plan": plan,
        "next_billing_date": end_date.isoformat()
    }

@app.post("/subscriptions/upgrade")
async def upgrade_subscription(
    request: SubscribeRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Upgrade to a higher plan"""
    user = await get_current_user(credentials.credentials)
    
    # Get current subscription
    current = await supabase.get_single("subscriptions", {
        "user_id": user["id"],
        "status": "active"
    })
    
    if not current:
        raise HTTPException(status_code=400, detail="No active subscription to upgrade")
    
    current_plan = await supabase.get_single("subscription_plans", {"id": current["plan_id"]})
    new_plan = await supabase.get_single("subscription_plans", {"id": request.plan_id})
    
    if not new_plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    
    # Ensure it's an upgrade
    plan_order = {"free": 0, "basic": 1, "premium": 2, "enterprise": 3}
    if plan_order.get(new_plan["plan_type"], 0) <= plan_order.get(current_plan["plan_type"], 0):
        raise HTTPException(status_code=400, detail="Can only upgrade to a higher plan")
    
    # Calculate prorated amount
    prorated_amount = await _calculate_proration(current, new_plan, request.billing_cycle)
    
    # Process payment
    if prorated_amount > 0:
        payment_result = await _process_subscription_payment(user["id"], prorated_amount, request.payment_method)
        if not payment_result.get("success"):
            raise HTTPException(status_code=400, detail=payment_result.get("error", "Payment failed"))
    
    # Update subscription
    end_date = _calculate_end_date(datetime.utcnow(), request.billing_cycle)
    
    updated = await supabase.update("subscriptions", {"id": current["id"]}, {
        "plan_id": request.plan_id,
        "billing_cycle": request.billing_cycle.value,
        "current_period_start": datetime.utcnow().isoformat(),
        "current_period_end": end_date.isoformat()
    })
    
    # Record billing
    await supabase.insert("subscription_billings", {
        "id": str(uuid.uuid4()),
        "subscription_id": current["id"],
        "amount": prorated_amount,
        "currency": "ZMW",
        "status": "paid",
        "billing_period_start": datetime.utcnow().isoformat(),
        "billing_period_end": end_date.isoformat(),
        "notes": f"Upgrade from {current_plan['name']} to {new_plan['name']}"
    })
    
    background_tasks.add_task(_update_user_role, user["id"], new_plan["plan_type"])
    
    return {
        "status": "upgraded",
        "from_plan": current_plan["name"],
        "to_plan": new_plan["name"],
        "prorated_amount": prorated_amount
    }

@app.post("/subscriptions/cancel")
async def cancel_subscription(
    request: CancelRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Cancel subscription"""
    user = await get_current_user(credentials.credentials)
    
    subscription = await supabase.get_single("subscriptions", {
        "user_id": user["id"],
        "status": "active"
    })
    
    if not subscription:
        raise HTTPException(status_code=400, detail="No active subscription to cancel")
    
    if request.immediate:
        # Cancel immediately
        await supabase.update("subscriptions", {"id": subscription["id"]}, {
            "status": SubscriptionStatus.CANCELLED.value,
            "cancelled_at": datetime.utcnow().isoformat(),
            "cancellation_reason": request.reason
        })
        background_tasks.add_task(_update_user_role, user["id"], "free")
        
        return {
            "status": "cancelled",
            "effective_date": datetime.utcnow().isoformat()
        }
    else:
        # Cancel at end of billing period
        await supabase.update("subscriptions", {"id": subscription["id"]}, {
            "auto_renew": False,
            "cancellation_reason": request.reason
        })
        
        return {
            "status": "scheduled_cancellation",
            "effective_date": subscription["current_period_end"]
        }

@app.post("/subscriptions/pause")
async def pause_subscription(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Pause subscription (max 30 days)"""
    user = await get_current_user(credentials.credentials)
    
    subscription = await supabase.get_single("subscriptions", {
        "user_id": user["id"],
        "status": "active"
    })
    
    if not subscription:
        raise HTTPException(status_code=400, detail="No active subscription to pause")
    
    await supabase.update("subscriptions", {"id": subscription["id"]}, {
        "status": SubscriptionStatus.PAUSED.value,
        "paused_at": datetime.utcnow().isoformat()
    })
    
    return {
        "status": "paused",
        "resume_by": (datetime.utcnow() + timedelta(days=30)).isoformat()
    }

@app.post("/subscriptions/resume")
async def resume_subscription(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Resume paused subscription"""
    user = await get_current_user(credentials.credentials)
    
    subscription = await supabase.get_single("subscriptions", {
        "user_id": user["id"],
        "status": "paused"
    })
    
    if not subscription:
        raise HTTPException(status_code=400, detail="No paused subscription to resume")
    
    # Extend period by pause duration
    paused_at = datetime.fromisoformat(subscription["paused_at"])
    pause_duration = datetime.utcnow() - paused_at
    new_end = datetime.fromisoformat(subscription["current_period_end"]) + pause_duration
    
    await supabase.update("subscriptions", {"id": subscription["id"]}, {
        "status": SubscriptionStatus.ACTIVE.value,
        "paused_at": None,
        "current_period_end": new_end.isoformat()
    })
    
    return {
        "status": "resumed",
        "new_period_end": new_end.isoformat()
    }

# ============== Billing History ==============
@app.get("/subscriptions/billing-history")
async def get_billing_history(
    limit: int = 20,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get billing history"""
    user = await get_current_user(credentials.credentials)
    
    # Get user's subscriptions
    subscriptions = await supabase.query("subscriptions", {"user_id": user["id"]})
    sub_ids = [s["id"] for s in subscriptions]
    
    if not sub_ids:
        return {"billings": [], "limit": limit, "offset": offset}
    
    # Get billing records
    billings = await supabase.rpc("get_user_billing_history", {
        "p_user_id": user["id"],
        "p_limit": limit,
        "p_offset": offset
    })
    
    return {"billings": billings, "limit": limit, "offset": offset}

# ============== Usage & Limits ==============
@app.get("/subscriptions/usage")
async def get_usage(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get current usage against subscription limits"""
    user = await get_current_user(credentials.credentials)
    
    subscription = await supabase.get_single("subscriptions", {
        "user_id": user["id"],
        "status": "active"
    })
    
    if not subscription:
        return {"usage": None, "message": "No active subscription"}
    
    plan = await supabase.get_single("subscription_plans", {"id": subscription["plan_id"]})
    usage = await _get_subscription_usage(user["id"], subscription["id"])
    
    return {
        "plan": plan["name"],
        "usage": usage,
        "limits": {
            "max_orders_per_month": plan.get("max_orders_per_month"),
            "max_products": plan.get("max_products")
        }
    }

# ============== Webhooks ==============
@app.post("/webhooks/renewal")
async def process_renewals():
    """Process subscription renewals (called by scheduler)"""
    # Get subscriptions due for renewal
    due = await supabase.rpc("get_subscriptions_due_renewal", {})
    
    results = {"renewed": 0, "failed": 0}
    
    for sub in due:
        try:
            plan = await supabase.get_single("subscription_plans", {"id": sub["plan_id"]})
            price = _get_cycle_price(plan, BillingCycle(sub["billing_cycle"]))
            
            payment_result = await _process_subscription_payment(sub["user_id"], price, sub["payment_method"])
            
            if payment_result.get("success"):
                # Extend subscription
                new_end = _calculate_end_date(datetime.utcnow(), BillingCycle(sub["billing_cycle"]))
                await supabase.update("subscriptions", {"id": sub["id"]}, {
                    "current_period_start": datetime.utcnow().isoformat(),
                    "current_period_end": new_end.isoformat()
                })
                
                # Record billing
                await supabase.insert("subscription_billings", {
                    "id": str(uuid.uuid4()),
                    "subscription_id": sub["id"],
                    "amount": price,
                    "currency": "ZMW",
                    "status": "paid",
                    "billing_period_start": datetime.utcnow().isoformat(),
                    "billing_period_end": new_end.isoformat()
                })
                
                results["renewed"] += 1
            else:
                # Mark as past due
                await supabase.update("subscriptions", {"id": sub["id"]}, {
                    "status": SubscriptionStatus.PAST_DUE.value
                })
                results["failed"] += 1
                
        except Exception as e:
            results["failed"] += 1
    
    return results

# ============== Helper Functions ==============
def _get_cycle_price(plan: dict, cycle: BillingCycle) -> float:
    """Get price for billing cycle"""
    if cycle == BillingCycle.MONTHLY:
        return plan["monthly_price"]
    elif cycle == BillingCycle.QUARTERLY:
        return plan.get("quarterly_price") or plan["monthly_price"] * 3 * 0.9
    elif cycle == BillingCycle.YEARLY:
        return plan.get("yearly_price") or plan["monthly_price"] * 12 * 0.8
    return plan["monthly_price"]

def _calculate_end_date(start: datetime, cycle: BillingCycle) -> datetime:
    """Calculate subscription end date"""
    if cycle == BillingCycle.MONTHLY:
        return start + timedelta(days=30)
    elif cycle == BillingCycle.QUARTERLY:
        return start + timedelta(days=90)
    elif cycle == BillingCycle.YEARLY:
        return start + timedelta(days=365)
    return start + timedelta(days=30)

async def _process_subscription_payment(user_id: str, amount: float, payment_method: str) -> dict:
    """Process subscription payment"""
    if payment_method == "wallet":
        result = await supabase.rpc("deduct_wallet_balance", {
            "p_user_id": user_id,
            "p_amount": amount,
            "p_reference": str(uuid.uuid4()),
            "p_description": "Subscription payment"
        })
        return {"success": result.get("success", False), "reference": result.get("transaction_id")}
    return {"success": False, "error": "Unsupported payment method"}

async def _calculate_proration(current_sub: dict, new_plan: dict, new_cycle: BillingCycle) -> float:
    """Calculate prorated amount for upgrade"""
    # Get remaining days in current period
    end_date = datetime.fromisoformat(current_sub["current_period_end"])
    remaining_days = (end_date - datetime.utcnow()).days
    
    if remaining_days <= 0:
        return _get_cycle_price(new_plan, new_cycle)
    
    # Calculate daily rate difference
    current_plan = await supabase.get_single("subscription_plans", {"id": current_sub["plan_id"]})
    current_daily = current_plan["monthly_price"] / 30
    new_daily = new_plan["monthly_price"] / 30
    
    prorated = (new_daily - current_daily) * remaining_days + _get_cycle_price(new_plan, new_cycle)
    return max(0, prorated)

async def _get_subscription_usage(user_id: str, subscription_id: str) -> dict:
    """Get subscription usage metrics"""
    result = await supabase.rpc("get_subscription_usage", {
        "p_user_id": user_id,
        "p_subscription_id": subscription_id
    })
    return result or {"orders_this_month": 0, "products_count": 0}

async def _update_user_role(user_id: str, plan_type: str):
    """Update user role based on subscription"""
    role_map = {
        "free": "user",
        "basic": "retailer_basic",
        "premium": "retailer_premium",
        "enterprise": "retailer_enterprise"
    }
    
    role = role_map.get(plan_type, "user")
    await supabase.update("user_profiles", {"id": user_id}, {"subscription_tier": plan_type})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8006)

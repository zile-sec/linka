"""
Linka Order Service - Supabase Integrated
Handles order creation, management, and real-time status updates
"""

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, List
from decimal import Decimal
from datetime import datetime
import os
import logging

# Import shared modules
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from shared.supabase_client import get_supabase_client, SupabaseClient
from shared.auth_middleware import (
    get_current_user,
    AuthenticatedUser,
    UserRole,
    require_roles
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Linka Order Service",
    description="Order management with real-time updates",
    version="2.0.0"
)

# ============ PYDANTIC MODELS ============

class OrderItemCreate(BaseModel):
    product_id: str
    variant_id: Optional[str] = None
    quantity: int = Field(..., gt=0)

class AddressSnapshot(BaseModel):
    recipient_name: str
    phone: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    province: str
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    delivery_instructions: Optional[str] = None

class OrderCreate(BaseModel):
    retailer_id: str
    items: List[OrderItemCreate]
    shipping_address: AddressSnapshot
    billing_address: Optional[AddressSnapshot] = None
    customer_notes: Optional[str] = None
    payment_method: str = "wallet"

class OrderStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(confirmed|processing|ready_for_pickup|out_for_delivery|delivered|cancelled)$")
    notes: Optional[str] = None

class OrderResponse(BaseModel):
    id: str
    order_number: str
    status: str
    payment_status: str
    total_amount: Decimal
    created_at: datetime

# ============ HEALTH CHECK ============

@app.get("/health")
async def health():
    return {"status": "alive", "service": "order-service", "version": "2.0.0"}

@app.get("/ready")
async def readiness():
    try:
        client = get_supabase_client()
        is_healthy = await client.health_check()
        if not is_healthy:
            return {"status": "not ready", "detail": "Supabase unavailable"}, 503
        return {"status": "ready", "service": "order-service"}
    except Exception as e:
        return {"status": "not ready", "detail": str(e)}, 503

# ============ ORDER ENDPOINTS ============

@app.post("/orders")
async def create_order(
    order: OrderCreate,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Create a new order with inventory reservation.
    """
    logger.info(f"Creating order for user: {user.id}, retailer: {order.retailer_id}")
    
    try:
        client = get_supabase_client()
        
        # Validate items and calculate totals
        order_items = []
        subtotal = Decimal("0")
        
        for item in order.items:
            # Get product details
            product = await client.query(
                table="products",
                filters={"id": item.product_id},
                single=True
            )
            
            if not product or product.get("status") != "active":
                raise HTTPException(status_code=400, detail=f"Product {item.product_id} not available")
            
            # Get variant if specified
            variant = None
            unit_price = Decimal(str(product["price"]))
            
            if item.variant_id:
                variant = await client.query(
                    table="product_variants",
                    filters={"id": item.variant_id, "product_id": item.product_id},
                    single=True
                )
                if variant:
                    unit_price = Decimal(str(variant["price"]))
            
            # Check inventory
            inventory_filter = {
                "product_id": item.product_id,
            }
            if item.variant_id:
                inventory_filter["variant_id"] = item.variant_id
                
            inventory = await client.query(
                table="inventory",
                filters=inventory_filter
            )
            
            total_available = sum(inv.get("available_quantity", 0) for inv in inventory)
            
            if total_available < item.quantity:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Insufficient stock for {product['name']}. Available: {total_available}"
                )
            
            item_total = unit_price * item.quantity
            subtotal += item_total
            
            order_items.append({
                "product_id": item.product_id,
                "variant_id": item.variant_id,
                "warehouse_id": inventory[0]["warehouse_id"] if inventory else None,
                "product_name": product["name"],
                "variant_name": variant["name"] if variant else None,
                "sku": variant["sku"] if variant else product.get("sku"),
                "image_url": product.get("image_url"),
                "quantity": item.quantity,
                "unit_price": float(unit_price),
                "discount_amount": 0,
                "tax_amount": 0,
                "total_price": float(item_total)
            })
        
        # Calculate totals
        tax_amount = subtotal * Decimal("0.16")  # 16% VAT for Zambia
        shipping_amount = Decimal("25.00")  # Base shipping
        total_amount = subtotal + tax_amount + shipping_amount
        
        # Create order
        order_data = {
            "customer_id": user.id,
            "retailer_id": order.retailer_id,
            "status": "pending",
            "shipping_address": order.shipping_address.model_dump(),
            "billing_address": order.billing_address.model_dump() if order.billing_address else order.shipping_address.model_dump(),
            "subtotal": float(subtotal),
            "tax_amount": float(tax_amount),
            "shipping_amount": float(shipping_amount),
            "total_amount": float(total_amount),
            "payment_method": order.payment_method,
            "customer_notes": order.customer_notes
        }
        
        created_order = await client.insert(table="orders", data=order_data)
        order_id = created_order[0]["id"]
        
        # Create order items
        for item in order_items:
            item["order_id"] = order_id
        
        await client.insert(table="order_items", data=order_items)
        
        # Reserve inventory in background
        background_tasks.add_task(
            reserve_inventory_for_order,
            order_id,
            order_items
        )
        
        # Log audit event
        await client.insert(
            table="audit_logs",
            data={
                "user_id": user.id,
                "action": "order.created",
                "resource_type": "orders",
                "resource_id": order_id,
                "new_values": {"order_number": created_order[0]["order_number"]}
            },
            return_data=False
        )
        
        logger.info(f"Order created: {created_order[0]['order_number']}")
        
        return {
            "id": order_id,
            "order_number": created_order[0]["order_number"],
            "status": "pending",
            "total_amount": float(total_amount),
            "message": "Order created successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Order creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/orders")
async def list_orders(
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """
    List orders for the current user (customer or retailer).
    """
    try:
        client = get_supabase_client()
        
        # Build filters based on user role
        filters = {}
        if user.role == UserRole.CUSTOMER:
            filters["customer_id"] = user.id
        elif user.role == UserRole.RETAILER:
            filters["retailer_id"] = user.id
        
        if status:
            filters["status"] = status
        
        orders = await client.query(
            table="orders",
            select="*, order_items(*)",
            filters=filters,
            order="created_at.desc",
            limit=limit,
            offset=offset
        )
        
        return {
            "orders": orders,
            "count": len(orders),
            "limit": limit,
            "offset": offset
        }
        
    except Exception as e:
        logger.error(f"Failed to list orders: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Get order details with items and status history.
    """
    try:
        client = get_supabase_client()
        
        order = await client.query(
            table="orders",
            select="*, order_items(*), order_status_history(*)",
            filters={"id": order_id},
            single=True
        )
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        # Check access
        if user.role == UserRole.CUSTOMER and order["customer_id"] != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        if user.role == UserRole.RETAILER and order["retailer_id"] != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        return order
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get order {order_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/orders/{order_id}/status")
async def update_order_status(
    order_id: str,
    update: OrderStatusUpdate,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Update order status. Retailers can update their orders.
    """
    try:
        client = get_supabase_client()
        
        # Get current order
        order = await client.query(
            table="orders",
            filters={"id": order_id},
            single=True
        )
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        # Check permission (retailer or admin)
        if user.role == UserRole.RETAILER and order["retailer_id"] != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        elif user.role == UserRole.CUSTOMER:
            # Customers can only cancel pending orders
            if update.status != "cancelled" or order["status"] != "pending":
                raise HTTPException(status_code=403, detail="Customers can only cancel pending orders")
        
        # Validate status transition
        valid_transitions = {
            "pending": ["confirmed", "cancelled"],
            "confirmed": ["processing", "cancelled"],
            "processing": ["ready_for_pickup", "cancelled"],
            "ready_for_pickup": ["out_for_delivery"],
            "out_for_delivery": ["delivered"],
        }
        
        current_status = order["status"]
        if current_status in valid_transitions:
            if update.status not in valid_transitions[current_status]:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Cannot transition from {current_status} to {update.status}"
                )
        
        # Update order
        update_data = {"status": update.status}
        
        if update.status == "confirmed":
            update_data["confirmed_at"] = datetime.utcnow().isoformat()
        elif update.status == "out_for_delivery":
            update_data["shipped_at"] = datetime.utcnow().isoformat()
        elif update.status == "delivered":
            update_data["delivered_at"] = datetime.utcnow().isoformat()
            update_data["fulfillment_status"] = "fulfilled"
        elif update.status == "cancelled":
            update_data["cancelled_at"] = datetime.utcnow().isoformat()
            update_data["cancellation_reason"] = update.notes
            # Release inventory in background
            background_tasks.add_task(release_inventory_for_order, order_id)
        
        await client.update(
            table="orders",
            data=update_data,
            filters={"id": order_id}
        )
        
        # Send notification in background
        background_tasks.add_task(
            send_order_notification,
            order_id,
            order["customer_id"],
            update.status
        )
        
        logger.info(f"Order {order_id} status updated to {update.status}")
        
        return {
            "id": order_id,
            "status": update.status,
            "message": "Order status updated"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update order {order_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ HELPER FUNCTIONS ============

async def reserve_inventory_for_order(order_id: str, items: List[dict]):
    """Reserve inventory for order items"""
    try:
        client = get_supabase_client()
        
        for item in items:
            await client.rpc(
                "reserve_inventory",
                {
                    "p_product_id": item["product_id"],
                    "p_variant_id": item.get("variant_id"),
                    "p_warehouse_id": item["warehouse_id"],
                    "p_quantity": item["quantity"],
                    "p_reference_type": "order",
                    "p_reference_id": order_id
                }
            )
        
        logger.info(f"Inventory reserved for order {order_id}")
        
    except Exception as e:
        logger.error(f"Failed to reserve inventory for order {order_id}: {e}")


async def release_inventory_for_order(order_id: str):
    """Release reserved inventory when order is cancelled"""
    try:
        client = get_supabase_client()
        
        items = await client.query(
            table="order_items",
            filters={"order_id": order_id}
        )
        
        for item in items:
            await client.rpc(
                "release_inventory_reservation",
                {
                    "p_product_id": item["product_id"],
                    "p_variant_id": item.get("variant_id"),
                    "p_warehouse_id": item["warehouse_id"],
                    "p_quantity": item["quantity"],
                    "p_reference_type": "order",
                    "p_reference_id": order_id
                }
            )
        
        logger.info(f"Inventory released for cancelled order {order_id}")
        
    except Exception as e:
        logger.error(f"Failed to release inventory for order {order_id}: {e}")


async def send_order_notification(order_id: str, customer_id: str, status: str):
    """Send notification about order status change"""
    try:
        client = get_supabase_client()
        
        # Get order number
        order = await client.query(
            table="orders",
            select="order_number",
            filters={"id": order_id},
            single=True
        )
        
        status_messages = {
            "confirmed": "Your order has been confirmed!",
            "processing": "Your order is being prepared.",
            "ready_for_pickup": "Your order is ready for pickup!",
            "out_for_delivery": "Your order is on its way!",
            "delivered": "Your order has been delivered.",
            "cancelled": "Your order has been cancelled."
        }
        
        await client.insert(
            table="notifications",
            data={
                "user_id": customer_id,
                "title": f"Order {order['order_number']} Update",
                "body": status_messages.get(status, f"Order status: {status}"),
                "type": "info" if status != "cancelled" else "warning",
                "category": "order",
                "reference_type": "order",
                "reference_id": order_id
            },
            return_data=False
        )
        
    except Exception as e:
        logger.error(f"Failed to send notification for order {order_id}: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

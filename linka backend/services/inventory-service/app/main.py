from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
from decimal import Decimal
import os
from datetime import datetime
import uuid

# Packages are available via PYTHONPATH
from shared.supabase_client import SupabaseClient
from shared.auth_middleware import get_current_user, require_roles

app = FastAPI(title="Linka Inventory Service")
security = HTTPBearer()

# Initialize Supabase client
supabase = SupabaseClient()

# ============== Enums ==============
class StockMovementType(str, Enum):
    RECEIVED = "received"
    SOLD = "sold"
    RETURNED = "returned"
    DAMAGED = "damaged"
    ADJUSTMENT = "adjustment"
    TRANSFER = "transfer"

class AlertType(str, Enum):
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    OVERSTOCK = "overstock"
    EXPIRING_SOON = "expiring_soon"

# ============== Pydantic Models ==============
class InventoryUpdateRequest(BaseModel):
    product_id: str
    warehouse_id: str
    quantity_change: int
    movement_type: StockMovementType
    reference_id: Optional[str] = None
    notes: Optional[str] = None
    cost_per_unit: Optional[Decimal] = None

class StockTransferRequest(BaseModel):
    product_id: str
    from_warehouse_id: str
    to_warehouse_id: str
    quantity: int = Field(..., gt=0)
    notes: Optional[str] = None

class WarehouseCreateRequest(BaseModel):
    name: str
    address: str
    city: str
    province: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    capacity: Optional[int] = None
    manager_id: Optional[str] = None

class StockAlertConfig(BaseModel):
    product_id: str
    warehouse_id: str
    low_stock_threshold: int = 10
    reorder_point: int = 20
    max_stock_level: Optional[int] = None

class ProductCreate(BaseModel):
    name: str
    description: Optional[str] = None
    category_id: Optional[str] = None
    sku: Optional[str] = None
    price: Decimal = Field(..., gt=0)
    compare_at_price: Optional[Decimal] = None
    cost_per_unit: Optional[Decimal] = None
    image_url: Optional[str] = None
    tags: List[str] = []
    initial_stock: Optional[int] = 0
    warehouse_id: Optional[str] = None

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    compare_at_price: Optional[Decimal] = None
    status: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None

# ============== Health Check ==============
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "inventory-service", "timestamp": datetime.utcnow().isoformat()}

# ============== Inventory Queries ==============
@app.get("/inventory")
async def get_inventory(
    warehouse_id: Optional[str] = None,
    product_id: Optional[str] = None,
    low_stock_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get inventory levels with optional filters"""
    user = await get_current_user(credentials.credentials)
    
    filters = {}
    if warehouse_id:
        filters["warehouse_id"] = warehouse_id
    if product_id:
        filters["product_id"] = product_id
    
    if low_stock_only:
        # Use RPC for complex query
        inventory = await supabase.rpc("get_low_stock_inventory", {
            "p_warehouse_id": warehouse_id,
            "p_limit": limit,
            "p_offset": offset
        })
    else:
        inventory = await supabase.query(
            "inventory",
            filters=filters,
            order_by="updated_at",
            ascending=False,
            limit=limit,
            offset=offset
        )
    
    return {"inventory": inventory, "limit": limit, "offset": offset}

@app.get("/inventory/{product_id}")
async def get_product_inventory(
    product_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get inventory levels for a specific product across all warehouses"""
    user = await get_current_user(credentials.credentials)
    
    inventory = await supabase.query(
        "inventory",
        filters={"product_id": product_id}
    )
    
    # Get product details
    product = await supabase.get_single("products", {"id": product_id})
    
    total_quantity = sum(item.get("quantity", 0) for item in inventory)
    total_reserved = sum(item.get("reserved_quantity", 0) for item in inventory)
    
    return {
        "product_id": product_id,
        "product_name": product.get("name") if product else None,
        "total_quantity": total_quantity,
        "total_reserved": total_reserved,
        "available_quantity": total_quantity - total_reserved,
        "by_warehouse": inventory
    }

@app.get("/inventory/warehouse/{warehouse_id}")
async def get_warehouse_inventory(
    warehouse_id: str,
    category_id: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get all inventory in a specific warehouse"""
    user = await get_current_user(credentials.credentials)
    
    # Use RPC for joined query with product details
    inventory = await supabase.rpc("get_warehouse_inventory_details", {
        "p_warehouse_id": warehouse_id,
        "p_category_id": category_id,
        "p_search": search,
        "p_limit": limit,
        "p_offset": offset
    })
    
    return {"warehouse_id": warehouse_id, "inventory": inventory, "limit": limit, "offset": offset}

# ============== Stock Management ==============
@app.post("/inventory/update")
async def update_inventory(
    request: InventoryUpdateRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Update inventory with stock movement tracking"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["admin", "warehouse_manager", "retailer"])
    
    # Get current inventory record
    inventory = await supabase.get_single("inventory", {
        "product_id": request.product_id,
        "warehouse_id": request.warehouse_id
    })
    
    if not inventory:
        # Create new inventory record
        inventory = await supabase.insert("inventory", {
            "id": str(uuid.uuid4()),
            "product_id": request.product_id,
            "warehouse_id": request.warehouse_id,
            "quantity": 0,
            "reserved_quantity": 0,
            "cost_per_unit": float(request.cost_per_unit) if request.cost_per_unit else 0
        })
    
    new_quantity = inventory["quantity"] + request.quantity_change
    if new_quantity < 0:
        raise HTTPException(status_code=400, detail="Insufficient stock for this operation")
    
    # Update inventory
    await supabase.update("inventory", {"id": inventory["id"]}, {
        "quantity": new_quantity,
        "cost_per_unit": float(request.cost_per_unit) if request.cost_per_unit else inventory.get("cost_per_unit")
    })
    
    # Record stock movement
    movement = await supabase.insert("stock_movements", {
        "id": str(uuid.uuid4()),
        "product_id": request.product_id,
        "warehouse_id": request.warehouse_id,
        "movement_type": request.movement_type.value,
        "quantity": abs(request.quantity_change),
        "direction": "in" if request.quantity_change > 0 else "out",
        "reference_id": request.reference_id,
        "notes": request.notes,
        "performed_by": user["id"],
        "quantity_before": inventory["quantity"],
        "quantity_after": new_quantity
    })
    
    # Check for alerts
    background_tasks.add_task(_check_stock_alerts, request.product_id, request.warehouse_id, new_quantity)
    
    return {
        "status": "updated",
        "movement_id": movement["id"],
        "previous_quantity": inventory["quantity"],
        "new_quantity": new_quantity,
        "change": request.quantity_change
    }

@app.post("/inventory/transfer")
async def transfer_stock(
    request: StockTransferRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Transfer stock between warehouses"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["admin", "warehouse_manager"])
    
    if request.from_warehouse_id == request.to_warehouse_id:
        raise HTTPException(status_code=400, detail="Cannot transfer to same warehouse")
    
    # Use atomic RPC for transfer
    result = await supabase.rpc("transfer_stock", {
        "p_product_id": request.product_id,
        "p_from_warehouse_id": request.from_warehouse_id,
        "p_to_warehouse_id": request.to_warehouse_id,
        "p_quantity": request.quantity,
        "p_performed_by": user["id"],
        "p_notes": request.notes
    })
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Transfer failed"))
    
    return {
        "status": "transferred",
        "transfer_id": result.get("transfer_id"),
        "quantity": request.quantity,
        "from_warehouse": request.from_warehouse_id,
        "to_warehouse": request.to_warehouse_id
    }

@app.post("/inventory/reserve")
async def reserve_stock(
    product_id: str,
    warehouse_id: str,
    quantity: int,
    order_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Reserve stock for an order"""
    user = await get_current_user(credentials.credentials)
    
    result = await supabase.rpc("reserve_inventory", {
        "p_product_id": product_id,
        "p_warehouse_id": warehouse_id,
        "p_quantity": quantity,
        "p_order_id": order_id
    })
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Reservation failed"))
    
    return {
        "status": "reserved",
        "reservation_id": result.get("reservation_id"),
        "product_id": product_id,
        "quantity": quantity
    }

@app.post("/inventory/release")
async def release_reservation(
    reservation_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Release a stock reservation"""
    user = await get_current_user(credentials.credentials)
    
    result = await supabase.rpc("release_reservation", {
        "p_reservation_id": reservation_id
    })
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Release failed"))
    
    return {"status": "released", "reservation_id": reservation_id}

# ============== Warehouses ==============
@app.get("/warehouses")
async def list_warehouses(
    active_only: bool = True,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """List all warehouses"""
    user = await get_current_user(credentials.credentials)
    
    filters = {}
    if active_only:
        filters["is_active"] = True
    
    warehouses = await supabase.query("warehouses", filters=filters)
    return {"warehouses": warehouses}

@app.post("/warehouses")
async def create_warehouse(
    request: WarehouseCreateRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Create a new warehouse"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["admin"])
    
    warehouse = await supabase.insert("warehouses", {
        "id": str(uuid.uuid4()),
        "name": request.name,
        "address": request.address,
        "city": request.city,
        "province": request.province,
        "latitude": request.latitude,
        "longitude": request.longitude,
        "capacity": request.capacity,
        "manager_id": request.manager_id,
        "is_active": True
    })
    
    return {"status": "created", "warehouse": warehouse}

@app.get("/warehouses/{warehouse_id}")
async def get_warehouse(
    warehouse_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get warehouse details with summary stats"""
    user = await get_current_user(credentials.credentials)
    
    warehouse = await supabase.get_single("warehouses", {"id": warehouse_id})
    if not warehouse:
        raise HTTPException(status_code=404, detail="Warehouse not found")
    
    # Get inventory summary
    summary = await supabase.rpc("get_warehouse_summary", {"p_warehouse_id": warehouse_id})
    
    return {
        "warehouse": warehouse,
        "summary": summary
    }

# ============== Stock Movements History ==============
@app.get("/movements")
async def get_stock_movements(
    product_id: Optional[str] = None,
    warehouse_id: Optional[str] = None,
    movement_type: Optional[StockMovementType] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get stock movement history"""
    user = await get_current_user(credentials.credentials)
    
    result = await supabase.rpc("get_stock_movements", {
        "p_product_id": product_id,
        "p_warehouse_id": warehouse_id,
        "p_movement_type": movement_type.value if movement_type else None,
        "p_start_date": start_date,
        "p_end_date": end_date,
        "p_limit": limit,
        "p_offset": offset
    })
    
    return {"movements": result, "limit": limit, "offset": offset}

# ============== Alerts ==============
@app.get("/alerts")
async def get_stock_alerts(
    alert_type: Optional[AlertType] = None,
    warehouse_id: Optional[str] = None,
    acknowledged: bool = False,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get active stock alerts"""
    user = await get_current_user(credentials.credentials)
    
    filters = {"is_acknowledged": acknowledged}
    if alert_type:
        filters["alert_type"] = alert_type.value
    if warehouse_id:
        filters["warehouse_id"] = warehouse_id
    
    alerts = await supabase.query(
        "stock_alerts",
        filters=filters,
        order_by="created_at",
        ascending=False
    )
    
    return {"alerts": alerts}

@app.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Acknowledge a stock alert"""
    user = await get_current_user(credentials.credentials)
    
    await supabase.update("stock_alerts", {"id": alert_id}, {
        "is_acknowledged": True,
        "acknowledged_by": user["id"],
        "acknowledged_at": datetime.utcnow().isoformat()
    })
    
    return {"status": "acknowledged", "alert_id": alert_id}

@app.post("/alerts/config")
async def configure_alerts(
    config: StockAlertConfig,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Configure stock alert thresholds"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["admin", "warehouse_manager"])
    
    # Upsert alert configuration
    existing = await supabase.get_single("stock_alert_configs", {
        "product_id": config.product_id,
        "warehouse_id": config.warehouse_id
    })
    
    config_data = {
        "low_stock_threshold": config.low_stock_threshold,
        "reorder_point": config.reorder_point,
        "max_stock_level": config.max_stock_level
    }
    
    if existing:
        await supabase.update("stock_alert_configs", {"id": existing["id"]}, config_data)
    else:
        config_data.update({
            "id": str(uuid.uuid4()),
            "product_id": config.product_id,
            "warehouse_id": config.warehouse_id
        })
        await supabase.insert("stock_alert_configs", config_data)
    
    return {"status": "configured", "config": config_data}

# ============== SME PRODUCT MANAGEMENT ==============
@app.post("/products")
async def create_product(
    request: ProductCreate,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """SMEs can create products in their catalog"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["retailer", "admin"])
    
    # Create product
    product = await supabase.insert("products", {
        "id": str(uuid.uuid4()),
        "retailer_id": user["id"],
        "name": request.name,
        "description": request.description,
        "category_id": request.category_id,
        "sku": request.sku,
        "price": float(request.price),
        "compare_at_price": float(request.compare_at_price) if request.compare_at_price else None,
        "cost_per_unit": float(request.cost_per_unit) if request.cost_per_unit else None,
        "image_url": request.image_url,
        "tags": request.tags,
        "status": "active"
    })
    
    product_id = product["id"]
    
    # Create initial inventory if specified
    if request.initial_stock and request.initial_stock > 0:
        warehouse_id = request.warehouse_id or await _get_default_warehouse(user["id"])
        
        await supabase.insert("inventory", {
            "id": str(uuid.uuid4()),
            "product_id": product_id,
            "warehouse_id": warehouse_id,
            "quantity": request.initial_stock,
            "reserved_quantity": 0,
            "cost_per_unit": float(request.cost_per_unit) if request.cost_per_unit else 0
        })
        
        # Record stock movement
        await supabase.insert("stock_movements", {
            "id": str(uuid.uuid4()),
            "product_id": product_id,
            "warehouse_id": warehouse_id,
            "movement_type": "purchase",
            "quantity": request.initial_stock,
            "quantity_before": 0,
            "quantity_after": request.initial_stock,
            "performed_by": user["id"],
            "notes": "Initial stock"
        })
    
    return {
        "status": "created",
        "product": product,
        "initial_stock": request.initial_stock
    }

@app.get("/products")
async def list_products(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """List products for the authenticated retailer"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["retailer", "admin"])
    
    filters = {"retailer_id": user["id"]}
    if status:
        filters["status"] = status
    
    products = await supabase.query(
        "products",
        filters=filters,
        order_by="created_at",
        ascending=False,
        limit=limit,
        offset=offset
    )
    
    # Enrich with inventory data
    for product in products:
        inventory = await supabase.query("inventory", {"product_id": product["id"]})
        product["total_stock"] = sum(inv.get("quantity", 0) for inv in inventory)
        product["available_stock"] = sum(inv.get("available_quantity", 0) for inv in inventory)
    
    return {"products": products, "count": len(products)}

@app.get("/products/{product_id}")
async def get_product(
    product_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get product details with inventory"""
    user = await get_current_user(credentials.credentials)
    
    product = await supabase.get_single("products", {"id": product_id})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    # Check ownership for retailers
    if user.get("role") == "retailer" and product["retailer_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get inventory
    inventory = await supabase.query("inventory", {"product_id": product_id})
    product["inventory"] = inventory
    product["total_stock"] = sum(inv.get("quantity", 0) for inv in inventory)
    product["available_stock"] = sum(inv.get("available_quantity", 0) for inv in inventory)
    
    return {"product": product}

@app.patch("/products/{product_id}")
async def update_product(
    product_id: str,
    request: ProductUpdate,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Update product details"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["retailer", "admin"])
    
    # Verify ownership
    product = await supabase.get_single("products", {"id": product_id, "retailer_id": user["id"]})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    update_data = {k: v for k, v in request.dict(exclude_unset=True).items() if v is not None}
    if "price" in update_data:
        update_data["price"] = float(update_data["price"])
    if "compare_at_price" in update_data:
        update_data["compare_at_price"] = float(update_data["compare_at_price"])
    
    await supabase.update("products", {"id": product_id}, update_data)
    
    return {"status": "updated", "product_id": product_id}

@app.delete("/products/{product_id}")
async def archive_product(
    product_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Archive a product (soft delete)"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["retailer", "admin"])
    
    # Verify ownership
    product = await supabase.get_single("products", {"id": product_id, "retailer_id": user["id"]})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    await supabase.update("products", {"id": product_id}, {"status": "archived"})
    
    return {"status": "archived", "product_id": product_id}

# ============== Stock Management for SMEs ==============
@app.post("/products/{product_id}/stock/add")
async def add_stock(
    product_id: str,
    quantity: int = Field(..., gt=0),
    warehouse_id: Optional[str] = None,
    cost_per_unit: Optional[Decimal] = None,
    notes: Optional[str] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Add stock to a product"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["retailer", "admin"])
    
    # Verify product ownership
    product = await supabase.get_single("products", {"id": product_id, "retailer_id": user["id"]})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    warehouse_id = warehouse_id or await _get_default_warehouse(user["id"])
    
    # Get current inventory
    inventory = await supabase.get_single("inventory", {
        "product_id": product_id,
        "warehouse_id": warehouse_id
    })
    
    if not inventory:
        # Create new inventory record
        inventory = await supabase.insert("inventory", {
            "id": str(uuid.uuid4()),
            "product_id": product_id,
            "warehouse_id": warehouse_id,
            "quantity": quantity,
            "reserved_quantity": 0,
            "cost_per_unit": float(cost_per_unit) if cost_per_unit else 0
        })
        quantity_before = 0
        quantity_after = quantity
    else:
        quantity_before = inventory["quantity"]
        quantity_after = quantity_before + quantity
        await supabase.update("inventory", {"id": inventory["id"]}, {
            "quantity": quantity_after,
            "last_restock_date": datetime.utcnow().isoformat()
        })
    
    # Record movement
    await supabase.insert("stock_movements", {
        "id": str(uuid.uuid4()),
        "product_id": product_id,
        "warehouse_id": warehouse_id,
        "movement_type": "purchase",
        "quantity": quantity,
        "quantity_before": quantity_before,
        "quantity_after": quantity_after,
        "performed_by": user["id"],
        "notes": notes
    })
    
    return {
        "status": "stock_added",
        "product_id": product_id,
        "quantity_added": quantity,
        "new_quantity": quantity_after
    }

@app.post("/products/{product_id}/stock/adjust")
async def adjust_stock(
    product_id: str,
    adjustment: int,
    warehouse_id: Optional[str] = None,
    reason: Optional[str] = None,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Adjust stock (positive or negative)"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["retailer", "admin"])
    
    # Verify product ownership
    product = await supabase.get_single("products", {"id": product_id, "retailer_id": user["id"]})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    warehouse_id = warehouse_id or await _get_default_warehouse(user["id"])
    
    inventory = await supabase.get_single("inventory", {
        "product_id": product_id,
        "warehouse_id": warehouse_id
    })
    
    if not inventory:
        raise HTTPException(status_code=404, detail="Inventory record not found")
    
    quantity_before = inventory["quantity"]
    quantity_after = quantity_before + adjustment
    
    if quantity_after < 0:
        raise HTTPException(status_code=400, detail="Adjustment would result in negative stock")
    
    await supabase.update("inventory", {"id": inventory["id"]}, {
        "quantity": quantity_after
    })
    
    # Record movement
    await supabase.insert("stock_movements", {
        "id": str(uuid.uuid4()),
        "product_id": product_id,
        "warehouse_id": warehouse_id,
        "movement_type": "adjustment",
        "quantity": abs(adjustment),
        "quantity_before": quantity_before,
        "quantity_after": quantity_after,
        "performed_by": user["id"],
        "notes": reason
    })
    
    return {
        "status": "adjusted",
        "product_id": product_id,
        "adjustment": adjustment,
        "new_quantity": quantity_after
    }

# ============== Dashboard ==============
@app.get("/dashboard")
async def get_dashboard(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get SME dashboard with stock overview and alerts"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["retailer", "admin"])
    
    # Get product count
    products = await supabase.query("products", {"retailer_id": user["id"], "status": "active"})
    product_count = len(products)
    
    # Get low stock items
    low_stock_items = []
    for product in products:
        inventory = await supabase.query("inventory", {"product_id": product["id"]})
        total_available = sum(inv.get("available_quantity", 0) for inv in inventory)
        low_threshold = inventory[0].get("low_stock_threshold", 10) if inventory else 10
        
        if total_available <= low_threshold:
            low_stock_items.append({
                "product_id": product["id"],
                "product_name": product["name"],
                "available_quantity": total_available,
                "threshold": low_threshold
            })
    
    # Get unacknowledged alerts
    alerts = await supabase.query("stock_alerts", {
        "is_acknowledged": False
    })
    
    # Filter alerts for retailer's products
    retailer_alerts = []
    for alert in alerts:
        product = await supabase.get_single("products", {"id": alert["product_id"]})
        if product and product["retailer_id"] == user["id"]:
            retailer_alerts.append(alert)
    
    # Get recent sales (last 7 days)
    recent_sales = await supabase.rpc("get_retailer_sales_summary", {
        "p_retailer_id": user["id"],
        "p_days": 7
    })
    
    return {
        "product_count": product_count,
        "low_stock_count": len(low_stock_items),
        "low_stock_items": low_stock_items,
        "active_alerts": len(retailer_alerts),
        "alerts": retailer_alerts[:5],  # Top 5 alerts
        "recent_sales": recent_sales
    }

# ============== Real-time Subscriptions ==============
@app.get("/inventory/subscribe/{warehouse_id}")
async def get_realtime_config(
    warehouse_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get Supabase realtime subscription config for inventory updates"""
    user = await get_current_user(credentials.credentials)
    
    return {
        "channel": f"inventory:{warehouse_id}",
        "table": "inventory",
        "filter": f"warehouse_id=eq.{warehouse_id}",
        "events": ["UPDATE", "INSERT"]
    }

# ============== Helper Functions ==============
async def _check_stock_alerts(product_id: str, warehouse_id: str, quantity: int):
    """Check and create stock alerts based on thresholds"""
    config = await supabase.get_single("stock_alert_configs", {
        "product_id": product_id,
        "warehouse_id": warehouse_id
    })
    
    if not config:
        # Use default thresholds
        config = {"low_stock_threshold": 10, "reorder_point": 20, "max_stock_level": 1000}
    
    alert_type = None
    if quantity == 0:
        alert_type = AlertType.OUT_OF_STOCK.value
    elif quantity <= config["low_stock_threshold"]:
        alert_type = AlertType.LOW_STOCK.value
    elif config.get("max_stock_level") and quantity > config["max_stock_level"]:
        alert_type = AlertType.OVERSTOCK.value
    
    if alert_type:
        # Check if alert already exists
        existing = await supabase.get_single("stock_alerts", {
            "product_id": product_id,
            "warehouse_id": warehouse_id,
            "alert_type": alert_type,
            "is_acknowledged": False
        })
        
        if not existing:
            await supabase.insert("stock_alerts", {
                "id": str(uuid.uuid4()),
                "product_id": product_id,
                "warehouse_id": warehouse_id,
                "alert_type": alert_type,
                "current_quantity": quantity,
                "threshold": config.get("low_stock_threshold", 10)
            })

async def _get_default_warehouse(retailer_id: str) -> str:
    """Get or create default warehouse for retailer"""
    warehouse = await supabase.get_single("warehouses", {
        "retailer_id": retailer_id,
        "is_active": True
    })
    
    if warehouse:
        return warehouse["id"]
    
    # Create default warehouse
    new_warehouse = await supabase.insert("warehouses", {
        "id": str(uuid.uuid4()),
        "retailer_id": retailer_id,
        "name": "Main Warehouse",
        "is_active": True
    })
    
    return new_warehouse["id"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004)

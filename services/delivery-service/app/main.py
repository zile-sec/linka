"""
Linka Delivery Service - Supabase Integrated
Handles delivery management and real-time tracking
"""

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import os
import logging
import json

from shared.supabase_client import get_supabase_client
from shared.auth_middleware import (
    get_current_user,
    AuthenticatedUser,
    UserRole,
    require_roles
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Linka Delivery Service",
    description="Delivery management and real-time tracking",
    version="2.0.0"
)

# Store active WebSocket connections
active_connections: dict = {}

# ============ MODELS ============

class DeliveryAssign(BaseModel):
    driver_id: str

class DeliveryStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(accepted|picked_up|in_transit|arrived|delivered|failed)$")
    notes: Optional[str] = None
    failure_reason: Optional[str] = None

class LocationUpdate(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    accuracy: Optional[float] = None
    speed: Optional[float] = None
    heading: Optional[float] = None
    battery_level: Optional[int] = None

class DeliveryRating(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None
    punctuality_rating: Optional[int] = Field(None, ge=1, le=5)
    condition_rating: Optional[int] = Field(None, ge=1, le=5)

class DriverAvailabilityUpdate(BaseModel):
    is_available: bool

# ============ HEALTH CHECK ============

@app.get("/health")
async def health():
    return {"status": "alive", "service": "delivery-service", "version": "2.0.0"}

@app.get("/ready")
async def readiness():
    try:
        client = get_supabase_client()
        is_healthy = await client.health_check()
        if not is_healthy:
            return {"status": "not ready"}, 503
        return {"status": "ready", "service": "delivery-service"}
    except Exception as e:
        return {"status": "not ready", "detail": str(e)}, 503

# ============ DELIVERY ENDPOINTS ============

@app.get("/deliveries")
async def list_deliveries(
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """List deliveries based on user role"""
    try:
        client = get_supabase_client()
        
        # Build query based on role
        if user.role == UserRole.DRIVER:
            # Get driver record
            driver = await client.query(
                table="drivers",
                filters={"user_id": user.id},
                single=True
            )
            if not driver:
                raise HTTPException(status_code=404, detail="Driver profile not found")
            
            filters = {"driver_id": driver["id"]}
        elif user.role == UserRole.ADMIN:
            filters = {}
        else:
            # Customer - get deliveries for their orders
            orders = await client.query(
                table="orders",
                select="id",
                filters={"customer_id": user.id}
            )
            order_ids = [o["id"] for o in orders]
            
            if not order_ids:
                return {"deliveries": [], "count": 0}
            
            # Custom query for customer deliveries
            deliveries = []
            for order_id in order_ids[:limit]:
                delivery = await client.query(
                    table="deliveries",
                    filters={"order_id": order_id}
                )
                deliveries.extend(delivery)
            
            return {"deliveries": deliveries, "count": len(deliveries)}
        
        if status:
            filters["status"] = status
        
        deliveries = await client.query(
            table="deliveries",
            select="*, orders(order_number, customer_id)",
            filters=filters,
            order="created_at.desc",
            limit=limit,
            offset=offset
        )
        
        return {
            "deliveries": deliveries,
            "count": len(deliveries)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list deliveries: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/deliveries/{delivery_id}")
async def get_delivery(
    delivery_id: str,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """Get delivery details with tracking history"""
    try:
        client = get_supabase_client()
        
        delivery = await client.query(
            table="deliveries",
            select="*, orders(order_number, customer_id, retailer_id), drivers(user_id, rating_average)",
            filters={"id": delivery_id},
            single=True
        )
        
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        
        # Get tracking history
        tracking = await client.query(
            table="delivery_tracking",
            filters={"delivery_id": delivery_id},
            order="recorded_at.desc",
            limit=100
        )
        
        delivery["tracking_history"] = tracking
        
        return delivery
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get delivery: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/deliveries/{delivery_id}/assign")
async def assign_driver(
    delivery_id: str,
    data: DeliveryAssign,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_roles([UserRole.ADMIN, UserRole.RETAILER]))
):
    """Assign a driver to a delivery"""
    try:
        client = get_supabase_client()
        
        # Verify driver exists and is available
        driver = await client.query(
            table="drivers",
            filters={"id": data.driver_id, "status": "active", "is_available": True},
            single=True
        )
        
        if not driver:
            raise HTTPException(status_code=400, detail="Driver not available")
        
        # Update delivery
        await client.update(
            table="deliveries",
            data={
                "driver_id": data.driver_id,
                "status": "assigned",
                "assigned_at": datetime.utcnow().isoformat()
            },
            filters={"id": delivery_id}
        )
        
        # Notify driver
        background_tasks.add_task(
            notify_driver_assignment,
            driver["user_id"],
            delivery_id
        )
        
        return {"message": "Driver assigned successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to assign driver: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/deliveries/{delivery_id}/status")
async def update_delivery_status(
    delivery_id: str,
    update: DeliveryStatusUpdate,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """Update delivery status (drivers only for their deliveries)"""
    try:
        client = get_supabase_client()
        
        # Get delivery
        delivery = await client.query(
            table="deliveries",
            filters={"id": delivery_id},
            single=True
        )
        
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        
        # Verify driver owns this delivery
        if user.role == UserRole.DRIVER:
            driver = await client.query(
                table="drivers",
                filters={"user_id": user.id},
                single=True
            )
            if not driver or delivery["driver_id"] != driver["id"]:
                raise HTTPException(status_code=403, detail="Access denied")
        
        # Build update data
        update_data = {"status": update.status}
        
        if update.status == "accepted":
            update_data["accepted_at"] = datetime.utcnow().isoformat()
        elif update.status == "picked_up":
            update_data["picked_up_at"] = datetime.utcnow().isoformat()
        elif update.status == "delivered":
            update_data["delivered_at"] = datetime.utcnow().isoformat()
            # Update driver stats
            background_tasks.add_task(
                increment_driver_deliveries,
                delivery["driver_id"]
            )
        elif update.status == "failed":
            update_data["failure_reason"] = update.failure_reason
        
        if update.notes:
            update_data["driver_notes"] = update.notes
        
        await client.update(
            table="deliveries",
            data=update_data,
            filters={"id": delivery_id}
        )
        
        # Update order status if delivered
        if update.status == "delivered":
            await client.update(
                table="orders",
                data={"status": "delivered", "delivered_at": datetime.utcnow().isoformat()},
                filters={"id": delivery["order_id"]}
            )
        
        # Notify customer
        background_tasks.add_task(
            notify_delivery_status,
            delivery_id,
            update.status
        )
        
        return {"message": "Status updated successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update delivery status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/deliveries/{delivery_id}/location")
async def update_location(
    delivery_id: str,
    location: LocationUpdate,
    user: AuthenticatedUser = Depends(require_roles([UserRole.DRIVER]))
):
    """Update driver location for a delivery (real-time tracking)"""
    try:
        client = get_supabase_client()
        
        # Verify driver owns this delivery
        driver = await client.query(
            table="drivers",
            filters={"user_id": user.id},
            single=True
        )
        
        delivery = await client.query(
            table="deliveries",
            filters={"id": delivery_id},
            single=True
        )
        
        if not delivery or delivery["driver_id"] != driver["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Insert tracking point
        await client.insert(
            table="delivery_tracking",
            data={
                "delivery_id": delivery_id,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "accuracy": location.accuracy,
                "speed": location.speed,
                "heading": location.heading,
                "battery_level": location.battery_level,
                "status": delivery["status"]
            },
            return_data=False
        )
        
        # Update driver's current location
        await client.update(
            table="drivers",
            data={
                "current_latitude": location.latitude,
                "current_longitude": location.longitude,
                "last_location_update": datetime.utcnow().isoformat()
            },
            filters={"id": driver["id"]}
        )
        
        # Broadcast to connected WebSocket clients
        if delivery_id in active_connections:
            for websocket in active_connections[delivery_id]:
                try:
                    await websocket.send_json({
                        "type": "location_update",
                        "delivery_id": delivery_id,
                        "latitude": location.latitude,
                        "longitude": location.longitude,
                        "speed": location.speed,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                except Exception:
                    pass
        
        return {"message": "Location updated"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update location: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/deliveries/{delivery_id}/rate")
async def rate_delivery(
    delivery_id: str,
    rating: DeliveryRating,
    user: AuthenticatedUser = Depends(get_current_user)
):
    """Rate a completed delivery"""
    try:
        client = get_supabase_client()
        
        # Get delivery and verify ownership
        delivery = await client.query(
            table="deliveries",
            select="*, orders(customer_id)",
            filters={"id": delivery_id},
            single=True
        )
        
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        
        if delivery["orders"]["customer_id"] != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        if delivery["status"] != "delivered":
            raise HTTPException(status_code=400, detail="Can only rate delivered orders")
        
        # Check if already rated
        existing = await client.query(
            table="delivery_ratings",
            filters={"delivery_id": delivery_id}
        )
        
        if existing:
            raise HTTPException(status_code=400, detail="Already rated")
        
        # Create rating
        await client.insert(
            table="delivery_ratings",
            data={
                "delivery_id": delivery_id,
                "driver_id": delivery["driver_id"],
                "customer_id": user.id,
                "rating": rating.rating,
                "comment": rating.comment,
                "punctuality_rating": rating.punctuality_rating,
                "condition_rating": rating.condition_rating
            }
        )
        
        return {"message": "Rating submitted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to rate delivery: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ DRIVER ENDPOINTS ============

@app.get("/driver/profile")
async def get_driver_profile(
    user: AuthenticatedUser = Depends(require_roles([UserRole.DRIVER]))
):
    """Get driver profile and stats"""
    try:
        client = get_supabase_client()
        
        driver = await client.query(
            table="drivers",
            filters={"user_id": user.id},
            single=True
        )
        
        if not driver:
            raise HTTPException(status_code=404, detail="Driver profile not found")
        
        return driver
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get driver profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/driver/availability")
async def update_availability(
    data: DriverAvailabilityUpdate,
    user: AuthenticatedUser = Depends(require_roles([UserRole.DRIVER]))
):
    """Update driver availability status"""
    try:
        client = get_supabase_client()
        
        await client.update(
            table="drivers",
            data={"is_available": data.is_available},
            filters={"user_id": user.id}
        )
        
        return {"message": "Availability updated", "is_available": data.is_available}
        
    except Exception as e:
        logger.error(f"Failed to update availability: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ WEBSOCKET FOR REAL-TIME TRACKING ============

@app.websocket("/ws/track/{delivery_id}")
async def track_delivery_websocket(websocket: WebSocket, delivery_id: str):
    """WebSocket endpoint for real-time delivery tracking"""
    await websocket.accept()
    
    # Add to active connections
    if delivery_id not in active_connections:
        active_connections[delivery_id] = []
    active_connections[delivery_id].append(websocket)
    
    try:
        while True:
            # Keep connection alive
            data = await websocket.receive_text()
            
            # Handle ping
            if data == "ping":
                await websocket.send_text("pong")
                
    except WebSocketDisconnect:
        active_connections[delivery_id].remove(websocket)
        if not active_connections[delivery_id]:
            del active_connections[delivery_id]


# ============ HELPER FUNCTIONS ============

async def notify_driver_assignment(driver_user_id: str, delivery_id: str):
    """Send notification to driver about new assignment"""
    try:
        client = get_supabase_client()
        
        await client.insert(
            table="notifications",
            data={
                "user_id": driver_user_id,
                "title": "New Delivery Assignment",
                "body": "You have been assigned a new delivery. Tap to view details.",
                "type": "info",
                "category": "delivery",
                "reference_type": "delivery",
                "reference_id": delivery_id
            },
            return_data=False
        )
    except Exception as e:
        logger.error(f"Failed to notify driver: {e}")


async def notify_delivery_status(delivery_id: str, status: str):
    """Notify customer about delivery status change"""
    try:
        client = get_supabase_client()
        
        delivery = await client.query(
            table="deliveries",
            select="orders(customer_id, order_number)",
            filters={"id": delivery_id},
            single=True
        )
        
        status_messages = {
            "accepted": "Driver has accepted your delivery",
            "picked_up": "Your order has been picked up",
            "in_transit": "Your order is on the way",
            "arrived": "Driver has arrived at your location",
            "delivered": "Your order has been delivered",
            "failed": "Delivery attempt failed"
        }
        
        await client.insert(
            table="notifications",
            data={
                "user_id": delivery["orders"]["customer_id"],
                "title": f"Delivery Update",
                "body": status_messages.get(status, f"Delivery status: {status}"),
                "type": "info" if status != "failed" else "warning",
                "category": "delivery",
                "reference_type": "delivery",
                "reference_id": delivery_id
            },
            return_data=False
        )
    except Exception as e:
        logger.error(f"Failed to notify customer: {e}")


async def increment_driver_deliveries(driver_id: str):
    """Increment completed deliveries count for driver"""
    try:
        client = get_supabase_client()
        
        driver = await client.query(
            table="drivers",
            filters={"id": driver_id},
            single=True
        )
        
        await client.update(
            table="drivers",
            data={"completed_deliveries": driver["completed_deliveries"] + 1},
            filters={"id": driver_id}
        )
    except Exception as e:
        logger.error(f"Failed to update driver stats: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

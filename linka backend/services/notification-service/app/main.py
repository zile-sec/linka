from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from enum import Enum
import os
from datetime import datetime
import uuid
import json
import asyncio

# Packages are available via PYTHONPATH
from shared.supabase_client import SupabaseClient
from shared.auth_middleware import get_current_user, require_roles

app = FastAPI(title="Linka Notification Service")
security = HTTPBearer()

# Initialize Supabase client
supabase = SupabaseClient()

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket
    
    def disconnect(self, user_id: str):
        self.active_connections.pop(user_id, None)
    
    async def send_to_user(self, user_id: str, message: dict):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_json(message)
    
    async def broadcast(self, message: dict, user_ids: List[str] = None):
        targets = user_ids if user_ids else self.active_connections.keys()
        for user_id in targets:
            if user_id in self.active_connections:
                await self.active_connections[user_id].send_json(message)

manager = ConnectionManager()

# ============== Enums ==============
class NotificationType(str, Enum):
    ORDER_PLACED = "order_placed"
    ORDER_CONFIRMED = "order_confirmed"
    ORDER_SHIPPED = "order_shipped"
    ORDER_DELIVERED = "order_delivered"
    ORDER_CANCELLED = "order_cancelled"
    PAYMENT_RECEIVED = "payment_received"
    PAYMENT_FAILED = "payment_failed"
    DELIVERY_ASSIGNED = "delivery_assigned"
    DELIVERY_STARTED = "delivery_started"
    DELIVERY_COMPLETED = "delivery_completed"
    LOW_STOCK_ALERT = "low_stock_alert"
    KYC_APPROVED = "kyc_approved"
    KYC_REJECTED = "kyc_rejected"
    PROMOTION = "promotion"
    SYSTEM = "system"

class NotificationChannel(str, Enum):
    PUSH = "push"
    SMS = "sms"
    EMAIL = "email"
    IN_APP = "in_app"

class NotificationPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

# ============== Pydantic Models ==============
class NotificationRequest(BaseModel):
    user_id: str
    notification_type: NotificationType
    title: str
    body: str
    channels: List[NotificationChannel] = [NotificationChannel.IN_APP, NotificationChannel.PUSH]
    priority: NotificationPriority = NotificationPriority.MEDIUM
    data: Optional[dict] = None
    action_url: Optional[str] = None

class BulkNotificationRequest(BaseModel):
    user_ids: List[str]
    notification_type: NotificationType
    title: str
    body: str
    channels: List[NotificationChannel] = [NotificationChannel.IN_APP]
    priority: NotificationPriority = NotificationPriority.MEDIUM
    data: Optional[dict] = None

class NotificationPreferences(BaseModel):
    push_enabled: bool = True
    sms_enabled: bool = True
    email_enabled: bool = True
    order_updates: bool = True
    delivery_updates: bool = True
    payment_updates: bool = True
    promotions: bool = False
    quiet_hours_start: Optional[str] = None  # HH:MM format
    quiet_hours_end: Optional[str] = None

class DeviceTokenRequest(BaseModel):
    token: str
    platform: str  # ios, android, web
    device_name: Optional[str] = None

# ============== Health Check ==============
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "notification-service", "timestamp": datetime.utcnow().isoformat()}

# ============== WebSocket for Real-time ==============
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """WebSocket endpoint for real-time notifications"""
    await manager.connect(user_id, websocket)
    try:
        # Subscribe to Supabase realtime for this user
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif message.get("type") == "mark_read":
                notification_id = message.get("notification_id")
                if notification_id:
                    await supabase.update("notifications", {"id": notification_id}, {"is_read": True, "read_at": datetime.utcnow().isoformat()})
    except WebSocketDisconnect:
        manager.disconnect(user_id)

# ============== Send Notifications ==============
@app.post("/notifications/send")
async def send_notification(
    request: NotificationRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Send a notification to a user"""
    user = await get_current_user(credentials.credentials)
    
    # Check user preferences
    preferences = await _get_user_preferences(request.user_id)
    
    # Check quiet hours
    if _is_quiet_hours(preferences) and request.priority != NotificationPriority.URGENT:
        # Queue for later delivery
        background_tasks.add_task(_queue_notification, request)
        return {"status": "queued", "message": "Notification queued for quiet hours"}
    
    # Create notification record
    notification = await supabase.insert("notifications", {
        "id": str(uuid.uuid4()),
        "user_id": request.user_id,
        "type": request.notification_type.value,
        "title": request.title,
        "body": request.body,
        "data": request.data or {},
        "action_url": request.action_url,
        "priority": request.priority.value,
        "is_read": False
    })
    
    # Send via enabled channels
    delivery_results = {}
    for channel in request.channels:
        if _is_channel_enabled(preferences, channel, request.notification_type):
            background_tasks.add_task(
                _deliver_notification, 
                notification["id"], 
                request.user_id, 
                channel, 
                request.title, 
                request.body,
                request.data
            )
            delivery_results[channel.value] = "queued"
        else:
            delivery_results[channel.value] = "disabled"
    
    # Send real-time via WebSocket
    await manager.send_to_user(request.user_id, {
        "type": "notification",
        "notification": notification
    })
    
    return {
        "status": "sent",
        "notification_id": notification["id"],
        "channels": delivery_results
    }

@app.post("/notifications/bulk")
async def send_bulk_notification(
    request: BulkNotificationRequest,
    background_tasks: BackgroundTasks,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Send notification to multiple users"""
    user = await get_current_user(credentials.credentials)
    await require_roles(user["id"], ["admin", "marketing"])
    
    notification_ids = []
    for user_id in request.user_ids:
        notification = await supabase.insert("notifications", {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "type": request.notification_type.value,
            "title": request.title,
            "body": request.body,
            "data": request.data or {},
            "priority": request.priority.value,
            "is_read": False
        })
        notification_ids.append(notification["id"])
        
        # Send via WebSocket if connected
        await manager.send_to_user(user_id, {
            "type": "notification",
            "notification": notification
        })
    
    # Queue channel deliveries
    for channel in request.channels:
        background_tasks.add_task(_deliver_bulk, notification_ids, channel)
    
    return {
        "status": "sent",
        "count": len(notification_ids),
        "notification_ids": notification_ids
    }

# ============== Get Notifications ==============
@app.get("/notifications")
async def get_notifications(
    unread_only: bool = False,
    notification_type: Optional[NotificationType] = None,
    limit: int = 20,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get user's notifications"""
    user = await get_current_user(credentials.credentials)
    
    filters = {"user_id": user["id"]}
    if unread_only:
        filters["is_read"] = False
    if notification_type:
        filters["type"] = notification_type.value
    
    notifications = await supabase.query(
        "notifications",
        filters=filters,
        order_by="created_at",
        ascending=False,
        limit=limit,
        offset=offset
    )
    
    # Get unread count
    unread_count = await supabase.rpc("get_unread_notification_count", {"p_user_id": user["id"]})
    
    return {
        "notifications": notifications,
        "unread_count": unread_count.get("count", 0),
        "limit": limit,
        "offset": offset
    }

@app.get("/notifications/{notification_id}")
async def get_notification(
    notification_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get a specific notification"""
    user = await get_current_user(credentials.credentials)
    
    notification = await supabase.get_single("notifications", {
        "id": notification_id,
        "user_id": user["id"]
    })
    
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    return {"notification": notification}

# ============== Mark as Read ==============
@app.post("/notifications/{notification_id}/read")
async def mark_as_read(
    notification_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Mark a notification as read"""
    user = await get_current_user(credentials.credentials)
    
    await supabase.update("notifications", 
        {"id": notification_id, "user_id": user["id"]}, 
        {"is_read": True, "read_at": datetime.utcnow().isoformat()}
    )
    
    return {"status": "marked_read", "notification_id": notification_id}

@app.post("/notifications/read-all")
async def mark_all_as_read(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Mark all notifications as read"""
    user = await get_current_user(credentials.credentials)
    
    result = await supabase.rpc("mark_all_notifications_read", {"p_user_id": user["id"]})
    
    return {"status": "all_marked_read", "count": result.get("count", 0)}

# ============== Device Tokens ==============
@app.post("/devices/register")
async def register_device(
    request: DeviceTokenRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Register a device for push notifications"""
    user = await get_current_user(credentials.credentials)
    
    # Check if token already exists
    existing = await supabase.get_single("device_tokens", {"token": request.token})
    
    if existing:
        if existing["user_id"] != user["id"]:
            # Token belongs to another user, update it
            await supabase.update("device_tokens", {"id": existing["id"]}, {
                "user_id": user["id"],
                "device_name": request.device_name,
                "is_active": True
            })
        return {"status": "updated", "device_id": existing["id"]}
    
    device = await supabase.insert("device_tokens", {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "token": request.token,
        "platform": request.platform,
        "device_name": request.device_name,
        "is_active": True
    })
    
    return {"status": "registered", "device_id": device["id"]}

@app.delete("/devices/{token}")
async def unregister_device(
    token: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Unregister a device token"""
    user = await get_current_user(credentials.credentials)
    
    await supabase.update("device_tokens", 
        {"token": token, "user_id": user["id"]}, 
        {"is_active": False}
    )
    
    return {"status": "unregistered"}

# ============== Preferences ==============
@app.get("/preferences")
async def get_preferences(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Get user's notification preferences"""
    user = await get_current_user(credentials.credentials)
    
    preferences = await supabase.get_single("notification_preferences", {"user_id": user["id"]})
    
    if not preferences:
        # Return defaults
        return {"preferences": NotificationPreferences().dict()}
    
    return {"preferences": preferences}

@app.put("/preferences")
async def update_preferences(
    request: NotificationPreferences,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Update notification preferences"""
    user = await get_current_user(credentials.credentials)
    
    existing = await supabase.get_single("notification_preferences", {"user_id": user["id"]})
    
    pref_data = request.dict()
    
    if existing:
        await supabase.update("notification_preferences", {"id": existing["id"]}, pref_data)
    else:
        pref_data["id"] = str(uuid.uuid4())
        pref_data["user_id"] = user["id"]
        await supabase.insert("notification_preferences", pref_data)
    
    return {"status": "updated", "preferences": pref_data}

# ============== Event Handlers (Internal) ==============
@app.post("/internal/events/order")
async def handle_order_event(
    payload: dict,
    background_tasks: BackgroundTasks
):
    """Handle order events from order service"""
    event_type = payload.get("event")
    order = payload.get("order")
    user_id = order.get("user_id")
    
    notification_map = {
        "order_placed": (NotificationType.ORDER_PLACED, "Order Placed", f"Your order #{order.get('order_number')} has been placed"),
        "order_confirmed": (NotificationType.ORDER_CONFIRMED, "Order Confirmed", f"Your order #{order.get('order_number')} has been confirmed"),
        "order_shipped": (NotificationType.ORDER_SHIPPED, "Order Shipped", f"Your order #{order.get('order_number')} is on the way"),
        "order_delivered": (NotificationType.ORDER_DELIVERED, "Order Delivered", f"Your order #{order.get('order_number')} has been delivered"),
        "order_cancelled": (NotificationType.ORDER_CANCELLED, "Order Cancelled", f"Your order #{order.get('order_number')} has been cancelled")
    }
    
    if event_type in notification_map:
        notif_type, title, body = notification_map[event_type]
        request = NotificationRequest(
            user_id=user_id,
            notification_type=notif_type,
            title=title,
            body=body,
            channels=[NotificationChannel.IN_APP, NotificationChannel.PUSH, NotificationChannel.SMS],
            priority=NotificationPriority.HIGH,
            data={"order_id": order.get("id"), "order_number": order.get("order_number")}
        )
        background_tasks.add_task(_send_notification_internal, request)
    
    return {"status": "processed"}

@app.post("/internal/events/delivery")
async def handle_delivery_event(
    payload: dict,
    background_tasks: BackgroundTasks
):
    """Handle delivery events"""
    event_type = payload.get("event")
    delivery = payload.get("delivery")
    user_id = delivery.get("customer_id")
    
    notification_map = {
        "delivery_assigned": (NotificationType.DELIVERY_ASSIGNED, "Driver Assigned", f"A driver has been assigned to your delivery"),
        "delivery_started": (NotificationType.DELIVERY_STARTED, "Delivery Started", f"Your order is being delivered"),
        "delivery_completed": (NotificationType.DELIVERY_COMPLETED, "Delivery Complete", f"Your order has been delivered")
    }
    
    if event_type in notification_map:
        notif_type, title, body = notification_map[event_type]
        request = NotificationRequest(
            user_id=user_id,
            notification_type=notif_type,
            title=title,
            body=body,
            channels=[NotificationChannel.IN_APP, NotificationChannel.PUSH],
            priority=NotificationPriority.HIGH,
            data={"delivery_id": delivery.get("id")}
        )
        background_tasks.add_task(_send_notification_internal, request)
    
    return {"status": "processed"}

# ============== Helper Functions ==============
async def _get_user_preferences(user_id: str) -> dict:
    """Get user notification preferences"""
    preferences = await supabase.get_single("notification_preferences", {"user_id": user_id})
    return preferences or NotificationPreferences().dict()

def _is_quiet_hours(preferences: dict) -> bool:
    """Check if current time is within quiet hours"""
    start = preferences.get("quiet_hours_start")
    end = preferences.get("quiet_hours_end")
    
    if not start or not end:
        return False
    
    now = datetime.utcnow().strftime("%H:%M")
    return start <= now <= end

def _is_channel_enabled(preferences: dict, channel: NotificationChannel, notif_type: NotificationType) -> bool:
    """Check if channel is enabled for notification type"""
    channel_key = f"{channel.value}_enabled"
    if not preferences.get(channel_key, True):
        return False
    
    # Check type-specific settings
    type_settings = {
        NotificationType.ORDER_PLACED: "order_updates",
        NotificationType.ORDER_CONFIRMED: "order_updates",
        NotificationType.ORDER_SHIPPED: "order_updates",
        NotificationType.ORDER_DELIVERED: "delivery_updates",
        NotificationType.DELIVERY_ASSIGNED: "delivery_updates",
        NotificationType.DELIVERY_STARTED: "delivery_updates",
        NotificationType.DELIVERY_COMPLETED: "delivery_updates",
        NotificationType.PAYMENT_RECEIVED: "payment_updates",
        NotificationType.PAYMENT_FAILED: "payment_updates",
        NotificationType.PROMOTION: "promotions"
    }
    
    setting_key = type_settings.get(notif_type)
    if setting_key:
        return preferences.get(setting_key, True)
    
    return True

async def _deliver_notification(notification_id: str, user_id: str, channel: NotificationChannel, title: str, body: str, data: dict):
    """Deliver notification via specific channel"""
    try:
        if channel == NotificationChannel.PUSH:
            await _send_push_notification(user_id, title, body, data)
        elif channel == NotificationChannel.SMS:
            await _send_sms(user_id, body)
        elif channel == NotificationChannel.EMAIL:
            await _send_email(user_id, title, body)
        
        # Update delivery status
        await supabase.insert("notification_deliveries", {
            "id": str(uuid.uuid4()),
            "notification_id": notification_id,
            "channel": channel.value,
            "status": "delivered",
            "delivered_at": datetime.utcnow().isoformat()
        })
    except Exception as e:
        await supabase.insert("notification_deliveries", {
            "id": str(uuid.uuid4()),
            "notification_id": notification_id,
            "channel": channel.value,
            "status": "failed",
            "error": str(e)
        })

async def _send_push_notification(user_id: str, title: str, body: str, data: dict):
    """Send push notification via FCM/APNs"""
    # Get user's device tokens
    devices = await supabase.query("device_tokens", {"user_id": user_id, "is_active": True})
    
    for device in devices:
        # Integration with Firebase/APNs would go here
        pass

async def _send_sms(user_id: str, body: str):
    """Send SMS notification"""
    profile = await supabase.get_single("user_profiles", {"id": user_id})
    if profile and profile.get("phone"):
        # Integration with SMS provider (e.g., Twilio) would go here
        pass

async def _send_email(user_id: str, title: str, body: str):
    """Send email notification"""
    profile = await supabase.get_single("user_profiles", {"id": user_id})
    if profile and profile.get("email"):
        # Integration with email service would go here
        pass

async def _queue_notification(request: NotificationRequest):
    """Queue notification for later delivery"""
    await supabase.insert("notification_queue", {
        "id": str(uuid.uuid4()),
        "user_id": request.user_id,
        "notification_type": request.notification_type.value,
        "title": request.title,
        "body": request.body,
        "channels": [c.value for c in request.channels],
        "priority": request.priority.value,
        "data": request.data,
        "scheduled_for": None  # Will be processed after quiet hours
    })

async def _send_notification_internal(request: NotificationRequest):
    """Internal notification sending without auth"""
    notification = await supabase.insert("notifications", {
        "id": str(uuid.uuid4()),
        "user_id": request.user_id,
        "type": request.notification_type.value,
        "title": request.title,
        "body": request.body,
        "data": request.data or {},
        "priority": request.priority.value,
        "is_read": False
    })
    
    await manager.send_to_user(request.user_id, {
        "type": "notification",
        "notification": notification
    })

async def _deliver_bulk(notification_ids: List[str], channel: NotificationChannel):
    """Bulk deliver notifications"""
    for notif_id in notification_ids:
        notification = await supabase.get_single("notifications", {"id": notif_id})
        if notification:
            await _deliver_notification(
                notif_id, 
                notification["user_id"], 
                channel, 
                notification["title"], 
                notification["body"],
                notification.get("data", {})
            )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)

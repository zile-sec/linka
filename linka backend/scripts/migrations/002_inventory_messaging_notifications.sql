-- ============================================
-- INVENTORY & NOTIFICATION ENHANCEMENTS
-- For SME marketplace with messaging
-- ============================================

-- ============ SME PRODUCTS & STOCK MANAGEMENT ============

-- Products table for SME inventory
CREATE TABLE IF NOT EXISTS public.products (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  retailer_id UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  category_id UUID,
  name TEXT NOT NULL,
  description TEXT,
  sku TEXT UNIQUE,
  price DECIMAL(12,2) NOT NULL CHECK (price >= 0),
  compare_at_price DECIMAL(12,2) CHECK (compare_at_price >= price),
  cost_per_unit DECIMAL(12,2),
  status TEXT DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'draft', 'archived')),
  image_url TEXT,
  images JSONB DEFAULT '[]'::jsonb,
  tags TEXT[] DEFAULT '{}',
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Product variants (sizes, colors, etc.)
CREATE TABLE IF NOT EXISTS public.product_variants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id UUID NOT NULL REFERENCES public.products(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sku TEXT UNIQUE,
  price DECIMAL(12,2) NOT NULL,
  compare_at_price DECIMAL(12,2),
  image_url TEXT,
  attributes JSONB DEFAULT '{}'::jsonb, -- {size: "M", color: "Red"}
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Inventory tracking per warehouse/location
CREATE TABLE IF NOT EXISTS public.inventory (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id UUID NOT NULL REFERENCES public.products(id) ON DELETE CASCADE,
  variant_id UUID REFERENCES public.product_variants(id) ON DELETE CASCADE,
  warehouse_id UUID REFERENCES public.warehouses(id),
  quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
  reserved_quantity INTEGER NOT NULL DEFAULT 0 CHECK (reserved_quantity >= 0),
  available_quantity INTEGER GENERATED ALWAYS AS (quantity - reserved_quantity) STORED,
  low_stock_threshold INTEGER DEFAULT 10,
  reorder_point INTEGER DEFAULT 20,
  cost_per_unit DECIMAL(12,2),
  last_restock_date TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(product_id, variant_id, warehouse_id)
);

-- Warehouses/Locations
CREATE TABLE IF NOT EXISTS public.warehouses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  retailer_id UUID REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  address TEXT,
  city TEXT,
  province TEXT,
  latitude DECIMAL(10,8),
  longitude DECIMAL(11,8),
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Stock movements history
CREATE TABLE IF NOT EXISTS public.stock_movements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id UUID NOT NULL REFERENCES public.products(id),
  variant_id UUID REFERENCES public.product_variants(id),
  warehouse_id UUID REFERENCES public.warehouses(id),
  movement_type TEXT NOT NULL CHECK (movement_type IN ('sale', 'purchase', 'return', 'adjustment', 'transfer', 'damaged')),
  quantity INTEGER NOT NULL,
  quantity_before INTEGER NOT NULL,
  quantity_after INTEGER NOT NULL,
  reference_type TEXT, -- 'order', 'purchase_order', etc.
  reference_id UUID,
  notes TEXT,
  performed_by UUID REFERENCES public.user_profiles(id),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Stock alerts for low inventory
CREATE TABLE IF NOT EXISTS public.stock_alerts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id UUID NOT NULL REFERENCES public.products(id) ON DELETE CASCADE,
  variant_id UUID REFERENCES public.product_variants(id),
  warehouse_id UUID REFERENCES public.warehouses(id),
  alert_type TEXT NOT NULL CHECK (alert_type IN ('low_stock', 'out_of_stock', 'overstock')),
  current_quantity INTEGER NOT NULL,
  threshold INTEGER,
  is_acknowledged BOOLEAN DEFAULT false,
  acknowledged_by UUID REFERENCES public.user_profiles(id),
  acknowledged_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============ IN-APP MESSAGING SYSTEM ============

-- Conversations between users (SME <-> Customer)
CREATE TABLE IF NOT EXISTS public.conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_1 UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  participant_2 UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  order_id UUID REFERENCES public.orders(id) ON DELETE SET NULL, -- Optional order context
  product_id UUID REFERENCES public.products(id) ON DELETE SET NULL, -- Optional product inquiry
  last_message_at TIMESTAMPTZ,
  last_message_preview TEXT,
  is_archived BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(participant_1, participant_2, order_id),
  CHECK (participant_1 < participant_2) -- Ensure consistent ordering
);

-- Messages within conversations
CREATE TABLE IF NOT EXISTS public.messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
  sender_id UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  message_type TEXT DEFAULT 'text' CHECK (message_type IN ('text', 'image', 'file', 'system')),
  content TEXT NOT NULL,
  media_url TEXT,
  metadata JSONB DEFAULT '{}'::jsonb,
  is_read BOOLEAN DEFAULT false,
  read_at TIMESTAMPTZ,
  is_deleted BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Message read receipts
CREATE TABLE IF NOT EXISTS public.message_read_receipts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id UUID NOT NULL REFERENCES public.messages(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  read_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(message_id, user_id)
);

-- ============ NOTIFICATIONS TABLE ============

CREATE TABLE IF NOT EXISTS public.notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK (type IN (
    'order_placed', 'order_confirmed', 'order_shipped', 'order_delivered', 'order_cancelled',
    'payment_received', 'payment_failed', 'low_stock', 'out_of_stock', 
    'delivery_assigned', 'delivery_started', 'delivery_completed',
    'new_message', 'message_reply', 'customer_inquiry', 
    'kyc_approved', 'kyc_rejected', 'promotion', 'system'
  )),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  data JSONB DEFAULT '{}'::jsonb,
  action_url TEXT,
  category TEXT DEFAULT 'general' CHECK (category IN ('order', 'payment', 'inventory', 'message', 'delivery', 'general')),
  priority TEXT DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
  reference_type TEXT, -- 'order', 'message', 'product', etc.
  reference_id UUID,
  is_read BOOLEAN DEFAULT false,
  read_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Notification preferences per user
CREATE TABLE IF NOT EXISTS public.notification_preferences (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL UNIQUE REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  push_enabled BOOLEAN DEFAULT true,
  sms_enabled BOOLEAN DEFAULT true,
  email_enabled BOOLEAN DEFAULT true,
  order_updates BOOLEAN DEFAULT true,
  delivery_updates BOOLEAN DEFAULT true,
  payment_updates BOOLEAN DEFAULT true,
  inventory_alerts BOOLEAN DEFAULT true,
  message_notifications BOOLEAN DEFAULT true,
  promotions BOOLEAN DEFAULT false,
  quiet_hours_start TIME,
  quiet_hours_end TIME,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Device tokens for push notifications
CREATE TABLE IF NOT EXISTS public.device_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,
  platform TEXT NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
  device_name TEXT,
  is_active BOOLEAN DEFAULT true,
  last_used_at TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Orders table (if not exists)
CREATE TABLE IF NOT EXISTS public.orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_number TEXT UNIQUE NOT NULL,
  customer_id UUID NOT NULL REFERENCES public.user_profiles(id),
  retailer_id UUID NOT NULL REFERENCES public.user_profiles(id),
  status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'processing', 'ready_for_pickup', 'out_for_delivery', 'delivered', 'cancelled')),
  fulfillment_status TEXT DEFAULT 'unfulfilled' CHECK (fulfillment_status IN ('unfulfilled', 'partially_fulfilled', 'fulfilled')),
  payment_status TEXT DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
  payment_method TEXT,
  subtotal DECIMAL(12,2) NOT NULL,
  tax_amount DECIMAL(12,2) DEFAULT 0,
  shipping_amount DECIMAL(12,2) DEFAULT 0,
  total_amount DECIMAL(12,2) NOT NULL,
  shipping_address JSONB NOT NULL,
  billing_address JSONB,
  customer_notes TEXT,
  cancellation_reason TEXT,
  confirmed_at TIMESTAMPTZ,
  shipped_at TIMESTAMPTZ,
  delivered_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Order items
CREATE TABLE IF NOT EXISTS public.order_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES public.orders(id) ON DELETE CASCADE,
  product_id UUID NOT NULL REFERENCES public.products(id),
  variant_id UUID REFERENCES public.product_variants(id),
  warehouse_id UUID REFERENCES public.warehouses(id),
  product_name TEXT NOT NULL,
  variant_name TEXT,
  sku TEXT,
  image_url TEXT,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  unit_price DECIMAL(12,2) NOT NULL,
  discount_amount DECIMAL(12,2) DEFAULT 0,
  tax_amount DECIMAL(12,2) DEFAULT 0,
  total_price DECIMAL(12,2) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============ INDEXES FOR PERFORMANCE ============

CREATE INDEX IF NOT EXISTS idx_products_retailer ON public.products(retailer_id);
CREATE INDEX IF NOT EXISTS idx_products_status ON public.products(status);
CREATE INDEX IF NOT EXISTS idx_inventory_product ON public.inventory(product_id);
CREATE INDEX IF NOT EXISTS idx_inventory_warehouse ON public.inventory(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_inventory_low_stock ON public.inventory(available_quantity) WHERE available_quantity <= low_stock_threshold;
CREATE INDEX IF NOT EXISTS idx_stock_movements_product ON public.stock_movements(product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_alerts_unacked ON public.stock_alerts(is_acknowledged, created_at DESC) WHERE NOT is_acknowledged;
CREATE INDEX IF NOT EXISTS idx_conversations_participants ON public.conversations(participant_1, participant_2);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON public.messages(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON public.messages(conversation_id, is_read) WHERE NOT is_read;
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON public.notifications(user_id, is_read, created_at DESC) WHERE NOT is_read;
CREATE INDEX IF NOT EXISTS idx_orders_customer ON public.orders(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_retailer ON public.orders(retailer_id, created_at DESC);

-- ============ ROW LEVEL SECURITY ============

ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.warehouses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stock_movements ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stock_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;

-- RLS Policies for Products
CREATE POLICY "Anyone can view active products"
  ON public.products FOR SELECT
  USING (status = 'active');

CREATE POLICY "Retailers can manage their products"
  ON public.products FOR ALL
  USING (retailer_id = auth.uid());

-- RLS Policies for Inventory
CREATE POLICY "Retailers can view their inventory"
  ON public.inventory FOR SELECT
  USING (
    product_id IN (SELECT id FROM public.products WHERE retailer_id = auth.uid())
  );

CREATE POLICY "Retailers can manage their inventory"
  ON public.inventory FOR ALL
  USING (
    product_id IN (SELECT id FROM public.products WHERE retailer_id = auth.uid())
  );

-- RLS for Conversations
CREATE POLICY "Users can view their conversations"
  ON public.conversations FOR SELECT
  USING (participant_1 = auth.uid() OR participant_2 = auth.uid());

CREATE POLICY "Users can create conversations"
  ON public.conversations FOR INSERT
  WITH CHECK (participant_1 = auth.uid() OR participant_2 = auth.uid());

-- RLS for Messages
CREATE POLICY "Users can view messages in their conversations"
  ON public.messages FOR SELECT
  USING (
    conversation_id IN (
      SELECT id FROM public.conversations
      WHERE participant_1 = auth.uid() OR participant_2 = auth.uid()
    )
  );

CREATE POLICY "Users can send messages"
  ON public.messages FOR INSERT
  WITH CHECK (sender_id = auth.uid());

-- RLS for Notifications
CREATE POLICY "Users can view their own notifications"
  ON public.notifications FOR SELECT
  USING (user_id = auth.uid());

CREATE POLICY "Users can update their notifications"
  ON public.notifications FOR UPDATE
  USING (user_id = auth.uid());

-- RLS for Orders
CREATE POLICY "Customers can view their orders"
  ON public.orders FOR SELECT
  USING (customer_id = auth.uid());

CREATE POLICY "Retailers can view their orders"
  ON public.orders FOR SELECT
  USING (retailer_id = auth.uid());

-- ============ TRIGGERS & FUNCTIONS ============

-- Auto-generate order numbers
CREATE OR REPLACE FUNCTION generate_order_number()
RETURNS TRIGGER AS $$
BEGIN
  NEW.order_number = 'ORD-' || TO_CHAR(NOW(), 'YYYYMMDD') || '-' || LPAD(NEXTVAL('order_number_seq')::TEXT, 6, '0');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE SEQUENCE IF NOT EXISTS order_number_seq START 1000;

DROP TRIGGER IF EXISTS set_order_number ON public.orders;
CREATE TRIGGER set_order_number
  BEFORE INSERT ON public.orders
  FOR EACH ROW
  EXECUTE FUNCTION generate_order_number();

-- Update inventory on stock movement
CREATE OR REPLACE FUNCTION update_inventory_on_movement()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.movement_type IN ('sale', 'damaged', 'adjustment') AND NEW.quantity < 0 THEN
    -- Decrease inventory
    UPDATE public.inventory
    SET quantity = quantity + NEW.quantity -- NEW.quantity is negative
    WHERE product_id = NEW.product_id
      AND (variant_id IS NULL OR variant_id = NEW.variant_id)
      AND (warehouse_id IS NULL OR warehouse_id = NEW.warehouse_id);
  ELSIF NEW.movement_type IN ('purchase', 'return', 'adjustment') AND NEW.quantity > 0 THEN
    -- Increase inventory
    UPDATE public.inventory
    SET quantity = quantity + NEW.quantity,
        last_restock_date = NOW()
    WHERE product_id = NEW.product_id
      AND (variant_id IS NULL OR variant_id = NEW.variant_id)
      AND (warehouse_id IS NULL OR warehouse_id = NEW.warehouse_id);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_inventory ON public.stock_movements;
CREATE TRIGGER trigger_update_inventory
  AFTER INSERT ON public.stock_movements
  FOR EACH ROW
  EXECUTE FUNCTION update_inventory_on_movement();

-- Check for low stock and create alerts
CREATE OR REPLACE FUNCTION check_low_stock_alert()
RETURNS TRIGGER AS $$
BEGIN
  -- Check if below threshold
  IF NEW.available_quantity <= NEW.low_stock_threshold THEN
    INSERT INTO public.stock_alerts (
      product_id,
      variant_id,
      warehouse_id,
      alert_type,
      current_quantity,
      threshold
    )
    VALUES (
      NEW.product_id,
      NEW.variant_id,
      NEW.warehouse_id,
      CASE 
        WHEN NEW.available_quantity = 0 THEN 'out_of_stock'
        ELSE 'low_stock'
      END,
      NEW.available_quantity,
      NEW.low_stock_threshold
    )
    ON CONFLICT DO NOTHING;
    
    -- Create notification for retailer
    INSERT INTO public.notifications (
      user_id,
      type,
      title,
      body,
      category,
      priority,
      reference_type,
      reference_id
    )
    SELECT
      p.retailer_id,
      'low_stock',
      'Low Stock Alert',
      'Product "' || p.name || '" is running low. Only ' || NEW.available_quantity || ' units remaining.',
      'inventory',
      CASE WHEN NEW.available_quantity = 0 THEN 'urgent' ELSE 'high' END,
      'product',
      NEW.product_id
    FROM public.products p
    WHERE p.id = NEW.product_id;
  END IF;
  
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_check_low_stock ON public.inventory;
CREATE TRIGGER trigger_check_low_stock
  AFTER UPDATE OF available_quantity ON public.inventory
  FOR EACH ROW
  WHEN (NEW.available_quantity <= NEW.low_stock_threshold)
  EXECUTE FUNCTION check_low_stock_alert();

-- Update conversation on new message
CREATE OR REPLACE FUNCTION update_conversation_on_message()
RETURNS TRIGGER AS $$
BEGIN
  UPDATE public.conversations
  SET 
    last_message_at = NEW.created_at,
    last_message_preview = LEFT(NEW.content, 100),
    updated_at = NEW.created_at
  WHERE id = NEW.conversation_id;
  
  -- Create notification for recipient
  INSERT INTO public.notifications (
    user_id,
    type,
    title,
    body,
    category,
    priority,
    reference_type,
    reference_id
  )
  SELECT
    CASE 
      WHEN c.participant_1 = NEW.sender_id THEN c.participant_2
      ELSE c.participant_1
    END,
    'new_message',
    'New Message',
    LEFT(NEW.content, 100),
    'message',
    'medium',
    'conversation',
    NEW.conversation_id
  FROM public.conversations c
  WHERE c.id = NEW.conversation_id;
  
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_conversation ON public.messages;
CREATE TRIGGER trigger_update_conversation
  AFTER INSERT ON public.messages
  FOR EACH ROW
  WHEN (NEW.message_type = 'text')
  EXECUTE FUNCTION update_conversation_on_message();

-- Notify on order item sale
CREATE OR REPLACE FUNCTION notify_on_sale()
RETURNS TRIGGER AS $$
DECLARE
  v_product_name TEXT;
  v_retailer_id UUID;
BEGIN
  -- Get product details
  SELECT p.name, p.retailer_id
  INTO v_product_name, v_retailer_id
  FROM public.products p
  WHERE p.id = NEW.product_id;
  
  -- Notify retailer of sale
  INSERT INTO public.notifications (
    user_id,
    type,
    title,
    body,
    category,
    priority,
    reference_type,
    reference_id,
    data
  )
  VALUES (
    v_retailer_id,
    'order_placed',
    'New Sale!',
    'You sold ' || NEW.quantity || ' units of "' || v_product_name || '"',
    'order',
    'high',
    'order',
    NEW.order_id,
    jsonb_build_object(
      'product_id', NEW.product_id,
      'quantity', NEW.quantity,
      'total_price', NEW.total_price
    )
  );
  
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_notify_sale ON public.order_items;
CREATE TRIGGER trigger_notify_sale
  AFTER INSERT ON public.order_items
  FOR EACH ROW
  EXECUTE FUNCTION notify_on_sale();

-- Updated_at triggers
CREATE TRIGGER update_products_updated_at
  BEFORE UPDATE ON public.products
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER update_inventory_updated_at
  BEFORE UPDATE ON public.inventory
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER update_conversations_updated_at
  BEFORE UPDATE ON public.conversations
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

-- ============ HELPER FUNCTIONS ============

-- Get unread message count for user
CREATE OR REPLACE FUNCTION get_unread_message_count(p_user_id UUID)
RETURNS INTEGER AS $$
BEGIN
  RETURN (
    SELECT COUNT(*)
    FROM public.messages m
    INNER JOIN public.conversations c ON m.conversation_id = c.id
    WHERE (c.participant_1 = p_user_id OR c.participant_2 = p_user_id)
      AND m.sender_id != p_user_id
      AND m.is_read = false
      AND m.is_deleted = false
  );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Get unread notification count
CREATE OR REPLACE FUNCTION get_unread_notification_count(p_user_id UUID)
RETURNS INTEGER AS $$
BEGIN
  RETURN (
    SELECT COUNT(*)
    FROM public.notifications
    WHERE user_id = p_user_id AND is_read = false
  );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Reserve inventory
CREATE OR REPLACE FUNCTION reserve_inventory(
  p_product_id UUID,
  p_variant_id UUID,
  p_warehouse_id UUID,
  p_quantity INTEGER,
  p_reference_type TEXT,
  p_reference_id UUID
)
RETURNS JSONB AS $$
DECLARE
  v_available INTEGER;
BEGIN
  -- Get available quantity
  SELECT available_quantity INTO v_available
  FROM public.inventory
  WHERE product_id = p_product_id
    AND (variant_id IS NULL OR variant_id = p_variant_id)
    AND (warehouse_id IS NULL OR warehouse_id = p_warehouse_id)
  FOR UPDATE;
  
  IF v_available IS NULL OR v_available < p_quantity THEN
    RETURN jsonb_build_object('success', false, 'error', 'Insufficient stock');
  END IF;
  
  -- Reserve the inventory
  UPDATE public.inventory
  SET reserved_quantity = reserved_quantity + p_quantity
  WHERE product_id = p_product_id
    AND (variant_id IS NULL OR variant_id = p_variant_id)
    AND (warehouse_id IS NULL OR warehouse_id = p_warehouse_id);
  
  RETURN jsonb_build_object('success', true, 'reserved_quantity', p_quantity);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Release inventory reservation
CREATE OR REPLACE FUNCTION release_inventory_reservation(
  p_product_id UUID,
  p_variant_id UUID,
  p_warehouse_id UUID,
  p_quantity INTEGER,
  p_reference_type TEXT,
  p_reference_id UUID
)
RETURNS JSONB AS $$
BEGIN
  UPDATE public.inventory
  SET reserved_quantity = GREATEST(0, reserved_quantity - p_quantity)
  WHERE product_id = p_product_id
    AND (variant_id IS NULL OR variant_id = p_variant_id)
    AND (warehouse_id IS NULL OR warehouse_id = p_warehouse_id);
  
  RETURN jsonb_build_object('success', true);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

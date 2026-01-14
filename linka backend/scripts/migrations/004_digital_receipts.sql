-- Digital Receipt System for E-commerce Platform
-- Automatically generates receipts upon payment completion

-- Create receipts table
CREATE TABLE IF NOT EXISTS receipts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    receipt_number VARCHAR(50) UNIQUE NOT NULL,
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    payment_id UUID NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES user_profiles(id),
    retailer_id UUID NOT NULL REFERENCES user_profiles(id),
    
    -- Receipt details
    subtotal DECIMAL(12, 2) NOT NULL,
    tax_amount DECIMAL(12, 2) DEFAULT 0,
    delivery_fee DECIMAL(12, 2) DEFAULT 0,
    discount_amount DECIMAL(12, 2) DEFAULT 0,
    total_amount DECIMAL(12, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'ZMW',
    
    -- Payment information
    payment_method VARCHAR(50) NOT NULL,
    payment_status VARCHAR(20) DEFAULT 'completed',
    payment_reference VARCHAR(255),
    
    -- Business information
    business_name VARCHAR(255),
    business_address TEXT,
    business_phone VARCHAR(20),
    business_tpin VARCHAR(50), -- Tax Payer Identification Number (Zambia)
    
    -- Customer information
    customer_name VARCHAR(255),
    customer_phone VARCHAR(20),
    customer_email VARCHAR(255),
    delivery_address TEXT,
    
    -- Receipt metadata
    issued_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    pdf_url TEXT, -- Link to generated PDF receipt
    is_emailed BOOLEAN DEFAULT FALSE,
    emailed_at TIMESTAMP WITH TIME ZONE,
    
    -- Zambian tax compliance
    is_tax_invoice BOOLEAN DEFAULT FALSE,
    zra_receipt_number VARCHAR(100), -- Zambia Revenue Authority receipt number
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create receipt line items table
CREATE TABLE IF NOT EXISTS receipt_line_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    receipt_id UUID NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    product_id UUID REFERENCES products(id),
    product_name VARCHAR(255) NOT NULL,
    product_sku VARCHAR(100),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(12, 2) NOT NULL,
    line_total DECIMAL(12, 2) NOT NULL,
    tax_rate DECIMAL(5, 2) DEFAULT 0, -- VAT percentage
    tax_amount DECIMAL(12, 2) DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_receipts_order_id ON receipts(order_id);
CREATE INDEX idx_receipts_payment_id ON receipts(payment_id);
CREATE INDEX idx_receipts_customer_id ON receipts(customer_id);
CREATE INDEX idx_receipts_retailer_id ON receipts(retailer_id);
CREATE INDEX idx_receipts_receipt_number ON receipts(receipt_number);
CREATE INDEX idx_receipts_issued_at ON receipts(issued_at DESC);
CREATE INDEX idx_receipt_line_items_receipt_id ON receipt_line_items(receipt_id);

-- Create function to generate unique receipt number
CREATE OR REPLACE FUNCTION generate_receipt_number()
RETURNS TEXT AS $$
DECLARE
    new_receipt_number TEXT;
    receipt_exists BOOLEAN;
BEGIN
    LOOP
        -- Format: RCP-YYYYMMDD-XXXX (e.g., RCP-20241214-0001)
        new_receipt_number := 'RCP-' || 
            TO_CHAR(NOW(), 'YYYYMMDD') || '-' || 
            LPAD(FLOOR(RANDOM() * 10000)::TEXT, 4, '0');
        
        -- Check if receipt number already exists
        SELECT EXISTS(SELECT 1 FROM receipts WHERE receipt_number = new_receipt_number) INTO receipt_exists;
        
        IF NOT receipt_exists THEN
            EXIT;
        END IF;
    END LOOP;
    
    RETURN new_receipt_number;
END;
$$ LANGUAGE plpgsql;

-- Create trigger function to auto-generate receipt when payment completes
CREATE OR REPLACE FUNCTION create_receipt_on_payment_completion()
RETURNS TRIGGER AS $$
DECLARE
    order_record RECORD;
    retailer_profile RECORD;
    customer_profile RECORD;
    receipt_id UUID;
    order_item RECORD;
BEGIN
    -- Only create receipt if payment just became completed
    IF NEW.status = 'completed' AND (OLD.status IS NULL OR OLD.status != 'completed') THEN
        
        -- Get order details
        SELECT o.*, up.full_name as retailer_name, up.phone as retailer_phone, 
               up.business_name, up.business_address, up.tpin
        INTO order_record
        FROM orders o
        LEFT JOIN user_profiles up ON o.retailer_id = up.id
        WHERE o.id = NEW.order_id;
        
        -- Get customer profile
        SELECT full_name, phone, email 
        INTO customer_profile
        FROM user_profiles
        WHERE id = order_record.customer_id;
        
        -- Create receipt
        INSERT INTO receipts (
            receipt_number,
            order_id,
            payment_id,
            customer_id,
            retailer_id,
            subtotal,
            tax_amount,
            delivery_fee,
            discount_amount,
            total_amount,
            currency,
            payment_method,
            payment_status,
            payment_reference,
            business_name,
            business_address,
            business_phone,
            business_tpin,
            customer_name,
            customer_phone,
            customer_email,
            delivery_address,
            is_tax_invoice
        ) VALUES (
            generate_receipt_number(),
            NEW.order_id,
            NEW.id,
            order_record.customer_id,
            order_record.retailer_id,
            COALESCE(order_record.subtotal, 0),
            COALESCE(order_record.tax_amount, 0),
            COALESCE(order_record.delivery_fee, 0),
            COALESCE(order_record.discount_amount, 0),
            NEW.amount,
            NEW.currency,
            NEW.payment_method,
            NEW.status,
            NEW.id::TEXT,
            order_record.business_name,
            order_record.business_address,
            order_record.retailer_phone,
            order_record.tpin,
            customer_profile.full_name,
            customer_profile.phone,
            customer_profile.email,
            order_record.delivery_address,
            order_record.tpin IS NOT NULL -- Tax invoice if business has TPIN
        )
        RETURNING id INTO receipt_id;
        
        -- Create receipt line items from order items
        INSERT INTO receipt_line_items (
            receipt_id,
            product_id,
            product_name,
            product_sku,
            quantity,
            unit_price,
            line_total,
            tax_rate,
            tax_amount
        )
        SELECT 
            receipt_id,
            oi.product_id,
            p.name,
            p.sku,
            oi.quantity,
            oi.unit_price,
            oi.line_total,
            COALESCE(oi.tax_rate, 0),
            COALESCE(oi.tax_amount, 0)
        FROM order_items oi
        LEFT JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = NEW.order_id;
        
        -- Create notification for customer
        INSERT INTO notifications (
            user_id,
            title,
            body,
            type,
            category,
            reference_type,
            reference_id
        ) VALUES (
            order_record.customer_id,
            'Receipt Generated',
            'Your digital receipt is ready for order ' || order_record.order_number,
            'info',
            'receipt',
            'receipt',
            receipt_id
        );
        
        -- Create notification for retailer/SME
        INSERT INTO notifications (
            user_id,
            title,
            body,
            type,
            category,
            reference_type,
            reference_id
        ) VALUES (
            order_record.retailer_id,
            'Payment Received',
            'Payment of ZMW ' || NEW.amount || ' received for order ' || order_record.order_number,
            'success',
            'payment',
            'payment',
            NEW.id
        );
        
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger to auto-generate receipt
DROP TRIGGER IF EXISTS trigger_create_receipt_on_payment ON payments;
CREATE TRIGGER trigger_create_receipt_on_payment
    AFTER INSERT OR UPDATE ON payments
    FOR EACH ROW
    EXECUTE FUNCTION create_receipt_on_payment_completion();

-- Create function to get receipt with full details
CREATE OR REPLACE FUNCTION get_receipt_details(p_receipt_id UUID)
RETURNS TABLE (
    receipt_id UUID,
    receipt_number VARCHAR,
    order_number VARCHAR,
    issued_at TIMESTAMP WITH TIME ZONE,
    subtotal DECIMAL,
    tax_amount DECIMAL,
    delivery_fee DECIMAL,
    discount_amount DECIMAL,
    total_amount DECIMAL,
    currency VARCHAR,
    payment_method VARCHAR,
    business_name VARCHAR,
    business_address TEXT,
    business_phone VARCHAR,
    business_tpin VARCHAR,
    customer_name VARCHAR,
    customer_phone VARCHAR,
    customer_email VARCHAR,
    delivery_address TEXT,
    is_tax_invoice BOOLEAN,
    line_items JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        r.id,
        r.receipt_number,
        o.order_number,
        r.issued_at,
        r.subtotal,
        r.tax_amount,
        r.delivery_fee,
        r.discount_amount,
        r.total_amount,
        r.currency,
        r.payment_method,
        r.business_name,
        r.business_address,
        r.business_phone,
        r.business_tpin,
        r.customer_name,
        r.customer_phone,
        r.customer_email,
        r.delivery_address,
        r.is_tax_invoice,
        (
            SELECT json_agg(json_build_object(
                'product_name', rli.product_name,
                'product_sku', rli.product_sku,
                'quantity', rli.quantity,
                'unit_price', rli.unit_price,
                'line_total', rli.line_total,
                'tax_rate', rli.tax_rate,
                'tax_amount', rli.tax_amount
            ))
            FROM receipt_line_items rli
            WHERE rli.receipt_id = r.id
        ) as line_items
    FROM receipts r
    LEFT JOIN orders o ON r.order_id = o.id
    WHERE r.id = p_receipt_id;
END;
$$ LANGUAGE plpgsql;

-- Enable RLS
ALTER TABLE receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE receipt_line_items ENABLE ROW LEVEL SECURITY;

-- RLS Policies for receipts
CREATE POLICY "Users can view their own receipts as customers"
    ON receipts FOR SELECT
    USING (auth.uid() = customer_id);

CREATE POLICY "Retailers can view receipts for their sales"
    ON receipts FOR SELECT
    USING (auth.uid() = retailer_id);

CREATE POLICY "Admins can view all receipts"
    ON receipts FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );

-- RLS Policies for receipt line items
CREATE POLICY "Users can view line items for their receipts"
    ON receipt_line_items FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM receipts
            WHERE id = receipt_id 
            AND (customer_id = auth.uid() OR retailer_id = auth.uid())
        )
    );

CREATE POLICY "Admins can view all receipt line items"
    ON receipt_line_items FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );

COMMENT ON TABLE receipts IS 'Digital receipts automatically generated upon payment completion';
COMMENT ON TABLE receipt_line_items IS 'Line items for each receipt';
COMMENT ON FUNCTION generate_receipt_number() IS 'Generates unique receipt numbers in format RCP-YYYYMMDD-XXXX';
COMMENT ON FUNCTION create_receipt_on_payment_completion() IS 'Trigger function to auto-create receipt when payment completes';
COMMENT ON FUNCTION get_receipt_details(UUID) IS 'Gets complete receipt details including line items';

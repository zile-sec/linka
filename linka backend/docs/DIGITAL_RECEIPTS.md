# Digital Receipt System Documentation

## Overview

The Linka platform features an automated digital receipt generation system that creates professional receipts for every completed payment. The system is designed to meet Zambian business and tax requirements, including support for Tax Invoices with TPIN (Tax Payer Identification Number).

## Features

### Automatic Generation
- **Trigger**: Receipts are automatically generated when a payment status changes to `completed`
- **Process**: Database trigger creates receipt, line items, and sends notifications
- **Real-time**: Both customer and SME/retailer receive instant notifications

### Receipt Components

#### Receipt Header
- Unique receipt number (Format: `RCP-YYYYMMDD-XXXX`)
- Issue date and time
- Tax invoice indicator (if business has TPIN)
- Order reference number

#### Business Information
- Business name
- Physical address
- Phone number
- TPIN (for tax compliance)

#### Customer Information
- Full name
- Phone number
- Email address
- Delivery address (if applicable)

#### Line Items
- Product name and SKU
- Quantity
- Unit price
- Line total
- Tax rate and amount (VAT)

#### Totals
- Subtotal
- Tax amount (VAT)
- Delivery fee
- Discount amount
- **Grand Total**
- Payment method used

## API Endpoints

### Get Receipt by ID
```http
GET /api/receipts/{receipt_id}
Authorization: Bearer {token}
```

**Response:**
```json
{
  "receipt": {
    "receipt_id": "uuid",
    "receipt_number": "RCP-20241214-0001",
    "order_number": "ORD-20241214-0001",
    "issued_at": "2024-12-14T10:30:00Z",
    "subtotal": 250.00,
    "tax_amount": 40.00,
    "delivery_fee": 25.00,
    "discount_amount": 0.00,
    "total_amount": 315.00,
    "currency": "ZMW",
    "payment_method": "mobile_money",
    "business_name": "Tech Solutions Ltd",
    "business_address": "123 Cairo Road, Lusaka",
    "business_phone": "+260977123456",
    "business_tpin": "1234567890",
    "customer_name": "John Doe",
    "customer_phone": "+260966123456",
    "customer_email": "john@example.com",
    "delivery_address": "456 Independence Ave, Lusaka",
    "is_tax_invoice": true,
    "line_items": [
      {
        "product_name": "Wireless Mouse",
        "product_sku": "WM-001",
        "quantity": 2,
        "unit_price": 75.00,
        "line_total": 150.00,
        "tax_rate": 16.00,
        "tax_amount": 24.00
      }
    ]
  },
  "download_url": "/receipts/{receipt_id}/pdf"
}
```

### Get Receipt by Order ID
```http
GET /api/receipts/order/{order_id}
Authorization: Bearer {token}
```

Retrieves the receipt associated with a specific order.

### List User Receipts
```http
GET /api/receipts?limit=20&offset=0
Authorization: Bearer {token}
```

**Query Parameters:**
- `limit`: Number of receipts to return (default: 20)
- `offset`: Pagination offset (default: 0)
- `start_date`: Filter receipts from date (ISO format)
- `end_date`: Filter receipts to date (ISO format)

### Download Receipt PDF
```http
GET /api/receipts/{receipt_id}/pdf
Authorization: Bearer {token}
```

Downloads the receipt as a printable HTML (can be converted to PDF by browser).

### Email Receipt
```http
POST /api/receipts/{receipt_id}/email
Authorization: Bearer {token}
```

Sends the receipt to the customer's email address. Only accessible by the retailer or admin.

## Database Schema

### receipts Table
```sql
CREATE TABLE receipts (
    id UUID PRIMARY KEY,
    receipt_number VARCHAR(50) UNIQUE NOT NULL,
    order_id UUID REFERENCES orders(id),
    payment_id UUID REFERENCES payments(id),
    customer_id UUID REFERENCES user_profiles(id),
    retailer_id UUID REFERENCES user_profiles(id),
    
    -- Financial details
    subtotal DECIMAL(12, 2),
    tax_amount DECIMAL(12, 2),
    delivery_fee DECIMAL(12, 2),
    discount_amount DECIMAL(12, 2),
    total_amount DECIMAL(12, 2),
    currency VARCHAR(3),
    
    -- Payment info
    payment_method VARCHAR(50),
    payment_status VARCHAR(20),
    payment_reference VARCHAR(255),
    
    -- Business/Customer details
    business_name VARCHAR(255),
    business_address TEXT,
    business_phone VARCHAR(20),
    business_tpin VARCHAR(50),
    customer_name VARCHAR(255),
    customer_phone VARCHAR(20),
    customer_email VARCHAR(255),
    delivery_address TEXT,
    
    -- Metadata
    issued_at TIMESTAMP WITH TIME ZONE,
    pdf_url TEXT,
    is_emailed BOOLEAN,
    emailed_at TIMESTAMP WITH TIME ZONE,
    is_tax_invoice BOOLEAN,
    zra_receipt_number VARCHAR(100),
    
    created_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE
);
```

### receipt_line_items Table
```sql
CREATE TABLE receipt_line_items (
    id UUID PRIMARY KEY,
    receipt_id UUID REFERENCES receipts(id),
    product_id UUID REFERENCES products(id),
    product_name VARCHAR(255),
    product_sku VARCHAR(100),
    quantity INTEGER,
    unit_price DECIMAL(12, 2),
    line_total DECIMAL(12, 2),
    tax_rate DECIMAL(5, 2),
    tax_amount DECIMAL(12, 2),
    created_at TIMESTAMP WITH TIME ZONE
);
```

## Automatic Triggers

### Receipt Generation Trigger
```sql
CREATE TRIGGER trigger_create_receipt_on_payment
    AFTER INSERT OR UPDATE ON payments
    FOR EACH ROW
    EXECUTE FUNCTION create_receipt_on_payment_completion();
```

**What it does:**
1. Monitors payment status changes
2. When payment becomes `completed`, creates receipt
3. Copies order line items to receipt line items
4. Generates unique receipt number
5. Sends notifications to customer and retailer

### Notifications Sent

#### To Customer:
```json
{
  "title": "Receipt Generated",
  "body": "Your digital receipt is ready for order ORD-20241214-0001",
  "type": "info",
  "category": "receipt",
  "reference_type": "receipt",
  "reference_id": "receipt_uuid"
}
```

#### To Retailer/SME:
```json
{
  "title": "Payment Received",
  "body": "Payment of ZMW 315.00 received for order ORD-20241214-0001",
  "type": "success",
  "category": "payment",
  "reference_type": "payment",
  "reference_id": "payment_uuid"
}
```

## Security & Access Control

### Row Level Security (RLS)

**Customers** can view:
- Their own receipts (as buyers)

**Retailers/SMEs** can view:
- Receipts for orders they sold

**Admins** can view:
- All receipts

### RLS Policies
```sql
-- Customer access
CREATE POLICY "Users can view their own receipts as customers"
    ON receipts FOR SELECT
    USING (auth.uid() = customer_id);

-- Retailer access
CREATE POLICY "Retailers can view receipts for their sales"
    ON receipts FOR SELECT
    USING (auth.uid() = retailer_id);

-- Admin access
CREATE POLICY "Admins can view all receipts"
    ON receipts FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );
```

## Zambian Tax Compliance

### Tax Invoice Requirements

For businesses registered with ZRA (Zambia Revenue Authority), the system automatically marks receipts as tax invoices when:
- Business has a valid TPIN
- Receipt includes VAT calculations
- All required business information is present

### Tax Invoice Fields:
- **TPIN**: Tax Payer Identification Number
- **VAT Rate**: Currently 16% in Zambia
- **Tax Amount**: Calculated per line item and totaled
- **ZRA Receipt Number**: Optional field for integration

### Compliance Features:
- Unique receipt numbering
- Detailed line items with VAT breakdown
- Business registration details
- Customer information
- Date and time stamps
- Payment method tracking

## Integration with Other Services

### Payment Service
- Triggers receipt generation on payment completion
- Links receipt to payment transaction
- Handles refund scenarios

### Order Service
- Provides order line items
- Links orders to receipts
- Updates order status

### Notification Service
- Sends receipt ready notifications
- Email delivery of receipts
- In-app notifications

### Inventory Service
- Product details for line items
- SKU information
- Pricing validation

## Usage Examples

### For Customers

#### View My Receipts
```bash
curl -X GET "http://localhost:8080/api/receipts" \
  -H "Authorization: Bearer {customer_token}"
```

#### Download Receipt
```bash
curl -X GET "http://localhost:8080/api/receipts/{receipt_id}/pdf" \
  -H "Authorization: Bearer {customer_token}" \
  -o receipt.html
```

### For Retailers/SMEs

#### View Sales Receipts
```bash
curl -X GET "http://localhost:8080/api/receipts?limit=50" \
  -H "Authorization: Bearer {retailer_token}"
```

#### Email Receipt to Customer
```bash
curl -X POST "http://localhost:8080/api/receipts/{receipt_id}/email" \
  -H "Authorization: Bearer {retailer_token}"
```

### For Admins

#### View All Receipts
```bash
curl -X GET "http://localhost:8080/api/receipts?start_date=2024-12-01&end_date=2024-12-31" \
  -H "Authorization: Bearer {admin_token}"
```

## Receipt Number Format

Format: `RCP-YYYYMMDD-XXXX`

- **RCP**: Receipt prefix
- **YYYYMMDD**: Date of issue
- **XXXX**: Random 4-digit number (0000-9999)

Example: `RCP-20241214-0001`

## Future Enhancements

### Planned Features:
1. **PDF Generation**: Native PDF output using ReportLab
2. **QR Code**: Add QR code for verification
3. **Multiple Currencies**: Support for USD, EUR alongside ZMW
4. **Receipt Templates**: Customizable receipt layouts for businesses
5. **Batch Export**: Export multiple receipts as ZIP
6. **ZRA Integration**: Direct submission to Zambia Revenue Authority
7. **Analytics**: Receipt analytics and reporting
8. **Digital Signatures**: Cryptographic signing for authenticity

## Troubleshooting

### Receipt Not Generated
**Symptom**: Payment completed but no receipt created

**Solutions:**
1. Check payment status is exactly `completed`
2. Verify trigger is enabled: `SELECT * FROM pg_trigger WHERE tgname = 'trigger_create_receipt_on_payment';`
3. Check order exists and has line items
4. Review database logs for errors

### Cannot Access Receipt
**Symptom**: 403 Forbidden when accessing receipt

**Solutions:**
1. Verify you are customer or retailer for that order
2. Check authentication token is valid
3. Ensure RLS policies are enabled
4. Verify user_id matches customer_id or retailer_id

### Receipt Missing Data
**Symptom**: Receipt created but fields are empty

**Solutions:**
1. Ensure order has all required fields
2. Verify user profiles have business information
3. Check product data is complete
4. Review order_items table for line item data

## Support

For issues or questions about the digital receipt system:
- Review the API documentation
- Check the database migration files
- Contact the development team
- Submit issues on the project repository
```

Now create a comprehensive API integration guide:

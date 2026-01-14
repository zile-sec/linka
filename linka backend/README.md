# Linka Backend - Zambian SME E-commerce Platform

A microservices-based backend platform for Zambian SMEs, featuring comprehensive Supabase integration, multi-factor authentication, real-time analytics, and Bank of Zambia (BoZ) compliance.

## Architecture Overview

The Linka backend consists of 11 microservices orchestrated via Docker Compose:

### Core Services
- **API Gateway** (`:8080`) - Entry point with rate limiting, auth, and compliance checks
- **User Service** (`:8000`) - Authentication, user profiles, MFA, session management
- **Product Service** (`:8002`) - Product catalog, categories, variants
- **Order Service** (`:8001`) - Order processing, lifecycle management
- **Payment Service** (`:8003`) - Payment processing, wallet integration
- **Wallet Service** (`:8004`) - Digital wallets, KYC verification, BoZ compliance
- **Inventory Service** (`:8005`) - Stock management, warehouse operations
- **Delivery Service** (`:8006`) - Driver management, real-time tracking
- **Notification Service** (`:8007`) - Multi-channel notifications (SMS, email, push)
- **Subscription Service** (`:8008`) - Subscription plans, billing cycles

### Infrastructure
- **Redis** (`:6380`) - Caching and session storage
- **RabbitMQ** (`:5673`, Management: `:15673`) - Message queue for async operations

## Prerequisites

1. **Docker & Docker Compose**
   - Docker version 20.10 or higher
   - Docker Compose version 2.0 or higher

2. **Supabase Project**
   - Create a project at [supabase.com](https://supabase.com)
   - Note your project URL and anon key

3. **Environment Variables**
   - Copy `.env.example` to `.env`
   - Update with your Supabase credentials

## Quick Start

### 1. Clone and Setup

```bash
cd "linka backend"
cp .env.example .env
```

### 2. Configure Supabase

Edit `.env` and add your Supabase credentials:

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-anon-key-here
```

### 3. Run Database Migrations

Run the SQL migrations in your Supabase SQL Editor in order:

```bash
# Navigate to your Supabase project > SQL Editor
# Run each migration file in order:
scripts/migrations/001_create_user_profiles.sql
```

Or use the v0 interface to run the scripts directly if available.

### 4. Start Services

```bash
docker-compose up --build
```

Wait for all health checks to pass (this may take 2-3 minutes).

### 5. Run Tests

```bash
cd gateway
chmod +x curl_tests.sh
./curl_tests.sh
```

## Service Endpoints

### API Gateway (Port 8080)
- `GET /health` - Gateway health check
- `GET /ready` - Readiness probe
- `POST /auth/signup` - User registration
- `POST /auth/login` - User authentication
- `GET /user/profile` - Get user profile (authenticated)
- `PUT /user/profile` - Update user profile (authenticated)
- `/{service}/{path}` - Proxy to microservices (authenticated)

### User Service (Port 8000)
- `POST /signup` - Register new user
- `POST /login` - Login and get token
- `GET /profile` - Get current user profile
- `PUT /profile` - Update user profile
- `GET /health` - Service health
- `GET /ready` - Service readiness

## Authentication Flow

### 1. Sign Up
```bash
curl -X POST http://localhost:8080/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "SecurePass123!",
    "role": "customer",
    "full_name": "John Doe",
    "phone": "+260971234567"
  }'
```

**Response:**
```json
{
  "message": "User created successfully. Please check your email to confirm your account.",
  "user_id": "uuid",
  "email": "user@example.com",
  "role": "customer"
}
```

### 2. Login
```bash
curl -X POST http://localhost:8080/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "SecurePass123!"
  }'
```

**Response:**
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "user": {
    "id": "uuid",
    "email": "user@example.com",
    "role": "customer",
    "full_name": "John Doe",
    "phone": "+260971234567",
    "kyc_status": "unverified",
    "kyc_level": 0,
    "created_at": "2024-01-01T00:00:00Z"
  }
}
```

### 3. Get Profile
```bash
curl -X GET http://localhost:8080/user/profile \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Database Schema

### user_profiles
```sql
- id (UUID, PK, FK to auth.users)
- email (TEXT, NOT NULL)
- role (TEXT, CHECK: customer|retailer|driver|admin|support)
- phone (TEXT)
- full_name (TEXT)
- avatar_url (TEXT)
- kyc_status (TEXT, DEFAULT: unverified)
- kyc_level (INTEGER, DEFAULT: 0)
- is_active (BOOLEAN, DEFAULT: true)
- last_login_at (TIMESTAMPTZ)
- created_at (TIMESTAMPTZ)
- updated_at (TIMESTAMPTZ)
```

**Triggers:**
- Auto-creates profile on user signup
- Updates `last_login_at` on session creation
- Updates `updated_at` on profile changes

**RLS Policies:**
- Users can view/update their own profile
- Admins can view all profiles

## Development

### Running Individual Services

```bash
# User Service only
docker-compose up user-service redis rabbitmq

# With API Gateway
docker-compose up api-gateway user-service redis rabbitmq
```

### Viewing Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f user-service

# Gateway only
docker-compose logs -f api-gateway
```

### Rebuilding After Changes

```bash
# Rebuild specific service
docker-compose up --build user-service

# Rebuild all
docker-compose up --build
```

## Testing

### Automated Tests
```bash
cd gateway
./curl_tests.sh
```

### Manual Testing
```bash
# Health check
curl http://localhost:8080/health

# Signup
curl -X POST http://localhost:8080/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"Test123!","role":"customer"}'

# Login
curl -X POST http://localhost:8080/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"Test123!"}'
```

## Troubleshooting

### Issue: "Service not ready"
**Solution:** Wait for all health checks to pass. Check logs with `docker-compose logs -f`

### Issue: "Invalid or expired token"
**Solution:** 
1. Verify `SUPABASE_URL` and `SUPABASE_KEY` in `.env`
2. Check if email confirmation is required in Supabase settings
3. Confirm user exists in Supabase auth.users table

### Issue: "User profile not found"
**Solution:**
1. Run the database migration `001_create_user_profiles.sql`
2. Check if the trigger `on_auth_user_created` exists
3. Verify RLS policies are enabled

### Issue: Services fail to start
**Solution:**
1. Check if ports are already in use
2. Verify Docker has enough resources
3. Check logs for specific error messages

## Security Features

- **JWT-based authentication** via Supabase
- **Row Level Security (RLS)** on all user data
- **Rate limiting** on API endpoints
- **MFA support** (TOTP)
- **KYC verification** via Smile ID
- **BoZ compliance** checks on financial operations
- **Audit logging** for sensitive operations

## Bank of Zambia Compliance

The platform implements BoZ regulatory requirements:

- Transaction limits enforcement
- KYC verification levels
- Compliance headers on wallet operations
- Audit trail for financial transactions
- Real-time monitoring and reporting

## License

Proprietary - Linka Platform

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review logs: `docker-compose logs -f`
3. Verify environment variables in `.env`
4. Ensure Supabase project is properly configured

## Contributing

1. Create feature branch
2. Make changes
3. Test with `./gateway/curl_tests.sh`
4. Submit pull request

## Roadmap

- [ ] Statistics and Business Analysis services
- [ ] Stats Presentation service with dashboards
- [ ] Advanced analytics and reporting
- [ ] Integration with Zambian payment providers
- [ ] Mobile app API extensions
- [ ] Real-time notifications via WebSocket

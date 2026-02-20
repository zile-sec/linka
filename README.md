# Linka Platform

A Zambian SME E-Commerce Marketplace with a microservices backend architecture.

## Project Structure

This repository contains:

- **Backend Services** (`linka backend/`) - Python microservices architecture with FastAPI
- **Minimal Next.js Wrapper** (`app/`, `package.json`) - Lightweight deployment wrapper (optional for local dev)

## Backend-First Development Philosophy

This project is **backend-focused**. The Next.js setup at the root is a minimal wrapper that exists solely for deployment compatibility. **You do not need to touch the frontend or run Next.js for local backend development**.

### Local Development (Backend Only)

To work on the backend services locally without any frontend complexity:

```bash
cd "linka backend"

# Set up environment variables
cp .env.example .env
# Edit .env with your Supabase credentials

# Start all backend services with Docker Compose
docker-compose up

# Your services will be available at:
# - API Gateway: http://localhost:8000
# - User Service: http://localhost:8001
# - Order Service: http://localhost:8002
# - Product Service: http://localhost:8003
# - Inventory Service: http://localhost:8004
# - Delivery Service: http://localhost:8005
# - Notification Service: http://localhost:8006
# - Payment Service: http://localhost:8007
# - Wallet Service: http://localhost:8008
# - Subscription Service: http://localhost:8009
# - Statistics Service: http://localhost:8010
# - Stats Presentation Service: http://localhost:8011
```

See the [Backend README](./linka%20backend/README.md) for detailed documentation.

### Testing Backend Services

```bash
cd "linka backend/gateway"
chmod +x curl_tests.sh
./curl_tests.sh
```

## Deployment

### Backend Deployment (Recommended)

Deploy backend services to any Docker-compatible platform:
- AWS ECS/EKS
- Google Cloud Run
- Azure Container Apps
- DigitalOcean App Platform
- Railway
- Render

### Vercel Deployment (Frontend Wrapper Only)

The minimal Next.js app at the root can be deployed to Vercel for documentation or landing page purposes. **It does not include or deploy the backend services**.

```bash
vercel deploy
```

The backend services must be deployed separately to a container platform.

## Why This Structure?

This structure allows you to:

1. **Develop backend services independently** without frontend dependencies
2. **Deploy backend to production** using Docker Compose or Kubernetes
3. **Optionally deploy a frontend** to Vercel if needed in the future
4. **Avoid Next.js complexity** when working purely on backend features

## Key Technologies

**Backend:**
- Python 3.11+ with FastAPI
- Supabase (PostgreSQL + Auth + Realtime)
- Docker & Docker Compose
- Redis for caching
- RabbitMQ for message queuing

**Frontend Wrapper (Optional):**
- Next.js 16 (minimal setup)
- React 19
- Tailwind CSS 4

## Getting Started

### Prerequisites

- Docker & Docker Compose
- Python 3.11+
- Supabase account (free tier works)

### Quick Start

1. Clone the repository
2. Navigate to backend: `cd "linka backend"`
3. Configure environment: `cp .env.example .env`
4. Start services: `docker-compose up`
5. Run migrations in Supabase SQL Editor
6. Test with curl: `./gateway/curl_tests.sh`

**No frontend setup required for backend development.**

## Documentation

- [Backend Architecture](./linka%20backend/README.md)
- [Digital Receipts System](./linka%20backend/docs/DIGITAL_RECEIPTS.md)
- [Inventory & Notifications](./linka%20backend/docs/INVENTORY_NOTIFICATION_INTEGRATION.md)
- [Docker Compose Guide](./linka%20backend/DOCKER_COMPOSE_GUIDE.md)

## Contributing

When contributing to this project:

- **Backend changes** go in `linka backend/`
- **Frontend changes** (if needed) go in `app/`
- The minimal Next.js setup should remain minimal
- Focus on backend architecture and API design

## License

See [LICENSE](./LICENSE) file for details.

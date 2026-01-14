# Docker Compose Testing Guide

## Overview

The improved `docker-compose.yml` is optimized for local development and testing with:
- Health checks on all services
- Proper networking with `linka-network`
- Environment variable configuration via `.env`
- Persistent volumes for databases
- Proper service dependencies
- Debug logging enabled

## Quick Start

### 1. **Start All Services**
```bash
docker compose up -d
```

This will:
- Start PostgreSQL, Redis, and RabbitMQ infrastructure
- Build and start all 9 microservices
- Start the API Gateway
- Wait for health checks to pass

### 2. **Check Service Status**
```bash
# See all running containers
docker compose ps

# Check if all services are healthy
docker compose ps --format "table {{.Names}}\t{{.Status}}"
```

### 3. **View Logs**
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f user-service

# Last 100 lines
docker compose logs --tail 100 api-gateway
```

### 4. **Stop All Services**
```bash
docker compose down
```

### 5. **Remove Everything (with volumes)**
```bash
docker compose down -v
```

## Service Ports

| Service | Port | Health Check |
|---------|------|--------------|
| API Gateway | 8080 | `http://localhost:8080/health` |
| User Service | 8000 | `http://localhost:8000/health` |
| Order Service | 8001 | `http://localhost:8001/health` |
| Product Service | 8002 | `http://localhost:8002/health` |
| Payment Service | 8003 | `http://localhost:8003/health` |
| Wallet Service | 8004 | `http://localhost:8004/health` |
| Inventory Service | 8005 | `http://localhost:8005/health` |
| Delivery Service | 8006 | `http://localhost:8006/health` |
| Notification Service | 8007 | `http://localhost:8007/health` |
| Subscription Service | 8008 | `http://localhost:8008/health` |
| PostgreSQL | 5432 | Internal only |
| Redis | 6379 | Internal only |
| RabbitMQ | 5672 | Internal only |
| RabbitMQ UI | 15672 | `http://localhost:15672` |

## Testing the Services

### 1. **Test User Service Registration**
```bash
curl -X POST http://localhost:8000/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "SecurePass123!",
    "role": "customer"
  }'
```

### 2. **Test API Gateway**
```bash
# Check gateway health
curl http://localhost:8080/health

# Check gateway readiness
curl http://localhost:8080/ready
```

### 3. **Test Service Health Checks**
```bash
# Test all services
for port in 8000 8001 8002 8003 8004 8005 8006 8007 8008 8080; do
  echo "Port $port:"
  curl -s http://localhost:$port/health | jq .
done
```

### 4. **Access RabbitMQ UI**
```
http://localhost:15672
Username: guest
Password: guest
```

### 5. **Connect to PostgreSQL**
```bash
psql postgresql://zile:localpass@localhost:5432/linka
```

## Environment Configuration

### Using `.env` File

The `.env` file controls:
- Database credentials and port
- Redis configuration
- RabbitMQ credentials and ports
- Service URLs
- Log level and environment

### Modify Environment Variables

To change values, edit `.env`:
```bash
DB_PASSWORD=your_new_password
LOG_LEVEL=INFO
```

Then restart services:
```bash
docker compose restart
```

## Troubleshooting

### Services Won't Start

**Check logs:**
```bash
docker compose logs -f service-name
```

**Common issues:**
- Port already in use: `sudo lsof -i :8000`
- Service dependencies not ready: Wait 30-60 seconds for health checks
- Docker build errors: `docker compose build --no-cache`

### Service Health Checks Failing

**View health endpoint:**
```bash
curl http://localhost:8000/ready
```

**Check service logs:**
```bash
docker compose logs user-service | tail -50
```

### Database Connection Issues

**Check PostgreSQL status:**
```bash
docker compose exec postgres pg_isready
```

**View PostgreSQL logs:**
```bash
docker compose logs postgres
```

### Reset Database

```bash
docker compose down -v
docker compose up postgres -d
docker compose exec postgres createdb -U zile linka
```

## Development Workflow

### 1. **Hot Reload**

All services have `--reload` enabled. Changes to code are automatically reloaded:
```bash
# Edit a file
vim services/user-service/app/main.py

# Changes take effect immediately (see logs)
docker compose logs -f user-service
```

### 2. **Run Tests Inside Container**

```bash
# Run user-service tests
docker compose exec user-service pytest tests/

# Run with coverage
docker compose exec user-service pytest --cov=app tests/
```

### 3. **Debug a Service**

Add breakpoint to code and attach debugger:
```bash
# View live logs
docker compose logs -f user-service

# Or use Docker exec to run Python directly
docker compose exec user-service python -m pdb app/main.py
```

## Advanced Commands

### Scale Services (experimental)

```bash
# Scale order-service to 3 replicas
docker compose up -d --scale order-service=3
```

Note: Ports will conflict. Use proper orchestration (Kubernetes) for production scaling.

### View Network

```bash
# List networks
docker network ls | grep linka

# Inspect network
docker network inspect linka-network
```

### Build Without Starting

```bash
docker compose build
```

### Rebuild Service (no cache)

```bash
docker compose build --no-cache user-service
```

## Performance Monitoring

### View Container Resource Usage

```bash
docker stats
```

### Top Processes in Container

```bash
docker compose top user-service
```

## Database Migrations (When Implemented)

```bash
# Apply migrations
docker compose exec api-gateway alembic upgrade head

# Revert migrations
docker compose exec api-gateway alembic downgrade -1
```

## Health Check Details

Each service has health checks configured:

**Liveness Probe** (`/health`):
- Returns `{"status": "alive", "service": "user-service"}`
- Always returns 200 if container is running
- Used by orchestrators to restart unhealthy containers

**Readiness Probe** (`/ready`):
- Returns `{"status": "ready"}` (200) or `{"status": "not ready"}` (503)
- Checks service dependencies
- Used by load balancers to route traffic

**Docker Health Check Retry Policy:**
- Check interval: 10 seconds
- Timeout: 5 seconds
- Retries: 5 before marking unhealthy

## Next Steps

1. Copy `.env.example` to `.env` and customize values
2. Run `docker compose up -d` to start all services
3. Wait for health checks to pass (~30 seconds)
4. Test endpoints using the commands above
5. Run tests: `docker compose exec user-service pytest`
6. Check logs for any errors: `docker compose logs -f`

---

For more info, see [TESTING.md](TESTING.md) and [QUICK_REFERENCE.md](QUICK_REFERENCE.md)

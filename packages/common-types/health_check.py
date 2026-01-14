"""
Shared health check utilities for microservices
"""
from fastapi import FastAPI, HTTPException
import requests
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class HealthCheckRegistry:
    """Registry for custom health check functions"""
    
    def __init__(self):
        self.checks: Dict[str, callable] = {}
    
    def register(self, name: str, check_func: callable):
        """Register a health check function"""
        self.checks[name] = check_func
    
    async def run_all(self) -> Dict[str, Any]:
        """Run all registered checks"""
        results = {}
        for name, check_func in self.checks.items():
            try:
                if hasattr(check_func, '__call__'):
                    result = await check_func() if hasattr(check_func, '__await__') else check_func()
                    results[name] = {"status": "healthy", "result": result}
                else:
                    results[name] = {"status": "healthy"}
            except Exception as e:
                logger.error(f"Health check '{name}' failed: {str(e)}")
                results[name] = {"status": "unhealthy", "error": str(e)}
        return results


def setup_health_checks(app: FastAPI, service_name: str, registry: Optional[HealthCheckRegistry] = None):
    """
    Add standard health check endpoints to FastAPI app
    
    Args:
        app: FastAPI application instance
        service_name: Name of the microservice
        registry: Optional HealthCheckRegistry for custom checks
    """
    
    @app.get("/health")
    async def health():
        """Liveness probe - basic service health"""
        logger.debug("Health check requested")
        return {"status": "alive", "service": service_name}
    
    @app.get("/ready")
    async def readiness():
        """Readiness probe - verify service dependencies"""
        try:
            logger.debug("Readiness check - verifying dependencies")
            
            if registry:
                checks = await registry.run_all()
                # If any check is unhealthy, service is not ready
                unhealthy = [c for c in checks.values() if c["status"] == "unhealthy"]
                if unhealthy:
                    logger.warning(f"Readiness check failed: {len(unhealthy)} checks unhealthy")
                    return {
                        "status": "not ready",
                        "service": service_name,
                        "checks": checks
                    }, 503
            
            logger.debug("Readiness check passed")
            return {"status": "ready", "service": service_name}
        except Exception as e:
            logger.error(f"Readiness check failed: {str(e)}")
            return {"status": "not ready", "detail": str(e)}, 503


def create_dependency_check(url: str, name: str, timeout: int = 5) -> callable:
    """
    Create a health check function for an external dependency
    
    Args:
        url: URL to check (e.g., "http://supabase:5432")
        name: Name of the dependency
        timeout: Timeout in seconds
    
    Returns:
        Async function that checks the dependency
    """
    async def check():
        try:
            response = requests.get(f"{url}/health", timeout=timeout)
            if response.status_code >= 500:
                raise Exception(f"{name} returned status {response.status_code}")
            return f"{name} is healthy"
        except Exception as e:
            raise Exception(f"{name} check failed: {str(e)}")
    
    return check

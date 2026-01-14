import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from main import app


@pytest.fixture
def client():
    """Create a test client for the FastAPI app"""
    return TestClient(app)


@pytest.fixture
def mock_supabase_env(monkeypatch):
    """Mock Supabase environment variables"""
    monkeypatch.setenv("SUPABASE_URL", "https://test-supabase.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-api-key")


@pytest.fixture
def mock_supabase_auth():
    """Mock Supabase auth response"""
    return {
        "access_token": "test-jwt-token",
        "refresh_token": "test-refresh-token",
        "user": {
            "id": "test-user-id-123",
            "email": "test@example.com",
            "user_metadata": {
                "role": "customer"
            }
        }
    }


@pytest.fixture
def mock_requests(monkeypatch):
    """Mock requests library for HTTP calls"""
    mock = MagicMock()
    monkeypatch.setattr("requests.request", mock)
    monkeypatch.setattr("requests.get", mock)
    monkeypatch.setattr("requests.post", mock)
    return mock

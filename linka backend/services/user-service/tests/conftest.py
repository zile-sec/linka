"""
Shared fixtures for user-service tests.

Every test runs against a TestClient and uses mocked Supabase calls
so no network or real database is required.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Ensure packages + app dirs are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages"))

# Set required env before anything imports the supabase client
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-anon-key")


# ---------------------------------------------------------------------------
# Helpers to build mock Supabase responses
# ---------------------------------------------------------------------------

def make_mock_user(user_id: str = "user-100", email: str = "test@example.com"):
    user = MagicMock()
    user.id = user_id
    user.email = email
    return user


def make_mock_session(
    access_token: str = "access-tok",
    refresh_token: str = "refresh-tok",
    expires_in: int = 3600,
):
    session = MagicMock()
    session.access_token = access_token
    session.refresh_token = refresh_token
    session.expires_in = expires_in
    return session


SAMPLE_PROFILE = {
    "id": "user-100",
    "email": "test@example.com",
    "role": "customer",
    "full_name": "Test User",
    "phone": "+260971234567",
    "avatar_url": None,
    "kyc_status": "unverified",
    "kyc_level": 0,
    "is_active": True,
    "last_login_at": None,
    "created_at": "2025-01-01T00:00:00+00:00",
    "updated_at": "2025-01-01T00:00:00+00:00",
}

SME_PROFILE = {
    **SAMPLE_PROFILE,
    "id": "sme-200",
    "email": "sme@example.com",
    "role": "retailer",
    "full_name": "SME Owner",
}

DRIVER_PROFILE = {
    **SAMPLE_PROFILE,
    "id": "driver-300",
    "email": "driver@example.com",
    "role": "driver",
    "full_name": "Delivery Maker",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_sb():
    """Return a mock SupabaseClient and patch get_supabase_client."""
    sb = MagicMock()
    sb.test_connection.return_value = True
    with patch("app.main.get_supabase_client", return_value=sb):
        yield sb


@pytest.fixture()
def mock_auth_user():
    """Patch get_current_user to return a default customer AuthenticatedUser."""
    from shared.auth_middleware import AuthenticatedUser, UserRole

    user = AuthenticatedUser(
        id="user-100",
        email="test@example.com",
        role=UserRole.CUSTOMER,
    )
    with patch("app.main.get_current_user", return_value=user):
        yield user


@pytest.fixture()
def mock_admin_user():
    """Patch get_current_user to return an admin AuthenticatedUser."""
    from shared.auth_middleware import AuthenticatedUser, UserRole

    user = AuthenticatedUser(
        id="admin-001",
        email="admin@linka.co.zm",
        role=UserRole.ADMIN,
    )
    with patch("app.main.get_current_user", return_value=user):
        yield user


@pytest.fixture()
def client():
    """Fresh TestClient (imports are deferred so env is set first)."""
    from app.main import app
    return TestClient(app)

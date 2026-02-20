"""
Unit tests for the Linka user-service.

All Supabase calls are mocked so these run offline.
Run with:  pytest -v  (from the user-service directory)
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from conftest import (
    make_mock_user,
    make_mock_session,
    SAMPLE_PROFILE,
    SME_PROFILE,
    DRIVER_PROFILE,
)


# ===================================================================
#  Health checks
# ===================================================================

class TestHealthChecks:

    def test_health_returns_alive(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "alive"
        assert body["service"] == "user-service"

    def test_ready_when_supabase_up(self, client, mock_sb):
        mock_sb.test_connection.return_value = True
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_ready_when_supabase_down(self, client, mock_sb):
        mock_sb.test_connection.return_value = False
        resp = client.get("/ready")
        assert resp.status_code == 503


# ===================================================================
#  Signup
# ===================================================================

class TestSignup:

    def test_customer_signup_success(self, client, mock_sb):
        mock_sb.client.auth.sign_up.return_value = MagicMock(
            user=make_mock_user("cust-1", "cust@test.com")
        )
        mock_sb.get_single.return_value = SAMPLE_PROFILE
        mock_sb.update.return_value = SAMPLE_PROFILE

        resp = client.post("/signup", json={
            "email": "cust@test.com",
            "password": "Secure1pass",
            "role": "customer",
            "full_name": "Jane Doe",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "customer"
        assert body["email"] == "cust@test.com"
        assert "user_id" in body

    def test_sme_signup_success(self, client, mock_sb):
        mock_sb.client.auth.sign_up.return_value = MagicMock(
            user=make_mock_user("sme-1", "sme@test.com")
        )
        mock_sb.get_single.return_value = SME_PROFILE
        mock_sb.update.return_value = SME_PROFILE

        resp = client.post("/signup", json={
            "email": "sme@test.com",
            "password": "Secure1pass",
            "role": "retailer",
            "full_name": "SME Boss",
            "business_name": "Lusaka Goods Ltd",
            "business_type": "food_delivery",
        })
        assert resp.status_code == 200
        assert resp.json()["role"] == "retailer"

    def test_sme_signup_requires_business_name(self, client, mock_sb):
        resp = client.post("/signup", json={
            "email": "sme@test.com",
            "password": "Secure1pass",
            "role": "retailer",
        })
        assert resp.status_code == 400
        assert "business_name" in resp.json()["detail"].lower()

    def test_driver_signup_success(self, client, mock_sb):
        mock_sb.client.auth.sign_up.return_value = MagicMock(
            user=make_mock_user("drv-1", "driver@test.com")
        )
        mock_sb.get_single.return_value = DRIVER_PROFILE
        mock_sb.update.return_value = DRIVER_PROFILE

        resp = client.post("/signup", json={
            "email": "driver@test.com",
            "password": "Secure1pass",
            "role": "driver",
            "full_name": "Driver Dan",
            "license_number": "ZM-DL-12345",
            "vehicle_type": "motorcycle",
        })
        assert resp.status_code == 200
        assert resp.json()["role"] == "driver"

    def test_driver_signup_requires_license(self, client, mock_sb):
        resp = client.post("/signup", json={
            "email": "driver@test.com",
            "password": "Secure1pass",
            "role": "driver",
        })
        assert resp.status_code == 400
        assert "license_number" in resp.json()["detail"].lower()

    def test_invalid_role_rejected(self, client):
        resp = client.post("/signup", json={
            "email": "bad@test.com",
            "password": "Secure1pass",
            "role": "admin",
        })
        assert resp.status_code == 422  # pydantic validation error

    def test_weak_password_rejected(self, client):
        resp = client.post("/signup", json={
            "email": "user@test.com",
            "password": "short",
            "role": "customer",
        })
        assert resp.status_code == 422

    def test_password_needs_digit(self, client):
        resp = client.post("/signup", json={
            "email": "user@test.com",
            "password": "NoDigitsHere",
            "role": "customer",
        })
        assert resp.status_code == 422

    def test_password_needs_uppercase(self, client):
        resp = client.post("/signup", json={
            "email": "user@test.com",
            "password": "alllower1",
            "role": "customer",
        })
        assert resp.status_code == 422

    def test_invalid_email_rejected(self, client):
        resp = client.post("/signup", json={
            "email": "not-an-email",
            "password": "Secure1pass",
            "role": "customer",
        })
        assert resp.status_code == 422

    def test_supabase_returns_no_user(self, client, mock_sb):
        mock_sb.client.auth.sign_up.return_value = MagicMock(user=None)

        resp = client.post("/signup", json={
            "email": "dup@test.com",
            "password": "Secure1pass",
            "role": "customer",
        })
        assert resp.status_code == 400

    def test_supabase_unavailable(self, client, mock_sb):
        mock_sb.client.auth.sign_up.side_effect = ConnectionError("timeout")

        resp = client.post("/signup", json={
            "email": "user@test.com",
            "password": "Secure1pass",
            "role": "customer",
        })
        assert resp.status_code == 502


# ===================================================================
#  Login
# ===================================================================

class TestLogin:

    def test_login_success_returns_profile(self, client, mock_sb):
        mock_sb.client.auth.sign_in_with_password.return_value = MagicMock(
            user=make_mock_user(),
            session=make_mock_session(),
        )
        mock_sb.get_single.return_value = SAMPLE_PROFILE

        resp = client.post("/login", json={
            "email": "test@example.com",
            "password": "Secure1pass",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"] == "access-tok"
        assert body["token_type"] == "bearer"
        assert body["user"]["email"] == "test@example.com"
        assert body["user"]["role"] == "customer"

    def test_login_invalid_credentials(self, client, mock_sb):
        mock_sb.client.auth.sign_in_with_password.return_value = MagicMock(session=None)

        resp = client.post("/login", json={
            "email": "test@example.com",
            "password": "WrongPassword1",
        })
        assert resp.status_code == 401

    def test_login_supabase_unavailable(self, client, mock_sb):
        mock_sb.client.auth.sign_in_with_password.side_effect = ConnectionError()

        resp = client.post("/login", json={
            "email": "test@example.com",
            "password": "Secure1pass",
        })
        assert resp.status_code == 502

    def test_login_profile_missing(self, client, mock_sb):
        mock_sb.client.auth.sign_in_with_password.return_value = MagicMock(
            user=make_mock_user(),
            session=make_mock_session(),
        )
        mock_sb.get_single.return_value = None

        resp = client.post("/login", json={
            "email": "test@example.com",
            "password": "Secure1pass",
        })
        assert resp.status_code == 404


# ===================================================================
#  Profile
# ===================================================================

class TestProfile:

    def test_get_profile(self, client, mock_sb, mock_auth_user):
        mock_sb.get_single.return_value = SAMPLE_PROFILE

        resp = client.get("/profile", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "test@example.com"

    def test_get_profile_no_token(self, client):
        resp = client.get("/profile")
        assert resp.status_code in (401, 403)

    def test_update_profile(self, client, mock_sb, mock_auth_user):
        updated = {**SAMPLE_PROFILE, "full_name": "Updated Name"}
        mock_sb.update.return_value = updated

        resp = client.put("/profile", json={"full_name": "Updated Name"},
                          headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        assert "Updated" in resp.json()["message"]

    def test_update_profile_empty_body(self, client, mock_sb, mock_auth_user):
        resp = client.put("/profile", json={},
                          headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 400

    def test_deactivate_account(self, client, mock_sb, mock_auth_user):
        mock_sb.update.return_value = {**SAMPLE_PROFILE, "is_active": False}

        resp = client.delete("/profile", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        assert "deactivated" in resp.json()["message"].lower()


# ===================================================================
#  Password management
# ===================================================================

class TestPassword:

    def test_request_reset(self, client, mock_sb):
        resp = client.post("/password/reset", json={"email": "user@test.com"})
        assert resp.status_code == 200
        # Must never reveal whether the email exists
        assert "reset link" in resp.json()["message"].lower()

    def test_update_password(self, client, mock_sb, mock_auth_user):
        resp = client.put("/password", json={"new_password": "NewSecure1"},
                          headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200


# ===================================================================
#  Admin endpoints
# ===================================================================

class TestAdmin:

    def test_list_users_as_admin(self, client, mock_sb, mock_admin_user):
        query_mock = MagicMock()
        query_mock.execute.return_value = MagicMock(data=[SAMPLE_PROFILE, SME_PROFILE])
        # Chain: table().select().order().range() -> query_mock
        mock_sb.client.table.return_value.select.return_value \
            .order.return_value.range.return_value = query_mock

        resp = client.get("/users", headers={"Authorization": "Bearer admin-tok"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_list_users_as_customer_forbidden(self, client, mock_sb, mock_auth_user):
        # mock_auth_user is a customer, so require_roles([ADMIN]) should reject
        resp = client.get("/users", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 403

    def test_get_user_by_id_as_admin(self, client, mock_sb, mock_admin_user):
        mock_sb.get_single.return_value = SAMPLE_PROFILE

        resp = client.get("/users/user-100", headers={"Authorization": "Bearer admin-tok"})
        assert resp.status_code == 200
        assert resp.json()["id"] == "user-100"

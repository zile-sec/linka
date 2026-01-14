import pytest
from unittest.mock import patch, MagicMock
import json
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from main import app
from fastapi.testclient import TestClient


client = TestClient(app)


class TestHealthChecks:
    """Test health check endpoints"""
    
    def test_health_endpoint(self):
        """Test liveness probe"""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"
        assert response.json()["service"] == "user-service"
    
    @patch("requests.get")
    def test_ready_endpoint_success(self, mock_get):
        """Test readiness probe when Supabase is available"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
    
    @patch("requests.get")
    def test_ready_endpoint_supabase_down(self, mock_get):
        """Test readiness probe when Supabase is down"""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_get.return_value = mock_response
        
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not ready"
    
    @patch("requests.get")
    def test_ready_endpoint_connection_error(self, mock_get):
        """Test readiness probe when Supabase connection fails"""
        mock_get.side_effect = ConnectionError("Connection refused")
        
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not ready"


class TestUserSignup:
    """Test user signup endpoint"""
    
    @patch("requests.request")
    @patch("requests.post")
    def test_signup_customer_success(self, mock_post, mock_request):
        """Test successful customer signup"""
        # Mock Supabase auth response
        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 200
        mock_auth_response.json.return_value = {
            "access_token": "test-jwt-token",
            "user": {"id": "user-123"}
        }
        mock_request.return_value = mock_auth_response
        
        response = client.post("/signup", json={
            "email": "customer@example.com",
            "password": "SecurePass123!",
            "role": "customer"
        })
        
        assert response.status_code == 200
        assert response.json()["role"] == "customer"
        assert "token" in response.json()
        assert response.json()["message"] == "User created"
    
    @patch("requests.request")
    @patch("requests.post")
    def test_signup_retailer_triggers_kyc(self, mock_post, mock_request):
        """Test that retailer signup triggers KYC workflow"""
        # Mock Supabase auth response
        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 200
        mock_auth_response.json.return_value = {
            "access_token": "test-jwt-token",
            "user": {"id": "retailer-123"}
        }
        mock_request.return_value = mock_auth_response
        
        response = client.post("/signup", json={
            "email": "retailer@example.com",
            "password": "SecurePass123!",
            "role": "retailer"
        })
        
        assert response.status_code == 200
        assert response.json()["role"] == "retailer"
        
        # Verify KYC was triggered
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "kyc/initiate" in call_args[0][0]
    
    def test_signup_invalid_role(self):
        """Test signup with invalid role"""
        response = client.post("/signup", json={
            "email": "user@example.com",
            "password": "SecurePass123!",
            "role": "admin"
        })
        
        assert response.status_code == 400
        assert "Invalid role" in response.json()["detail"]
    
    def test_signup_invalid_email_format(self):
        """Test signup with invalid email format"""
        response = client.post("/signup", json={
            "email": "not-an-email",
            "password": "SecurePass123!",
            "role": "customer"
        })
        
        assert response.status_code == 422
    
    @patch("requests.request")
    def test_signup_supabase_error(self, mock_request):
        """Test signup when Supabase returns an error"""
        mock_error_response = MagicMock()
        mock_error_response.status_code = 400
        mock_error_response.json.return_value = {"error": "User already exists"}
        mock_request.return_value = mock_error_response
        
        response = client.post("/signup", json={
            "email": "existing@example.com",
            "password": "SecurePass123!",
            "role": "customer"
        })
        
        assert response.status_code == 400


class TestUserLogin:
    """Test user login endpoint"""
    
    @patch("requests.request")
    def test_login_success(self, mock_request):
        """Test successful login"""
        # Mock Supabase token response
        mock_token_response = MagicMock()
        mock_token_response.status_code = 200
        mock_token_response.json.side_effect = [
            {"access_token": "test-jwt-token"},
            {
                "user_metadata": {"role": "customer"},
                "email": "user@example.com"
            }
        ]
        mock_request.return_value = mock_token_response
        
        response = client.post("/login", json={
            "email": "user@example.com",
            "password": "SecurePass123!"
        })
        
        assert response.status_code == 200
        assert response.json()["role"] == "customer"
        assert "token" in response.json()
    
    @patch("requests.request")
    def test_login_invalid_credentials(self, mock_request):
        """Test login with invalid credentials"""
        mock_error_response = MagicMock()
        mock_error_response.status_code = 401
        mock_error_response.json.return_value = {"error": "Invalid credentials"}
        mock_request.return_value = mock_error_response
        
        response = client.post("/login", json={
            "email": "user@example.com",
            "password": "WrongPassword"
        })
        
        assert response.status_code == 401
    
    def test_login_invalid_email_format(self):
        """Test login with invalid email format"""
        response = client.post("/login", json={
            "email": "not-an-email",
            "password": "SecurePass123!"
        })
        
        assert response.status_code == 422


class TestUserProfile:
    """Test user profile endpoint"""
    
    @patch("requests.request")
    def test_get_profile_success(self, mock_request):
        """Test successful profile retrieval"""
        mock_profile_response = MagicMock()
        mock_profile_response.status_code = 200
        mock_profile_response.json.return_value = {
            "id": "user-123",
            "email": "user@example.com",
            "user_metadata": {"role": "customer"}
        }
        mock_request.return_value = mock_profile_response
        
        response = client.get(
            "/profile",
            headers={"Authorization": "Bearer test-jwt-token"}
        )
        
        assert response.status_code == 200
        assert response.json()["email"] == "user@example.com"
    
    def test_get_profile_missing_token(self):
        """Test profile retrieval without token"""
        response = client.get("/profile")
        
        assert response.status_code == 403
    
    @patch("requests.request")
    def test_get_profile_invalid_token(self, mock_request):
        """Test profile retrieval with invalid token"""
        mock_error_response = MagicMock()
        mock_error_response.status_code = 401
        mock_error_response.json.return_value = {"error": "Invalid token"}
        mock_request.return_value = mock_error_response
        
        response = client.get(
            "/profile",
            headers={"Authorization": "Bearer invalid-token"}
        )
        
        assert response.status_code == 401


class TestSupabaseIntegration:
    """Integration tests with Supabase mocking"""
    
    @patch("requests.request")
    def test_signup_and_login_flow(self, mock_request):
        """Test complete signup and login flow"""
        # First request: signup
        signup_response = MagicMock()
        signup_response.status_code = 200
        signup_response.json.return_value = {
            "access_token": "signup-token",
            "user": {"id": "new-user-123"}
        }
        
        # Subsequent requests for profile update and login
        login_response = MagicMock()
        login_response.status_code = 200
        login_response.json.side_effect = [
            signup_response.json(),
            signup_response.json(),  # For profile update
            {"access_token": "login-token"},
            {"user_metadata": {"role": "customer"}}  # For login profile fetch
        ]
        mock_request.return_value = login_response
        
        # Signup
        signup = client.post("/signup", json={
            "email": "newuser@example.com",
            "password": "SecurePass123!",
            "role": "customer"
        })
        
        assert signup.status_code == 200
        signup_token = signup.json()["token"]
        
        # Login
        login = client.post("/login", json={
            "email": "newuser@example.com",
            "password": "SecurePass123!"
        })
        
        assert login.status_code == 200
        assert "token" in login.json()
    
    @patch("requests.request")
    def test_supabase_timeout_handling(self, mock_request):
        """Test handling of Supabase timeout"""
        mock_request.side_effect = TimeoutError("Supabase connection timeout")
        
        response = client.post("/signup", json={
            "email": "user@example.com",
            "password": "SecurePass123!",
            "role": "customer"
        })
        
        assert response.status_code == 500

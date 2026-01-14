#!/bin/bash

# Configuration
BASE_URL="http://localhost:8080"
EMAIL="quickfixzed@gmail.com" # Unique email each run
PASSWORD="StrongPass123!"

echo "========================================"
echo "🚀 Starting API Gateway Curl Tests"
echo "Target: $BASE_URL"
echo "User:   $EMAIL"
echo "========================================"

# 1. Test Health Check
echo -e "\n1. Testing Liveness Probe (/health)..."
curl -v "$BASE_URL/health"

# 2. Test Readiness Check
echo -e "\n2. Testing Readiness Probe (/ready)..."
curl -v "$BASE_URL/ready"

# 3. Test Signup (Public Endpoint)
echo -e "\n3. Testing Signup (/auth/signup)..."
SIGNUP_RESPONSE=$(curl -v -X POST "$BASE_URL/auth/signup" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$EMAIL\", \"password\": \"$PASSWORD\", \"role\": \"business\", \"category\": \"tech\"}")

echo "Response: $SIGNUP_RESPONSE"

# 4. Test Login (Public Endpoint)
echo -e "\n4. Testing Login (/auth/login)..."
LOGIN_RESPONSE=$(curl -v -X POST "$BASE_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$EMAIL\", \"password\": \"$PASSWORD\"}")

# Extract Token (Assuming standard JWT response structure)
# Adjust jq selector based on your actual Auth provider response
TOKEN=$(echo $LOGIN_RESPONSE | jq -r '.token // .access_token // .session.access_token // empty')

if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then
    echo "⚠️  Login failed or no token returned. Skipping authenticated tests."
    echo "Raw Login Response: $LOGIN_RESPONSE"
    # For testing purposes without a real backend, you might manually set a token here:
    # TOKEN="your_manual_jwt_here"
else
    echo "✅ Token received: ${TOKEN:0:15}..."

    # 5. Test Proxy to User Service (Requires Auth)
    # Maps to: user-service/profile (or /health if profile not implemented)
    echo -e "\n5. Testing Authenticated Proxy (/user/profile)..."
    curl -v -X GET "$BASE_URL/user/profile" \
      -H "Authorization: Bearer $TOKEN"

    # 6. Test Compliance Header - FAILURE CASE
    # Should fail because 'kyc_context' header is missing for wallet POST
    echo -e "\n6. Testing Compliance Check (Expecting 400 Failure)..."
    curl -v -X POST "$BASE_URL/wallet/transfer" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"amount": 100, "recipient": "u123"}'

    # 7. Test Compliance Header - SUCCESS CASE
    # Should pass (or forward) because header is present
    echo -e "\n7. Testing Compliance Check (Expecting Success/Forward)..."
    curl -v -X POST "$BASE_URL/wallet/transfer" \
      -H "Authorization: Bearer $TOKEN" \
      -H "kyc_context: verified_tier_1" \
      -H "Content-Type: application/json" \
      -d '{"amount": 100, "recipient": "u123"}'
fi

echo -e "\n========================================"
echo "🏁 Tests Completed"
echo "========================================"
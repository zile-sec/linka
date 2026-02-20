"""
Mobile Money and Bank Provider Adapters
Handles external API communication with MTN, Airtel, Zamtel, and banks.
Each adapter is a simple function: request -> response dict.
"""

import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict

import httpx

logger = logging.getLogger("wallet-service.providers")

# ===================================================================
#  HELPERS
# ===================================================================

def _sign_payload(payload: str, secret: str) -> str:
    """Generate HMAC-SHA256 signature."""
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _http_post(url: str, headers: Dict, json_data: Dict, timeout: int = 10) -> Dict:
    """Wrapper around httpx POST with error handling."""
    try:
        resp = httpx.post(url, headers=headers, json=json_data, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        logger.error("http POST %s failed: %s %s", url, e.response.status_code, e.response.text)
        return {"error": "api_error", "message": str(e)}
    except Exception as e:
        logger.error("http POST %s exception: %s", url, e)
        return {"error": "network_error", "message": str(e)}


# ===================================================================
#  MTN MOBILE MONEY
# ===================================================================

def mtn_request_payment(phone: str, amount: Decimal, reference: str) -> Dict[str, Any]:
    """
    MTN MoMo Collections API: request a payment from a user.
    Docs: https://momodeveloper.mtn.com/docs/services/collection
    """
    base_url = os.getenv("MTN_MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com")
    api_key = os.getenv("MTN_MOMO_API_KEY", "")
    subscription_key = os.getenv("MTN_MOMO_SUBSCRIPTION_KEY", "")
    callback_url = os.getenv("MTN_MOMO_CALLBACK_URL", "")
    
    if not api_key or not subscription_key:
        logger.warning("MTN credentials missing, using mock response")
        return {
            "status": "processing",
            "provider_ref": f"mtn-mock-{uuid.uuid4().hex[:12]}",
            "message": "MTN payment initiated (mock)",
        }
    
    # Generate unique reference
    tx_ref = f"mtn-{uuid.uuid4().hex[:16]}"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Reference-Id": tx_ref,
        "X-Target-Environment": "sandbox",  # change to "mtnnigeria" or "mtnuganda" in prod
        "Ocp-Apim-Subscription-Key": subscription_key,
        "Content-Type": "application/json",
    }
    
    payload = {
        "amount": str(amount),
        "currency": "ZMW",
        "externalId": reference,
        "payer": {
            "partyIdType": "MSISDN",
            "partyId": phone.lstrip("+"),
        },
        "payerMessage": "Payment to Linka Wallet",
        "payeeNote": f"Linka ref {reference}",
    }
    
    if callback_url:
        payload["callbackUrl"] = callback_url
    
    url = f"{base_url}/collection/v1_0/requesttopay"
    result = _http_post(url, headers, payload)
    
    if "error" in result:
        return {
            "status": "failed",
            "provider_ref": tx_ref,
            "error": result.get("error"),
            "message": result.get("message", "MTN API call failed"),
        }
    
    logger.info("MTN payment requested: ref=%s phone=%s amount=%s", tx_ref, phone, amount)
    return {
        "status": "processing",
        "provider_ref": tx_ref,
        "message": "Approve the payment on your MTN phone",
    }


def mtn_check_transaction_status(tx_ref: str) -> Dict[str, Any]:
    """Poll MTN for transaction status."""
    base_url = os.getenv("MTN_MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com")
    api_key = os.getenv("MTN_MOMO_API_KEY", "")
    subscription_key = os.getenv("MTN_MOMO_SUBSCRIPTION_KEY", "")
    
    if not api_key or not subscription_key:
        return {"status": "unknown", "message": "Credentials missing"}
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Target-Environment": "sandbox",
        "Ocp-Apim-Subscription-Key": subscription_key,
    }
    
    url = f"{base_url}/collection/v1_0/requesttopay/{tx_ref}"
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # MTN returns "SUCCESSFUL", "FAILED", "PENDING"
        mtn_status = data.get("status", "UNKNOWN")
        if mtn_status == "SUCCESSFUL":
            return {"status": "completed", "data": data}
        elif mtn_status == "FAILED":
            return {"status": "failed", "data": data, "message": data.get("reason")}
        else:
            return {"status": "processing", "data": data}
    except Exception as e:
        logger.error("MTN status check failed: %s", e)
        return {"status": "unknown", "message": str(e)}


def mtn_disburse(phone: str, amount: Decimal, reference: str) -> Dict[str, Any]:
    """MTN Disbursement (send money to a user)."""
    base_url = os.getenv("MTN_MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com")
    api_key = os.getenv("MTN_MOMO_API_KEY", "")
    subscription_key = os.getenv("MTN_MOMO_SUBSCRIPTION_KEY", "")
    
    if not api_key or not subscription_key:
        logger.warning("MTN disbursement: credentials missing, using mock")
        return {
            "status": "processing",
            "provider_ref": f"mtn-disburse-{uuid.uuid4().hex[:12]}",
            "message": "Disbursement initiated (mock)",
        }
    
    tx_ref = f"mtn-disburse-{uuid.uuid4().hex[:16]}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Reference-Id": tx_ref,
        "X-Target-Environment": "sandbox",
        "Ocp-Apim-Subscription-Key": subscription_key,
        "Content-Type": "application/json",
    }
    
    payload = {
        "amount": str(amount),
        "currency": "ZMW",
        "externalId": reference,
        "payee": {
            "partyIdType": "MSISDN",
            "partyId": phone.lstrip("+"),
        },
        "payerMessage": "Disbursement from Linka Wallet",
        "payeeNote": f"Linka ref {reference}",
    }
    
    url = f"{base_url}/disbursement/v1_0/transfer"
    result = _http_post(url, headers, payload)
    
    if "error" in result:
        return {
            "status": "failed",
            "provider_ref": tx_ref,
            "error": result.get("error"),
            "message": result.get("message", "MTN disbursement failed"),
        }
    
    logger.info("MTN disbursement: ref=%s phone=%s amount=%s", tx_ref, phone, amount)
    return {
        "status": "processing",
        "provider_ref": tx_ref,
        "message": "Disbursement initiated",
    }


# ===================================================================
#  AIRTEL MONEY
# ===================================================================

def airtel_request_payment(phone: str, amount: Decimal, reference: str) -> Dict[str, Any]:
    """Airtel Money Push Payment."""
    base_url = os.getenv("AIRTEL_MONEY_BASE_URL", "https://openapiuat.airtel.africa")
    api_key = os.getenv("AIRTEL_MONEY_API_KEY", "")
    api_secret = os.getenv("AIRTEL_MONEY_API_SECRET", "")
    
    if not api_key or not api_secret:
        logger.warning("Airtel credentials missing, using mock")
        return {
            "status": "processing",
            "provider_ref": f"airtel-mock-{uuid.uuid4().hex[:12]}",
            "message": "Airtel payment initiated (mock)",
        }
    
    tx_ref = f"airtel-{uuid.uuid4().hex[:16]}"
    headers = {
        "Content-Type": "application/json",
        "X-Country": "ZM",
        "X-Currency": "ZMW",
        "Authorization": f"Bearer {api_key}",
    }
    
    payload = {
        "reference": tx_ref,
        "subscriber": {
            "country": "ZM",
            "currency": "ZMW",
            "msisdn": phone.lstrip("+"),
        },
        "transaction": {
            "amount": float(amount),
            "country": "ZM",
            "currency": "ZMW",
            "id": reference,
        },
    }
    
    url = f"{base_url}/merchant/v1/payments/"
    result = _http_post(url, headers, payload)
    
    if "error" in result:
        return {
            "status": "failed",
            "provider_ref": tx_ref,
            "error": result.get("error"),
            "message": result.get("message", "Airtel API error"),
        }
    
    logger.info("Airtel payment requested: ref=%s phone=%s amount=%s", tx_ref, phone, amount)
    return {
        "status": "processing",
        "provider_ref": tx_ref,
        "message": "Approve on your Airtel phone",
    }


def airtel_check_transaction_status(tx_ref: str) -> Dict[str, Any]:
    """Poll Airtel for status."""
    base_url = os.getenv("AIRTEL_MONEY_BASE_URL", "https://openapiuat.airtel.africa")
    api_key = os.getenv("AIRTEL_MONEY_API_KEY", "")
    
    if not api_key:
        return {"status": "unknown", "message": "Credentials missing"}
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Country": "ZM",
        "X-Currency": "ZMW",
    }
    
    url = f"{base_url}/standard/v1/payments/{tx_ref}"
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        status_code = data.get("data", {}).get("transaction", {}).get("status")
        if status_code == "TS":
            return {"status": "completed", "data": data}
        elif status_code == "TF":
            return {"status": "failed", "data": data}
        else:
            return {"status": "processing", "data": data}
    except Exception as e:
        logger.error("Airtel status check failed: %s", e)
        return {"status": "unknown", "message": str(e)}


# ===================================================================
#  ZAMTEL KWACHA
# ===================================================================

def zamtel_request_payment(phone: str, amount: Decimal, reference: str) -> Dict[str, Any]:
    """Zamtel Kwacha payment request (API structure is a placeholder)."""
    base_url = os.getenv("ZAMTEL_KWACHA_BASE_URL", "https://api.zamtel.co.zm")
    api_key = os.getenv("ZAMTEL_KWACHA_API_KEY", "")
    
    if not api_key:
        logger.warning("Zamtel credentials missing, using mock")
        return {
            "status": "processing",
            "provider_ref": f"zamtel-mock-{uuid.uuid4().hex[:12]}",
            "message": "Zamtel payment initiated (mock)",
        }
    
    tx_ref = f"zamtel-{uuid.uuid4().hex[:16]}"
    # NOTE: Replace with actual Zamtel API structure when available
    logger.info("Zamtel payment (placeholder): ref=%s phone=%s amount=%s", tx_ref, phone, amount)
    return {
        "status": "processing",
        "provider_ref": tx_ref,
        "message": "Approve on your Zamtel phone",
    }


def zamtel_check_transaction_status(tx_ref: str) -> Dict[str, Any]:
    """Poll Zamtel for status."""
    logger.info("Zamtel status check (placeholder): %s", tx_ref)
    return {"status": "processing", "message": "Placeholder"}


# ===================================================================
#  BANK ACCOUNT BALANCE (via Unified Payment Interface or Open Banking)
# ===================================================================

def bank_get_balance(account_id: str, user_id: str) -> Dict[str, Any]:
    """
    Fetch balance from a linked bank account.
    This is a placeholder; implement via Open Banking API or bank-specific API.
    """
    logger.info("bank_get_balance: account=%s user=%s", account_id, user_id)
    # Mock response
    return {
        "status": "success",
        "balance": 0.00,
        "currency": "ZMW",
        "message": "Bank balance check (placeholder)",
    }


# ===================================================================
#  PROVIDER DISPATCHER
# ===================================================================

PROVIDER_REQUEST_MAP = {
    "mtn": mtn_request_payment,
    "airtel": airtel_request_payment,
    "zamtel": zamtel_request_payment,
}

PROVIDER_STATUS_MAP = {
    "mtn": mtn_check_transaction_status,
    "airtel": airtel_check_transaction_status,
    "zamtel": zamtel_check_transaction_status,
}

PROVIDER_DISBURSE_MAP = {
    "mtn": mtn_disburse,
    # Add Airtel/Zamtel disbursement when available
}


def request_payment(provider: str, phone: str, amount: Decimal, reference: str) -> Dict[str, Any]:
    """Dispatch payment request to the correct provider."""
    handler = PROVIDER_REQUEST_MAP.get(provider.lower())
    if not handler:
        return {"status": "failed", "error": "unsupported_provider", "message": f"Provider {provider} not supported"}
    return handler(phone, amount, reference)


def check_transaction_status(provider: str, tx_ref: str) -> Dict[str, Any]:
    """Dispatch status check to the correct provider."""
    handler = PROVIDER_STATUS_MAP.get(provider.lower())
    if not handler:
        return {"status": "unknown", "message": "Provider not supported"}
    return handler(tx_ref)


def disburse_funds(provider: str, phone: str, amount: Decimal, reference: str) -> Dict[str, Any]:
    """Dispatch disbursement to the correct provider."""
    handler = PROVIDER_DISBURSE_MAP.get(provider.lower())
    if not handler:
        return {"status": "failed", "error": "unsupported_provider", "message": "Disbursement not supported for this provider"}
    return handler(phone, amount, reference)

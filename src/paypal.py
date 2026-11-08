import uuid
import base64
import aiohttp
from config import (
    PAYPAL_CLIENT_ID, 
    PAYPAL_CLIENT_SECRET, 
    PAYPAL_API_BASE, 
    PUBLIC_BASE_URL, 
    logger
)
from database import deals_db

def is_paypal_configured() -> bool:
    """Checks if real PayPal Sandbox credentials are set in .env."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return False
    if "your_sandbox" in PAYPAL_CLIENT_ID.lower() or "your_paypal" in PAYPAL_CLIENT_ID.lower():
        return False
    return True

async def get_paypal_access_token() -> str:
    """Generates an OAuth2 access token for PayPal Sandbox."""
    url = f"{PAYPAL_API_BASE}/v1/oauth2/token"
    auth_string = f"{PAYPAL_CLIENT_ID.strip()}:{PAYPAL_CLIENT_SECRET.strip()}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()
    
    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=data) as response:
            if response.status == 200:
                json_resp = await response.json()
                return json_resp["access_token"]
            else:
                error_text = await response.text()
                logger.error(f"PayPal OAuth Error ({response.status}): {error_text}")
                raise Exception(f"PayPal Auth Failed: {error_text}")

async def authorize_order(order_id: str) -> str:
    """Places the hold on the buyer's funds after they approve the order on PayPal."""
    if not is_paypal_configured() or order_id.startswith("MOCK_"):
        return f"MOCK_AUTH_{uuid.uuid4().hex[:8].upper()}"

    token = await get_paypal_access_token()
    url = f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/authorize"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": f"auth_{order_id}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={}) as response:
            data = await response.json()
            if response.status in (200, 201):
                auth_id = (
                    data["purchase_units"][0]["payments"]["authorizations"][0]["id"]
                )
                return auth_id
            else:
                logger.error(f"PayPal Authorize Error: {data}")
                raise Exception(f"Failed to authorize order: {data}")

async def capture_authorization(authorization_id: str) -> str:
    """Releases the held funds to the seller once delivery proof is verified."""
    if not is_paypal_configured() or authorization_id.startswith("MOCK_"):
        return f"MOCK_CAP_{uuid.uuid4().hex[:8].upper()}"

    token = await get_paypal_access_token()
    url = f"{PAYPAL_API_BASE}/v2/payments/authorizations/{authorization_id}/capture"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": f"cap_{authorization_id}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={"final_capture": True}) as response:
            data = await response.json()
            if response.status in (200, 201):
                return data.get("id")
            else:
                logger.error(f"PayPal Capture Error: {data}")
                raise Exception(f"Failed to capture funds: {data}")

async def void_authorization(authorization_id: str) -> bool:
    """Cancels the hold and returns funds to the buyer if the deal is cancelled or expires."""
    if not is_paypal_configured() or authorization_id.startswith("MOCK_"):
        return True

    token = await get_paypal_access_token()
    url = f"{PAYPAL_API_BASE}/v2/payments/authorizations/{authorization_id}/void"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": f"void_{authorization_id}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers) as response:
            if response.status == 204:
                return True
            data = await response.text()
            logger.error(f"PayPal Void Error: {data}")
            return False

async def create_authorize_order(deal_id: str, amount: str = "50.00", currency: str = "USD") -> str:
    """
    Creates a PayPal order with intent AUTHORIZE and returns the buyer approval URL.
    """
    if not is_paypal_configured():
        logger.warning("PayPal credentials not set in .env! Using local mock checkout URL.")
        if deal_id in deals_db:
            deals_db[deal_id]["paypal_order_id"] = f"MOCK_ORDER_{deal_id}"
        return f"{PUBLIC_BASE_URL}/paypal/mock-checkout?deal_id={deal_id}&amount={amount}&currency={currency}"

    token = await get_paypal_access_token()
    url = f"{PAYPAL_API_BASE}/v2/checkout/orders"
    
    request_id = f"create_{deal_id}_{uuid.uuid4().hex[:6]}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": request_id
    }
    
    payload = {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "reference_id": deal_id,
                "custom_id": deal_id,
                "description": f"Escrow Hold for Deal #{deal_id}",
                "amount": {
                    "currency_code": currency,
                    "value": amount
                }
            }
        ],
        # FIXED: Must be lowercase "application_context" so PayPal redirects back!
        "application_context": {
            "return_url": f"{PUBLIC_BASE_URL}/paypal/return?deal_id={deal_id}",
            "cancel_url": f"{PUBLIC_BASE_URL}/paypal/cancel?deal_id={deal_id}",
            "brand_name": "AI Deal Agent",
            "user_action": "CONTINUE"
        }
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            order_data = await response.json()
            if response.status in (200, 201):
                order_id = order_data.get("id")
                if deal_id in deals_db:
                    deals_db[deal_id]["paypal_order_id"] = order_id
                
                for link in order_data.get("links", []):
                    if link.get("rel") in ("approve", "payer-action"):
                        return link["href"]
                        
                raise Exception(f"No approval URL found in PayPal response: {order_data}")
            else:
                logger.error(f"PayPal Create Order Error: {order_data}")
                raise Exception(f"PayPal Order Error: {order_data.get('message', 'Unknown error')}")

async def verify_and_authorize_deal(deal_id: str) -> tuple[bool, str]:
    """
    Checks the order status directly on PayPal when the buyer says 'I have paid' 
    or clicks 'Verify Payment'. If APPROVED, authorizes the hold immediately.
    """
    deal = deals_db.get(deal_id)
    if not deal:
        return False, "Deal not found."
        
    if deal.get("state") == "FUNDS_HELD":
        return True, deal.get("authorization_id", "ALREADY_HELD")

    order_id = deal.get("paypal_order_id")
    if not order_id:
        return False, "No PayPal order found for this deal."

    if not is_paypal_configured() or order_id.startswith("MOCK_"):
        auth_id = f"MOCK_AUTH_{uuid.uuid4().hex[:8].upper()}"
        return True, auth_id

    token = await get_paypal_access_token()
    url = f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            data = await response.json()
            status = data.get("status")
            logger.info(f"Checked PayPal Order {order_id} status: {status}")

            if status == "APPROVED":
                # Buyer approved it on PayPal! Now place the authorization hold:
                auth_id = await authorize_order(order_id)
                return True, auth_id
            elif status == "COMPLETED":
                # Already authorized/captured
                try:
                    auth_id = data["purchase_units"][0]["payments"]["authorizations"][0]["id"]
                except Exception:
                    auth_id = order_id
                return True, auth_id
            else:
                return False, f"PayPal order status is still '{status}'. Please complete the checkout login on PayPal first."
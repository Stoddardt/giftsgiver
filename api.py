import os, time, base64, urllib.parse, requests
from typing import Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
EPN_CAMPAIGN_ID = os.getenv("EPN_CAMPAIGN_ID")

app = FastAPI(title="GiftGiver eBay API")

# Root route for Render health check
@app.get("/")
def root():
    return {"status": "ok", "service": "giftgiver"}

# Allow requests from GHL or your web app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache token to avoid repeated requests
_token_cache = {"value": None, "exp": 0.0}

def get_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["exp"] - 60:
        return _token_cache["value"]

    auth = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    # ✅ IMPORTANT: correct scope for Browse API
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope/buy.browse"
    }

    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token", headers=headers, data=data)
    r.raise_for_status()
    token_data = r.json()
    _token_cache["value"] = token_data["access_token"]
    _token_cache["exp"] = now + token_data.get("expires_in", 7200)
    return _token_cache["value"]

def epn_link(item_url: str, customid: Optional[str] = None) -> str:
    base = "https://rover.ebay.com/rover/1/711-53200-19255-0/1"
    u = f"{base}?mpre={urllib.parse.quote(item_url, safe='')}&campid={EPN_CAMPAIGN_ID}"
    if customid:
        u += f"&customid={urllib.parse.quote(customid)}"
    return u

class SearchIn(BaseModel):
    query: str
    maxPrice: Optional[float] = None
    customId: Optional[str] = None
    limit: int = 10

@app.post("/search")
def search(payload: SearchIn):
    token = get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json"
    }

    params = {"q": payload.query, "limit": str(payload.limit)}
    if payload.maxPrice:
        params["price_currency"] = "USD"
        params["price_max"] = str(payload.maxPrice)

    r = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search", headers=headers, params=params)
    r.raise_for_status()
    data = r.json()

    # ✅ If eBay returns an error payload, show it to help debugging
    if "errors" in data:
        return {"items": [], "ebay_errors": data["errors"]}

    items = []
    for it in data.get("itemSummaries", []):
        url = it.get("itemWebUrl")
        items.append({
            "id": it.get("itemId"),
            "title": it.get("title"),
            "price": (it.get("price") or {}).get("value"),
            "image": (it.get("image") or {}).get("imageUrl"),
            "url_affiliate": epn_link(url, payload.customId) if url else None,
        })

    return {"count": len(items), "items": items}

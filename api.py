# api.py
import os
import time
import base64
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
EPN_CAMPAIGN_ID = os.getenv("EPN_CAMPAIGN_ID")
EPN_CUSTOM_ID = os.getenv("EPN_CUSTOM_ID", "giftgiver")

if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
    raise RuntimeError("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in .env")

app = FastAPI(title="GiftGiver API", version="1.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------
# Models
# ---------------------------

class SuggestionRequest(BaseModel):
    source: Optional[str] = "ghl"
    payload: Dict[str, Any]

class SuggestionResponse(BaseModel):
    ok: bool
    query: Dict[str, Any]
    items: List[Dict[str, Any]]
    note: Optional[str] = None

# ---------------------------
# eBay helpers
# ---------------------------

_OAUTH_CACHE: Dict[str, Tuple[str, float]] = {}

def get_ebay_oauth_token(scope: str = "https://api.ebay.com/oauth/api_scope") -> str:
    """Get an app token for the Browse API. Only the base scope is required."""
    cache_key = "app_token"
    now = time.time()
    if cache_key in _OAUTH_CACHE:
        tok, exp = _OAUTH_CACHE[cache_key]
        if now < exp - 60:
            return tok

    auth_b64 = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    data = {"grant_type": "client_credentials", "scope": scope}
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {auth_b64}",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"eBay OAuth failed {resp.status_code} {resp.text}")

    j = resp.json()
    token = j["access_token"]
    expires_in = int(j.get("expires_in", 7200))
    _OAUTH_CACHE[cache_key] = (token, now + expires_in)
    return token

def affiliate_wrap(url: str) -> str:
    if not EPN_CAMPAIGN_ID:
        return url
    sep = "&" if "?" in url else "?"
    parts = [
        f"campid={EPN_CAMPAIGN_ID}",
        f"customid={EPN_CUSTOM_ID}" if EPN_CUSTOM_ID else None,
    ]
    tail = "&".join([p for p in parts if p])
    return f"{url}{sep}{tail}"

def call_ebay_browse_search(q: str, price_min: Optional[float], price_max: Optional[float], limit: int = 12) -> List[Dict[str, Any]]:
    # FIX: use only base scope
    token = get_ebay_oauth_token()
    endpoint = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    params = {"q": q, "limit": str(limit), "sort": "price+asc"}

    filters = []
    if price_min is not None and price_max is not None:
        filters.append(f"price:[{price_min}..{price_max}]")
    elif price_min is not None:
        filters.append(f"price:[{price_min}..]")
    elif price_max is not None:
        filters.append(f"price:[..{price_max}]")
    if filters:
        params["filter"] = ",".join(filters)

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.get(endpoint, headers=headers, params=params, timeout=30)

    if r.status_code != 200:
        logging.error("eBay search error %s %s", r.status_code, r.text)
        raise HTTPException(status_code=502, detail="eBay search failed")

    data = r.json()
    out = []
    for it in data.get("itemSummaries", []):
        title = it.get("title")
        url = it.get("itemWebUrl") or it.get("itemAffiliateWebUrl") or ""
        price = it.get("price", {}).get("value")
        currency = it.get("price", {}).get("currency")
        image = None
        if it.get("image", {}).get("imageUrl"):
            image = it["image"]["imageUrl"]
        elif it.get("thumbnailImages"):
            image = it["thumbnailImages"][0].get("imageUrl")

        out.append(
            {
                "title": title,
                "price": price,
                "currency": currency,
                "url": affiliate_wrap(url) if url else "",
                "image": image,
                "item_id": it.get("itemId"),
                "condition": it.get("condition"),
                "seller": it.get("seller", {}).get("username"),
            }
        )
    return out

# ---------------------------
# Form mapping and query build
# ---------------------------

def normalize_text(x: Optional[str]) -> str:
    return (x or "").strip()

def to_float(x: Any) -> Optional[float]:
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(str(x).replace("$", "").replace(",", "").strip())
    except Exception:
        return None

RANGE_PATTERN = re.compile(
    r"(?P<min>\d+(\.\d+)?)\s*(?:-|to|–|—|\.\.)\s*(?P<max>\d+(\.\d+)?)",
    re.IGNORECASE,
)

def parse_budget_range(raw: Any) -> Tuple[Optional[float], Optional[float]]:
    if raw is None:
        return None, None
    s = str(raw)
    s = s.replace("$", "").replace("USD", "").replace("usd", "").replace("dollars", "")
    m = RANGE_PATTERN.search(s)
    if m:
        lo = to_float(m.group("min"))
        hi = to_float(m.group("max"))
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return lo, hi
    nums = re.findall(r"\d+(\.\d+)?", s)
    if len(nums) == 1:
        val = to_float(nums[0])
        return None, val
    return None, None

def coerce_to_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [t.strip() for t in str(v).replace(";", ",").split(",") if t.strip()]

def join_terms(*parts: List[Union[str, List[str]]]) -> str:
    tokens: List[str] = []
    for p in parts:
        if p is None:
            continue
        if isinstance(p, list):
            tokens.extend([str(x).strip() for x in p if str(x).strip()])
        else:
            for t in str(p).split(","):
                tok = t.strip()
                if tok:
                    tokens.append(tok)
    return " ".join(tokens)

def pick(payload: Dict[str, Any], *keys: str) -> Optional[Any]:
    for k in keys:
        if k in payload and payload[k] not in (None, ""):
            return payload[k]
    return None

def flatten_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    if "contact" in raw and isinstance(raw["contact"], dict):
        base = dict(raw["contact"])
        base.update({k: v for k, v in raw.items() if k != "contact"})
        return base
    return raw

def build_query_from_form(raw_form: Dict[str, Any]) -> Dict[str, Any]:
    form = flatten_payload(raw_form)

    recipient = pick(form, "recipient_namenickname", "recipient_name", "recipient")
    relationship = pick(form, "relationship_to_you", "relationship")
    age_range = pick(form, "age_range")
    gender = pick(form, "genderpronouns", "gender_pronouns", "gender")
    occasion = pick(form, "occasion")
    hobbies_interests = coerce_to_list(pick(form, "hobbiesinterests", "hobbies", "interests"))
    favorite_brands = coerce_to_list(pick(form, "favorite_brandsstores", "favorite_brands", "brand_likes"))
    favorite_colors_styles = coerce_to_list(pick(form, "favorite_colorsstyles", "favorite_colors", "style"))
    dislikes = coerce_to_list(pick(form, "anything_they_dont_like__avoid", "dislikes", "avoid"))
    gift_type_pref = pick(form, "gift_type_preference", "gift_type")
    location_city = pick(form, "locationcity", "city")
    budget_range_raw = pick(form, "budget_range", "budget")
    budget_min, budget_max = parse_budget_range(budget_range_raw)

    q = join_terms(
        relationship,
        occasion,
        hobbies_interests,
        favorite_brands,
        favorite_colors_styles,
        gift_type_pref,
    )

    soft_terms: List[str] = []
    if age_range:
        soft_terms.append(str(age_range))
    if gender:
        soft_terms.append(str(gender))
    if location_city:
        soft_terms.append(str(location_city))
    if dislikes:
        soft_terms.extend([f"not {d}" for d in dislikes])

    if soft_terms:
        q = f"{q} " + " ".join(soft_terms)

    if not q.strip():
        q = "gift ideas"

    mapping_echo = {
        "recipient": recipient,
        "relationship": relationship,
        "age_range": age_range,
        "gender": gender,
        "occasion": occasion,
        "hobbies_interests": hobbies_interests,
        "favorite_brands": favorite_brands,
        "favorite_colors_styles": favorite_colors_styles,
        "dislikes": dislikes,
        "gift_type_preference": gift_type_pref,
        "location_city": location_city,
        "budget_range_raw": budget_range_raw,
        "budget_min": budget_min,
        "budget_max": budget_max,
    }

    return {
        "q": q.strip(),
        "price_min": budget_min,
        "price_max": budget_max,
        "raw_mapping": mapping_echo,
    }

# ---------------------------
# Routes
# ---------------------------

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/suggest", response_model=SuggestionResponse)
def suggest(req: SuggestionRequest):
    try:
        form = req.payload or {}
        built = build_query_from_form(form)
        items = call_ebay_browse_search(
            q=built["q"],
            price_min=built["price_min"],
            price_max=built["price_max"],
            limit=12,
        )
        return SuggestionResponse(ok=True, query=built, items=items)
    except HTTPException as he:
        raise he
    except Exception as e:
        logging.exception("suggest error")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ghl/suggest", response_model=SuggestionResponse)
async def ghl_suggest(request: Request):
    body = await request.json()
    try:
        built = build_query_from_form(body)
        items = call_ebay_browse_search(
            q=built["q"],
            price_min=built["price_min"],
            price_max=built["price_max"],
            limit=12,
        )
        return SuggestionResponse(ok=True, query=built, items=items, note="ghl webhook")
    except HTTPException as he:
        raise he
    except Exception as e:
        logging.exception("ghl_suggest error")
        raise HTTPException(status_code=500, detail=str(e))
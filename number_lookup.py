import asyncio
import re
import requests
import config

def _get_json(url, params, headers=None):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code >= 400:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None

def _numverify(number):
    if not config.NUMVERIFY_KEY:
        return None
    # Numverify commonly expects number without leading plus.
    data = _get_json(
        "http://apilayer.net/api/validate",
        {"access_key": config.NUMVERIFY_KEY, "number": number.lstrip("+")}
    )
    if not isinstance(data, dict) or not data.get("valid"):
        return None
    return {
        "valid": data.get("valid"),
        "country": data.get("country_name"),
        "country_code": data.get("country_code"),
        "carrier": data.get("carrier"),
        "line_type": data.get("line_type"),
        "international_format": data.get("international_format"),
    }

def _abstract(number):
    if not config.ABSTRACT_KEY:
        return None
    data = _get_json(
        "https://phonevalidation.abstractapi.com/v1/",
        {"api_key": config.ABSTRACT_KEY, "phone": number}
    )
    if not isinstance(data, dict):
        return None
    # Return only general phone metadata, not personal identity or tracking fields.
    country = data.get("country") or {}
    location = country.get("name") if isinstance(country, dict) else None
    carrier = data.get("carrier") or {}
    carrier_name = carrier.get("name") if isinstance(carrier, dict) else carrier
    line_type = carrier.get("type") if isinstance(carrier, dict) else None
    return {
        "valid": data.get("valid"),
        "country": location,
        "country_code": country.get("code") if isinstance(country, dict) else None,
        "carrier": carrier_name,
        "line_type": line_type,
        "international_format": data.get("format", {}).get("international")
            if isinstance(data.get("format"), dict) else None,
    }

def _custom_local(number):
    # The uploaded custom API is a Flask web scraper requiring a live endpoint.
    # It cannot be called as a local function without refactoring its main.py.
    # Keep it disabled by default; only safe general metadata fields are allowed.
    if not config.CUSTOM_API_ENABLED:
        return None
    if not config.CUSTOM_API_URL:
        return {"note": "Custom API enabled but CUSTOM_API_URL is empty; this API requires a reachable endpoint."}
    data = _get_json(config.CUSTOM_API_URL, {"number": number})
    if not isinstance(data, dict):
        return None
    # Allowlist only basic phone metadata. Do not relay personal or tracking fields.
    allowed = ("Country", "Country Code", "Connection", "SIM Card", "Mobile State", "Carrier",
               "Line Type", "country", "country_code", "carrier", "line_type", "valid")
    return {k: data[k] for k in allowed if k in data and data[k] not in (None, "", "N/A")}

async def lookup_number(number):
    # requests is kept in worker threads so the Telegram event loop is not blocked.
    tasks = {}
    if config.NUMVERIFY_KEY:
        tasks["Numverify"] = asyncio.to_thread(_numverify, number)
    if config.ABSTRACT_KEY:
        tasks["Abstract"] = asyncio.to_thread(_abstract, number)
    if config.CUSTOM_API_ENABLED:
        tasks["Custom Num Info API"] = asyncio.to_thread(_custom_local, number)
    if not tasks:
        return {}
    names = list(tasks)
    values = await asyncio.gather(*(tasks[n] for n in names), return_exceptions=True)
    out = {}
    for name, value in zip(names, values):
        if isinstance(value, Exception):
            out[name] = None
        else:
            out[name] = value
    return out

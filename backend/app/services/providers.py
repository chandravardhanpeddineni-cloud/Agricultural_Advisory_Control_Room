import csv
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError


class WeatherPayload(BaseModel):
    summary: str = Field(min_length=1, max_length=1500)
    temperature_c: float | None = None
    humidity_percent: float | None = Field(default=None, ge=0, le=100)
    rainfall_next_48h_mm: float | None = Field(default=None, ge=0)
    precipitation_probability_percent: float | None = Field(default=None, ge=0, le=100)
    source: str = "Configured weather provider"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


MARKET_DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "AgriAI.csv"
MARKET_CSV_COLUMNS = {
    "state": "State",
    "district": "District",
    "market": "Market",
    "commodity": "Commodity",
    "variety": "Variety",
    "grade": "Grade",
    "date": "Arrival_Date",
    "minimum": "Min_x0020_Price",
    "maximum": "Max_x0020_Price",
    "modal": "Modal_x0020_Price",
}


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


@lru_cache(maxsize=4)
def _read_market_csv(path: str, modified_ns: int) -> tuple[dict, ...]:
    del modified_ns  # Included in the cache key so replacing the CSV reloads it.
    records = []
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = set(MARKET_CSV_COLUMNS.values())
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Market CSV is missing required AGMARKNET columns")
        for row in reader:
            try:
                records.append(
                    {
                        "state": row["State"].strip(),
                        "district": row["District"].strip(),
                        "market": row["Market"].strip(),
                        "commodity": row["Commodity"].strip(),
                        "variety": row["Variety"].strip(),
                        "grade": row["Grade"].strip(),
                        "date": datetime.strptime(row["Arrival_Date"].strip(), "%d/%m/%Y")
                        .replace(tzinfo=timezone.utc),
                        "minimum": float(row["Min_x0020_Price"]),
                        "maximum": float(row["Max_x0020_Price"]),
                        "modal": float(row["Modal_x0020_Price"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    return tuple(records)


def _crop_aliases(crop: str) -> set[str]:
    normalized = _normalize(crop)
    aliases = {normalized}
    if normalized in {"rice", "paddy", "paddy common", "paddy dhan common"}:
        aliases.update({"rice", "paddy common", "paddy dhan common"})
    return aliases


def _location_score(location: str, record: dict) -> int:
    query = _normalize(location)
    if not query:
        return 0
    fields = [(record["state"], 10), (record["district"], 20), (record["market"], 30)]
    best = 0
    for raw, weight in fields:
        value = _normalize(raw)
        if value and (value in query or query in value):
            best = max(best, weight)
        elif value and len(query) >= 5 and len(value) >= 5:
            similarity = SequenceMatcher(None, query, value).ratio()
            if similarity >= 0.72:
                best = max(best, int(weight * similarity))
        query_tokens, value_tokens = set(query.split()), set(value.split())
        overlap = query_tokens & value_tokens
        if overlap:
            best = max(best, int(weight * len(overlap) / len(value_tokens)))
    return best


def _select_market_records(
    records: list[dict] | tuple[dict, ...], crop: str, location: str
) -> tuple[list[dict], str]:
    aliases = _crop_aliases(crop)
    candidates = [record for record in records if _normalize(record["commodity"]) in aliases]
    if not candidates:
        return [], ""
    latest_date = max(record["date"] for record in candidates)
    candidates = [record for record in candidates if record["date"] == latest_date]
    scores = [(record, _location_score(location, record)) for record in candidates]
    best_score = max(score for _, score in scores)
    if best_score:
        selected = [record for record, score in scores if score == best_score]
        return selected, "location"

    # When no local market is present, use a transparent all-India snapshot.
    exact = [record for record in candidates if _normalize(record["commodity"]) == _normalize(crop)]
    return exact or candidates, "national"


def _local_market_records(crop: str, location: str) -> tuple[list[dict], str]:
    stat = MARKET_DATA_FILE.stat()
    records = _read_market_csv(str(MARKET_DATA_FILE), stat.st_mtime_ns)
    return _select_market_records(records, crop, location)


def _market_api_record(row: dict) -> dict | None:
    try:
        date_value = row.get("arrival_date", row.get("Arrival_Date"))
        try:
            price_date = datetime.strptime(str(date_value), "%d/%m/%Y")
        except ValueError:
            price_date = datetime.fromisoformat(str(date_value))
        return {
            "state": str(row.get("state", row.get("State", ""))).strip(),
            "district": str(row.get("district", row.get("District", ""))).strip(),
            "market": str(row.get("market", row.get("Market", ""))).strip(),
            "commodity": str(row.get("commodity", row.get("Commodity", ""))).strip(),
            "variety": str(row.get("variety", row.get("Variety", ""))).strip(),
            "grade": str(row.get("grade", row.get("Grade", ""))).strip(),
            "date": price_date.replace(tzinfo=timezone.utc),
            "minimum": float(row.get("min_price", row.get("Min_x0020_Price"))),
            "maximum": float(row.get("max_price", row.get("Max_x0020_Price"))),
            "modal": float(row.get("modal_price", row.get("Modal_x0020_Price"))),
        }
    except (TypeError, ValueError):
        return None


async def _data_gov_market_records(settings, crop: str) -> list[dict]:
    if not settings.market_api_url or not settings.market_api_key:
        return []
    parsed = urlparse(settings.market_api_url)
    if parsed.hostname != "api.data.gov.in" or not parsed.path.startswith("/resource/"):
        return []
    params = {
        "api-key": settings.market_api_key.get_secret_value(),
        "format": "json",
        "offset": 0,
        "limit": 1000,
        "filters[commodity]": crop.strip().title(),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        response = await client.get(
            settings.market_api_url,
            params=params,
            headers={"accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise ValueError("data.gov.in response did not contain a records list")
    return [record for row in rows if (record := _market_api_record(row)) is not None]


async def _post_json(url: str, api_key: str | None, body: dict) -> dict:
    headers = {"accept": "application/json", "content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        return response.json()


async def _openweather_payload(url: str, api_key: str, location: str) -> dict:
    """Fetch either configured current conditions or a 5-day forecast."""
    parsed = urlparse(url)
    endpoint = url.rstrip("/")
    if parsed.path in {"", "/"}:
        endpoint = f"{parsed.scheme}://{parsed.netloc}/data/2.5/forecast"
    async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
        params = {"q": location, "appid": api_key, "units": "metric"}
        response = await client.get(
            endpoint,
            params=params,
            headers={"accept": "application/json"},
        )
        if response.status_code == 404:
            # Rural village names often are not recognized by the weather API's
            # city-name shortcut. Resolve them once, then retry with coordinates.
            geocode = await client.get(
                f"{parsed.scheme}://{parsed.netloc}/geo/1.0/direct",
                params={"q": location, "appid": api_key, "limit": 1},
                headers={"accept": "application/json"},
            )
            geocode.raise_for_status()
            places = geocode.json()
            if places:
                params = {
                    "lat": places[0]["lat"],
                    "lon": places[0]["lon"],
                    "appid": api_key,
                    "units": "metric",
                }
                response = await client.get(
                    endpoint, params=params, headers={"accept": "application/json"}
                )
        response.raise_for_status()
        raw = response.json()

    if parsed.path.rstrip("/").endswith("/weather"):
        current = raw.get("main", {})
        conditions = raw.get("weather", [])
        if not isinstance(current, dict) or current.get("temp") is None or not conditions:
            raise ValueError("OpenWeather response did not contain current conditions")
        place = raw.get("name") or location
        description = conditions[0].get("description", "conditions reported by provider")
        observed_at = datetime.fromtimestamp(raw.get("dt", datetime.now().timestamp()), timezone.utc)
        return {
            "summary": (
                f"Current conditions in {place}: {description}; "
                "this endpoint does not provide a forecast."
            ),
            "temperature_c": float(current["temp"]),
            "humidity_percent": current.get("humidity"),
            "rainfall_next_48h_mm": None,
            "precipitation_probability_percent": None,
            "source": "OpenWeather current weather observation",
            "retrieved_at": observed_at.isoformat(),
        }

    entries = raw.get("list")
    if not isinstance(entries, list) or not entries:
        raise ValueError("OpenWeather response did not contain forecast entries")
    now = datetime.now(timezone.utc)
    horizon = now.timestamp() + 48 * 60 * 60
    upcoming = [
        item
        for item in entries
        if isinstance(item, dict)
        and isinstance(item.get("dt"), (int, float))
        and now.timestamp() <= item["dt"] <= horizon
    ]
    if not upcoming:
        raise ValueError("OpenWeather response contained no forecast for the next 48 hours")

    rainfall = sum(
        max(0.0, float(item.get("rain", {}).get("3h", 0) or 0))
        for item in upcoming
        if isinstance(item.get("rain", {}), dict)
    )
    probabilities = [
        float(item["pop"]) * 100 for item in upcoming if item.get("pop") is not None
    ]
    temperatures = [
        float(item["main"]["temp"])
        for item in upcoming
        if isinstance(item.get("main"), dict) and item["main"].get("temp") is not None
    ]
    humidity = [
        float(item["main"]["humidity"])
        for item in upcoming
        if isinstance(item.get("main"), dict) and item["main"].get("humidity") is not None
    ]
    descriptions = [
        item["weather"][0]["description"]
        for item in upcoming
        if isinstance(item.get("weather"), list)
        and item["weather"]
        and item["weather"][0].get("description")
    ]
    place = raw.get("city", {}).get("name") or location
    summary = f"Forecast for {place} over the next 48 hours: "
    summary += ", ".join(dict.fromkeys(descriptions[:4])) or "conditions reported by provider"
    summary += f". Forecast rainfall: {rainfall:.1f} mm."
    return {
        "summary": summary,
        "temperature_c": max(temperatures) if temperatures else None,
        "humidity_percent": max(humidity) if humidity else None,
        "rainfall_next_48h_mm": rainfall,
        "precipitation_probability_percent": max(probabilities) if probabilities else None,
        "source": "OpenWeather 5 day / 3 hour forecast",
        "retrieved_at": now.isoformat(),
    }


async def fetch_weather(settings, farmer: dict) -> dict:
    if not settings.weather_api_url:
        return {
            "available": False,
            "status": "unavailable",
            "reason_unavailable": "Weather provider is not configured. No forecast was generated.",
        }
    try:
        parsed = urlparse(settings.weather_api_url)
        api_key = settings.weather_api_key.get_secret_value() if settings.weather_api_key else None
        if parsed.hostname == "api.openweathermap.org":
            if not api_key:
                return {
                    "available": False,
                    "status": "unavailable",
                    "reason_unavailable": "OpenWeather is selected, but WEATHER_API_KEY is not configured.",
                }
            raw = await _openweather_payload(
                settings.weather_api_url, api_key, farmer["location"]
            )
        else:
            raw = await _post_json(
                settings.weather_api_url,
                api_key,
                {
                    "location": farmer["location"],
                    "crop": farmer["crop"],
                    "stage": farmer["crop_stage"],
                },
            )
        payload = WeatherPayload.model_validate(raw)
        return {"available": True, "status": "available", **payload.model_dump(mode="json")}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        try:
            provider_message = exc.response.json().get("message")
        except (ValueError, AttributeError):
            provider_message = None
        reason = f"Weather provider returned HTTP {status_code}"
        if provider_message:
            reason += f": {str(provider_message)[:160]}"
        return {
            "available": False,
            "status": "failed",
            "reason_unavailable": reason,
        }
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        return {
            "available": False,
            "status": "failed",
            "reason_unavailable": (
                f"Weather provider did not return usable data ({type(exc).__name__}). "
                "Check the configured API endpoint and key."
            ),
        }


async def fetch_market(settings, farmer: dict) -> dict:
    source = "AgriAI.csv (user-provided market snapshot)"
    scope = "national"
    records = []
    live_api_error = None
    try:
        api_records = await _data_gov_market_records(settings, farmer["crop"])
        if api_records:
            records, scope = _select_market_records(
                api_records, farmer["crop"], farmer["location"]
            )
            if records:
                source = "data.gov.in AGMARKNET live API"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        live_api_error = type(exc).__name__

    if not records:
        try:
            records, scope = _local_market_records(farmer["crop"], farmer["location"])
        except (OSError, ValueError, ValidationError) as exc:
            return {
                "available": False,
                "status": "unavailable",
                "crop": farmer["crop"],
                "reason": (
                    f"No matching live market record was available and the local snapshot "
                    f"could not be read ({type(exc).__name__})."
                ),
            }
        if not records:
            return {
                "available": False,
                "status": "unavailable",
                "crop": farmer["crop"],
                "reason": f"No live or CSV market price was found for {farmer['crop']}; no price was inferred.",
            }
    markets = sorted({record["market"] for record in records})
    districts = sorted({record["district"] for record in records})
    states = sorted({record["state"] for record in records})
    modal_prices = [record["modal"] for record in records]
    if scope == "location" and len(markets) == 1:
        market_name = markets[0]
    elif scope == "location" and len(districts) == 1:
        market_name = f"{districts[0]} district snapshot ({len(markets)} markets)"
    elif scope == "location" and len(states) == 1:
        market_name = f"{states[0]} snapshot ({len(markets)} markets)"
    else:
        market_name = f"India snapshot ({len(markets)} markets)"
    latest_date = max(record["date"] for record in records)
    retrieved_at = latest_date.isoformat()
    return {
        "available": True,
        "status": "available",
        "crop": farmer["crop"],
        "market": market_name,
        "current_price": median(modal_prices),
        "minimum_price": min(record["minimum"] for record in records),
        "maximum_price": max(record["maximum"] for record in records),
        "unit": "INR/quintal",
        "trend": "insufficient_data",
        "source": source,
        "retrieved_at": retrieved_at,
        "price_date": latest_date.date().isoformat(),
        "record_count": len(records),
        "location_scope": scope,
        "live_api_fallback": bool(live_api_error and source.startswith("AgriAI.csv")),
        "history_points": [],
    }

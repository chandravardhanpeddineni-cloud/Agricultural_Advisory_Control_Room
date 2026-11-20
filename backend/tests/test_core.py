from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models.schemas import AdvisoryRequest, FarmerProfileInput
from app.services import providers


def test_password_hashing_and_token_round_trip():
    password = "long-secure-example-passphrase"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed)
    assert not verify_password("incorrect-passphrase", hashed)
    settings = Settings(secret_key="test-key-with-more-than-thirty-two-characters")
    token, token_id, expires = create_access_token("farmer@example.test", settings)
    claims = decode_access_token(token, settings)
    assert claims["sub"] == "farmer@example.test"
    assert claims["jti"] == token_id
    assert expires > datetime.now(timezone.utc)


def test_advisory_input_rejects_invalid_stage_and_area():
    base = {"crop": "Rice", "location": "Mysuru", "crop_stage": "Flowering", "land_area": 1.2}
    assert AdvisoryRequest(**base).land_area == 1.2
    with pytest.raises(ValueError):
        AdvisoryRequest(**{**base, "crop_stage": "Unknown"})
    with pytest.raises(ValueError):
        AdvisoryRequest(**{**base, "land_area": 0})


def test_profile_location_is_required():
    assert FarmerProfileInput(location="Mysuru").preferred_crops == []
    with pytest.raises(ValueError):
        FarmerProfileInput(location=" ")


@pytest.mark.asyncio
async def test_weather_provider_absent_does_not_make_up_forecast():
    settings = Settings(weather_api_url=None)
    result = await providers.fetch_weather(
        settings, {"location": "Mysuru", "crop": "Rice", "crop_stage": "Flowering"}
    )
    assert result["available"] is False
    assert "not configured" in result["reason_unavailable"]
    assert "temperature_c" not in result


@pytest.mark.asyncio
async def test_market_trend_uses_provider_history(monkeypatch):
    async def fake_post(url, key, body):
        return {
            "market": "Example Mandi",
            "current_price": 2500,
            "unit": "INR/quintal",
            "source": "provider",
            "history": [
                {"date": "2026-11-10T00:00:00Z", "price": 2000},
                {"date": "2026-11-17T00:00:00Z", "price": 2200},
            ],
        }

    monkeypatch.setattr(providers, "_post_json", fake_post)
    settings = Settings(market_api_url="https://provider.invalid")
    result = await providers.fetch_market(settings, {"location": "Mysuru", "crop": "Rice"})
    assert result["available"] is True
    assert result["trend"] == "increasing"
    assert result["current_price"] == 2500

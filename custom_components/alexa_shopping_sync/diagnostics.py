"""Diagnostics support for Alexa Shopping List Sync."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import AlexaShoppingCoordinator

# Keys that must be redacted from diagnostics
REDACT_KEYS = {
    "password",
    "otp_secret",
    "cookie",
    "token",
    "csrf",
    "session_id",
    "access_token",
    "refresh_token",
}


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys from a dict."""
    redacted = {}
    for key, value in data.items():
        if any(s in key.lower() for s in REDACT_KEYS):
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = _redact_dict(value)
        elif isinstance(value, list):
            redacted[key] = [
                _redact_dict(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            redacted[key] = value
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: AlexaShoppingCoordinator = entry.runtime_data

    diag_data = coordinator.get_diagnostics_data()

    # Redact config entry data
    entry_data = _redact_dict(dict(entry.data))
    entry_options = _redact_dict(dict(entry.options))

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": entry_data,
            "options": entry_options,
        },
        "coordinator": diag_data,
    }

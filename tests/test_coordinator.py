"""Tests for AlexaShoppingCoordinator runtime checks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.alexa_shopping_sync.const import (
    CONF_TARGET_LIST,
    DOMAIN,
    TARGET_SHOPPING_LIST,
)
from custom_components.alexa_shopping_sync.coordinator import AlexaShoppingCoordinator

ENTRY_ID = "entry-xyz"
TARGET_ENTITY = "todo.cookidoo_extras"


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.data = {
        CONF_TARGET_LIST: TARGET_ENTITY,
        "_sync_enabled": True,
        # Auth fields the coordinator __init__ reads via entry.data.get/[]
        "email": "user@example.com",
        "password": "pw",
        "otp_secret": "x" * 52,
    }
    entry.options = {}
    return entry


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.bus.async_listen = MagicMock()
    return hass


def _make_coordinator() -> AlexaShoppingCoordinator:
    """Build a coordinator with the parent DataUpdateCoordinator init bypassed.

    The runtime check we're exercising doesn't need polling/auth wiring —
    only the bridge, entry, and hass references.
    """
    coord = AlexaShoppingCoordinator.__new__(AlexaShoppingCoordinator)
    coord.hass = _make_hass()
    coord._entry = _make_entry()
    coord._target_list = TARGET_ENTITY
    coord._ha_bridge = MagicMock()
    coord._target_list_unavailable_logged = False
    return coord


@pytest.mark.asyncio
async def test_target_available_clears_issue():
    """When the bridge reports available, the repair issue is removed."""
    coord = _make_coordinator()
    coord._ha_bridge.async_validate_available = AsyncMock(return_value=True)

    with (
        patch(
            "custom_components.alexa_shopping_sync.coordinator.ir.async_create_issue"
        ) as create_issue,
        patch(
            "custom_components.alexa_shopping_sync.coordinator.ir.async_delete_issue"
        ) as delete_issue,
    ):
        result = await coord._async_target_list_available()

    assert result is True
    create_issue.assert_not_called()
    delete_issue.assert_called_once_with(coord.hass, DOMAIN, f"target_list_missing_{ENTRY_ID}")


@pytest.mark.asyncio
async def test_target_missing_creates_issue_and_logs_once(caplog):
    """First missing detection: warn + create issue. Second: silent, issue stays."""
    coord = _make_coordinator()
    coord._ha_bridge.async_validate_available = AsyncMock(return_value=False)

    with (
        patch(
            "custom_components.alexa_shopping_sync.coordinator.ir.async_create_issue"
        ) as create_issue,
        patch(
            "custom_components.alexa_shopping_sync.coordinator.ir.async_delete_issue"
        ) as delete_issue,
        caplog.at_level("WARNING"),
    ):
        assert await coord._async_target_list_available() is False
        # Second call within the same outage — no second warning, issue
        # re-asserted (idempotent in HA's registry).
        assert await coord._async_target_list_available() is False

    assert create_issue.call_count == 2
    create_issue.assert_called_with(
        coord.hass,
        DOMAIN,
        f"target_list_missing_{ENTRY_ID}",
        is_fixable=False,
        severity=create_issue.call_args.kwargs["severity"],
        translation_key="target_list_missing",
        translation_placeholders={"entity_id": TARGET_ENTITY},
    )
    delete_issue.assert_not_called()
    warning_count = sum(
        1 for r in caplog.records if r.levelname == "WARNING" and "unavailable" in r.message
    )
    assert warning_count == 1


@pytest.mark.asyncio
async def test_target_recovery_clears_issue_and_logs_info(caplog):
    """Going from missing → available logs the recovery and clears the issue."""
    coord = _make_coordinator()
    coord._ha_bridge.async_validate_available = AsyncMock(side_effect=[False, True])

    with (
        patch("custom_components.alexa_shopping_sync.coordinator.ir.async_create_issue"),
        patch(
            "custom_components.alexa_shopping_sync.coordinator.ir.async_delete_issue"
        ) as delete_issue,
        caplog.at_level("INFO"),
    ):
        assert await coord._async_target_list_available() is False
        assert await coord._async_target_list_available() is True

    delete_issue.assert_called_once_with(coord.hass, DOMAIN, f"target_list_missing_{ENTRY_ID}")
    info_count = sum(
        1 for r in caplog.records if r.levelname == "INFO" and "available again" in r.message
    )
    assert info_count == 1
    assert coord._target_list_unavailable_logged is False


@pytest.mark.asyncio
async def test_target_check_without_bridge_returns_false():
    """Defensive: no bridge wired → check fails closed."""
    coord = _make_coordinator()
    coord._ha_bridge = None

    with (
        patch("custom_components.alexa_shopping_sync.coordinator.ir.async_create_issue"),
        patch("custom_components.alexa_shopping_sync.coordinator.ir.async_delete_issue"),
    ):
        assert await coord._async_target_list_available() is False


@pytest.mark.asyncio
async def test_shopping_list_target_uses_shopping_list_missing_issue():
    """Built-in shopping list uses the shopping_list_missing key and ID.

    Same wording the user already sees from the setup-time check — otherwise
    the runtime message would (incorrectly) refer to a "to-do entity" for
    the built-in list.
    """
    coord = _make_coordinator()
    coord._target_list = TARGET_SHOPPING_LIST
    coord._entry.data = {**coord._entry.data, CONF_TARGET_LIST: TARGET_SHOPPING_LIST}
    coord._ha_bridge.async_validate_available = AsyncMock(return_value=False)

    with (
        patch(
            "custom_components.alexa_shopping_sync.coordinator.ir.async_create_issue"
        ) as create_issue,
        patch("custom_components.alexa_shopping_sync.coordinator.ir.async_delete_issue"),
    ):
        assert await coord._async_target_list_available() is False

    create_issue.assert_called_once()
    args, kwargs = create_issue.call_args
    # Positional: hass, DOMAIN, issue_id
    assert args[2] == f"shopping_list_missing_{ENTRY_ID}"
    assert kwargs["translation_key"] == "shopping_list_missing"
    # shopping_list_missing has no placeholders — must not be passed
    assert "translation_placeholders" not in kwargs

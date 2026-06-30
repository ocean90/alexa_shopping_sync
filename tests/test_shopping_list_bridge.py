"""Tests for ShoppingListBridge."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.alexa_shopping_sync.exceptions import ShoppingListMissingError
from custom_components.alexa_shopping_sync.shopping_list_bridge import (
    SHOPPING_LIST_DOMAIN,
    ShoppingListBridge,
)


def _make_hass(*, loaded_entries=None, hass_data=None) -> MagicMock:
    """Build a mock hass with configurable shopping_list discovery."""
    hass = MagicMock()
    hass.data = hass_data or {}
    hass.config_entries.async_loaded_entries = MagicMock(return_value=loaded_entries or [])
    return hass


@pytest.fixture
def shopping_data() -> MagicMock:
    """A stand-in for HA's ShoppingData instance."""
    data = MagicMock()
    data.items = [
        {"id": "id-1", "name": "Milk", "complete": False},
        {"id": "id-2", "name": "Bread", "complete": True},
    ]
    return data


@pytest.mark.asyncio
async def test_validate_available_via_config_entry(shopping_data):
    """Modern HA stores ShoppingData on the loaded config entry's runtime_data."""
    entry = MagicMock(runtime_data=shopping_data)
    hass = _make_hass(loaded_entries=[entry])
    bridge = ShoppingListBridge(hass)

    assert await bridge.async_validate_available() is True
    hass.config_entries.async_loaded_entries.assert_called_once_with(SHOPPING_LIST_DOMAIN)


@pytest.mark.asyncio
async def test_validate_available_via_hass_data_fallback(shopping_data):
    """Older cores exposed ShoppingData via hass.data — keep supporting it."""
    hass = _make_hass(hass_data={SHOPPING_LIST_DOMAIN: shopping_data})
    bridge = ShoppingListBridge(hass)

    assert await bridge.async_validate_available() is True


@pytest.mark.asyncio
async def test_validate_unavailable_when_not_configured():
    """No loaded entry and no hass.data entry → list is unavailable."""
    hass = _make_hass()
    bridge = ShoppingListBridge(hass)

    assert await bridge.async_validate_available() is False


@pytest.mark.asyncio
async def test_entry_without_runtime_data_falls_back(shopping_data):
    """A loaded entry whose runtime_data is not yet set must not mask the data.

    Guards against AttributeError-style breakage: when the entry exists but
    runtime_data is None, we fall through to the hass.data lookup.
    """
    entry = MagicMock(runtime_data=None)
    hass = _make_hass(
        loaded_entries=[entry],
        hass_data={SHOPPING_LIST_DOMAIN: shopping_data},
    )
    bridge = ShoppingListBridge(hass)

    assert await bridge.async_validate_available() is True


@pytest.mark.asyncio
async def test_get_items_uses_config_entry_runtime_data(shopping_data):
    """Items are read from the resolved ShoppingData instance."""
    entry = MagicMock(runtime_data=shopping_data)
    hass = _make_hass(loaded_entries=[entry])
    bridge = ShoppingListBridge(hass)

    items = await bridge.async_get_items()

    assert len(items) == 2
    assert items[0].item_id == "id-1"
    assert items[0].name == "Milk"
    assert items[0].complete is False
    assert items[1].item_id == "id-2"
    assert items[1].complete is True


@pytest.mark.asyncio
async def test_get_items_raises_when_missing():
    """Reaching the data when none is configured raises a clear error."""
    hass = _make_hass()
    bridge = ShoppingListBridge(hass)

    with pytest.raises(ShoppingListMissingError):
        await bridge.async_get_items()

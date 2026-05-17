"""Tests for async_setup_entry — target list availability handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.alexa_shopping_sync import async_setup_entry
from custom_components.alexa_shopping_sync.const import CONF_TARGET_LIST


def _make_entry(target_list: str) -> MagicMock:
    entry = MagicMock()
    entry.data = {CONF_TARGET_LIST: target_list}
    entry.runtime_data = None
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock()
    return entry


def _make_hass(core_state: CoreState, states: dict[str, object]) -> MagicMock:
    hass = MagicMock()
    hass.state = core_state
    hass.config.components = set()
    hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    return hass


@pytest.mark.asyncio
async def test_target_missing_during_startup_raises_not_ready_without_issue():
    """While HA is still starting, a missing target should retry silently."""
    hass = _make_hass(CoreState.starting, states={})
    entry = _make_entry("todo.cookidoo_extras")

    with (
        patch("custom_components.alexa_shopping_sync.ir.async_create_issue") as create_issue,
        patch("custom_components.alexa_shopping_sync.ir.async_delete_issue") as delete_issue,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    create_issue.assert_not_called()
    delete_issue.assert_not_called()


@pytest.mark.asyncio
async def test_target_missing_after_startup_creates_issue_and_raises_not_ready():
    """Once HA is running, a missing target is a real problem — surface it."""
    hass = _make_hass(CoreState.running, states={})
    entry = _make_entry("todo.cookidoo_extras")

    with (
        patch("custom_components.alexa_shopping_sync.ir.async_create_issue") as create_issue,
        patch("custom_components.alexa_shopping_sync.ir.async_delete_issue") as delete_issue,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    create_issue.assert_called_once()
    args, kwargs = create_issue.call_args
    assert args[2] == "target_list_missing"
    assert kwargs["translation_placeholders"] == {"entity_id": "todo.cookidoo_extras"}
    delete_issue.assert_not_called()


@pytest.mark.asyncio
async def test_target_present_proceeds_and_clears_stale_issue():
    """Target available -> setup proceeds and any stale issue is cleared."""
    hass = _make_hass(
        CoreState.running,
        states={"todo.cookidoo_extras": MagicMock()},
    )
    entry = _make_entry("todo.cookidoo_extras")

    coordinator = MagicMock()
    coordinator.async_initialize = AsyncMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_register_services = AsyncMock()
    coordinator.async_start_event_listener = MagicMock()

    with (
        patch(
            "custom_components.alexa_shopping_sync.AlexaShoppingCoordinator",
            return_value=coordinator,
        ),
        patch("custom_components.alexa_shopping_sync.ir.async_create_issue") as create_issue,
        patch("custom_components.alexa_shopping_sync.ir.async_delete_issue") as delete_issue,
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    create_issue.assert_not_called()
    # Both stale issues should be cleared on a successful setup.
    delete_keys = {call.args[2] for call in delete_issue.call_args_list}
    assert delete_keys == {"target_list_missing", "shopping_list_missing"}

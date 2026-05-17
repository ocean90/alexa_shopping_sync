"""Tests for async_setup_entry — target list availability handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.alexa_shopping_sync import async_setup_entry
from custom_components.alexa_shopping_sync.const import CONF_TARGET_LIST


def _make_entry(target_list: str, entry_id: str = "entry-abc") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
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
async def test_target_missing_after_startup_creates_issue_and_raises_not_ready(caplog):
    """Once HA is running, a missing target is a real problem — surface it."""
    hass = _make_hass(CoreState.running, states={})
    entry = _make_entry("todo.cookidoo_extras")

    issue_registry = MagicMock()
    issue_registry.async_get_issue = MagicMock(return_value=None)

    with (
        patch(
            "custom_components.alexa_shopping_sync.ir.async_get",
            return_value=issue_registry,
        ),
        patch("custom_components.alexa_shopping_sync.ir.async_create_issue") as create_issue,
        patch("custom_components.alexa_shopping_sync.ir.async_delete_issue") as delete_issue,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    create_issue.assert_called_once()
    args, kwargs = create_issue.call_args
    # Issue ID is scoped per entry so multi-instance setups don't collide.
    assert args[2] == "target_list_missing_entry-abc"
    assert kwargs["translation_key"] == "target_list_missing"
    assert kwargs["translation_placeholders"] == {"entity_id": "todo.cookidoo_extras"}
    delete_issue.assert_not_called()
    # First failure -> exactly one ERROR log
    error_records = [
        r for r in caplog.records if r.levelname == "ERROR" and "Target todo entity" in r.message
    ]
    assert len(error_records) == 1


@pytest.mark.asyncio
async def test_target_missing_retries_do_not_spam_log(caplog):
    """ConfigEntryNotReady triggers retries; only the first one should log."""
    hass = _make_hass(CoreState.running, states={})
    entry = _make_entry("todo.cookidoo_extras")

    issue_registry = MagicMock()
    # Simulate: first call returns None (no issue yet), subsequent calls
    # return an existing entry (issue was created by the first attempt).
    issue_registry.async_get_issue = MagicMock(side_effect=[None, MagicMock(), MagicMock()])

    with (
        patch(
            "custom_components.alexa_shopping_sync.ir.async_get",
            return_value=issue_registry,
        ),
        patch("custom_components.alexa_shopping_sync.ir.async_create_issue"),
        patch("custom_components.alexa_shopping_sync.ir.async_delete_issue"),
    ):
        for _ in range(3):
            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)

    error_records = [
        r for r in caplog.records if r.levelname == "ERROR" and "Target todo entity" in r.message
    ]
    assert len(error_records) == 1


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
    # Both stale issues for THIS entry should be cleared on a successful setup,
    # using per-entry IDs so other entries' issues aren't affected.
    delete_keys = {call.args[2] for call in delete_issue.call_args_list}
    assert delete_keys == {
        "target_list_missing_entry-abc",
        "shopping_list_missing_entry-abc",
    }


@pytest.mark.asyncio
async def test_multiple_entries_use_distinct_issue_ids():
    """A second entry with a different target must not clear the first's issue."""
    hass = _make_hass(CoreState.running, states={})
    entry_a = _make_entry("todo.cookidoo_extras", entry_id="entry-a")
    entry_b = _make_entry("todo.bring_list", entry_id="entry-b")

    issue_registry = MagicMock()
    issue_registry.async_get_issue = MagicMock(return_value=None)

    with (
        patch(
            "custom_components.alexa_shopping_sync.ir.async_get",
            return_value=issue_registry,
        ),
        patch("custom_components.alexa_shopping_sync.ir.async_create_issue") as create_issue,
        patch("custom_components.alexa_shopping_sync.ir.async_delete_issue"),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry_a)
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry_b)

    created_ids = [call.args[2] for call in create_issue.call_args_list]
    assert created_ids == [
        "target_list_missing_entry-a",
        "target_list_missing_entry-b",
    ]

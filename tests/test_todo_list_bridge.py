"""Tests for TodoListBridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.alexa_shopping_sync.todo_list_bridge import TodoListBridge

ENTITY_ID = "todo.cookidoo_shopping_list"


@pytest.fixture
def mock_hass():
    """Create a mock HomeAssistant instance."""
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=MagicMock(state="3"))
    hass.services.async_call = AsyncMock()
    return hass


@pytest.fixture
def bridge(mock_hass):
    """Create a TodoListBridge."""
    return TodoListBridge(mock_hass, ENTITY_ID)


@pytest.mark.asyncio
async def test_validate_available(bridge, mock_hass):
    """Test entity exists check."""
    assert await bridge.async_validate_available() is True

    mock_hass.states.get.return_value = None
    assert await bridge.async_validate_available() is False


@pytest.mark.asyncio
async def test_get_items(bridge, mock_hass):
    """Test fetching items from todo entity."""
    mock_hass.services.async_call.return_value = {
        ENTITY_ID: {
            "items": [
                {"uid": "uid-1", "summary": "Milk", "status": "needs_action"},
                {"uid": "uid-2", "summary": "Bread", "status": "completed"},
            ]
        }
    }

    items = await bridge.async_get_items()

    assert len(items) == 2
    assert items[0].item_id == "uid-1"
    assert items[0].name == "Milk"
    assert items[0].complete is False
    assert items[1].item_id == "uid-2"
    assert items[1].name == "Bread"
    assert items[1].complete is True

    mock_hass.services.async_call.assert_called_once_with(
        "todo",
        "get_items",
        {"entity_id": ENTITY_ID, "status": ["needs_action", "completed"]},
        blocking=True,
        return_response=True,
    )


@pytest.mark.asyncio
async def test_add_item(bridge, mock_hass):
    """Test adding an item using diff approach."""
    # First call: get_items before add (empty)
    # Second call: add_item (no return)
    # Third call: get_items after add (one item)
    call_count = 0

    async def mock_call(domain, service, data, **kwargs):
        nonlocal call_count
        call_count += 1
        if service == "get_items":
            if call_count == 1:
                return {ENTITY_ID: {"items": []}}
            return {
                ENTITY_ID: {
                    "items": [{"uid": "new-uid", "summary": "Eggs", "status": "needs_action"}]
                }
            }
        return None

    mock_hass.services.async_call = AsyncMock(side_effect=mock_call)

    result = await bridge.async_add_item("Eggs")

    assert result is not None
    assert result.item_id == "new-uid"
    assert result.name == "Eggs"
    assert result.complete is False


@pytest.mark.asyncio
async def test_add_item_complete(bridge, mock_hass):
    """Test adding a completed item triggers update_item afterward."""
    call_count = 0

    async def mock_call(domain, service, data, **kwargs):
        nonlocal call_count
        call_count += 1
        if service == "get_items":
            if call_count == 1:
                return {ENTITY_ID: {"items": []}}
            return {
                ENTITY_ID: {
                    "items": [{"uid": "new-uid", "summary": "Done Item", "status": "needs_action"}]
                }
            }
        return None

    mock_hass.services.async_call = AsyncMock(side_effect=mock_call)

    result = await bridge.async_add_item("Done Item", complete=True)

    assert result is not None
    assert result.complete is True

    # Verify update_item was called with status: completed
    calls = mock_hass.services.async_call.call_args_list
    update_calls = [c for c in calls if c[0][1] == "update_item"]
    assert len(update_calls) == 1
    assert update_calls[0][0][2]["status"] == "completed"


@pytest.mark.asyncio
async def test_update_item(bridge, mock_hass):
    """Test updating a todo item."""
    mock_hass.services.async_call = AsyncMock(
        return_value={
            ENTITY_ID: {
                "items": [{"uid": "uid-1", "summary": "Oat Milk", "status": "needs_action"}]
            }
        }
    )

    result = await bridge.async_update_item("uid-1", name="Oat Milk")

    assert result is not None
    assert result.name == "Oat Milk"

    calls = mock_hass.services.async_call.call_args_list
    update_call = calls[0]
    assert update_call[0][1] == "update_item"
    assert update_call[0][2]["item"] == "uid-1"
    assert update_call[0][2]["rename"] == "Oat Milk"


@pytest.mark.asyncio
async def test_update_item_no_changes(bridge, mock_hass):
    """Test update with no fields returns None."""
    result = await bridge.async_update_item("uid-1")
    assert result is None
    mock_hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_delete_item(bridge, mock_hass):
    """Test deleting a todo item."""
    result = await bridge.async_delete_item("uid-1")

    assert result is True
    mock_hass.services.async_call.assert_called_once_with(
        "todo",
        "remove_item",
        {"entity_id": ENTITY_ID, "item": ["uid-1"]},
        blocking=True,
        return_response=False,
    )


@pytest.mark.asyncio
async def test_delete_item_failure(bridge, mock_hass):
    """Test delete failure returns False."""
    mock_hass.services.async_call.side_effect = Exception("Service error")

    result = await bridge.async_delete_item("uid-1")
    assert result is False


@pytest.mark.asyncio
async def test_clear_completed(bridge, mock_hass):
    """Test clearing completed items."""
    await bridge.async_clear_completed()

    mock_hass.services.async_call.assert_called_once_with(
        "todo",
        "remove_completed_items",
        {"entity_id": ENTITY_ID},
        blocking=True,
        return_response=False,
    )


@pytest.mark.asyncio
async def test_get_items_empty_response(bridge, mock_hass):
    """Test get_items with empty response."""
    mock_hass.services.async_call.return_value = None

    items = await bridge.async_get_items()
    assert items == []


@pytest.mark.asyncio
async def test_get_items_skips_items_without_uid(bridge, mock_hass):
    """Test that items without uid are skipped."""
    mock_hass.services.async_call.return_value = {
        ENTITY_ID: {
            "items": [
                {"uid": "uid-1", "summary": "Valid", "status": "needs_action"},
                {"summary": "No UID", "status": "needs_action"},
                {"uid": "", "summary": "Empty UID", "status": "needs_action"},
            ]
        }
    }

    items = await bridge.async_get_items()
    assert len(items) == 1
    assert items[0].item_id == "uid-1"

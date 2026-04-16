"""Test fixtures for Alexa Shopping List Sync."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.alexa_shopping_sync.amazon_client import AmazonShoppingClient
from custom_components.alexa_shopping_sync.auth import AuthManager
from custom_components.alexa_shopping_sync.const import (
    InitialSyncMode,
    SyncMode,
)
from custom_components.alexa_shopping_sync.models import (
    AlexaShoppingItem,
    HAShoppingItem,
)
from custom_components.alexa_shopping_sync.shopping_list_bridge import (
    ShoppingListBridge,
)
from custom_components.alexa_shopping_sync.sync_engine import SyncEngine


@pytest.fixture
def mock_hass():
    """Create a mock HomeAssistant instance."""
    hass = MagicMock()
    hass.data = {}
    hass.config.components = {"shopping_list"}
    hass.bus.async_listen = MagicMock()
    return hass


@pytest.fixture
def mock_auth_manager(mock_hass):
    """Create a mock AuthManager."""
    auth = MagicMock(spec=AuthManager)
    auth.authenticated = True
    auth.amazon_domain = "amazon.de"
    auth.base_url = "https://www.amazon.de"
    auth.async_get_authenticated_session = AsyncMock()
    return auth


@pytest.fixture
def mock_amazon_client(mock_auth_manager):
    """Create a mock AmazonShoppingClient."""
    client = MagicMock(spec=AmazonShoppingClient)
    client._auth = mock_auth_manager
    client.shopping_list_id = "test-list-123"
    client.async_get_snapshot = AsyncMock(return_value=[])
    client.async_add_item = AsyncMock()
    client.async_update_item = AsyncMock()
    client.async_mark_complete = AsyncMock()
    client.async_delete_item = AsyncMock(return_value=True)
    client.async_discover_shopping_list_id = AsyncMock(return_value="test-list-123")
    client.compute_snapshot_hash = MagicMock(return_value="abc123")
    return client


@pytest.fixture
def mock_ha_bridge(mock_hass):
    """Create a mock ShoppingListBridge."""
    bridge = MagicMock(spec=ShoppingListBridge)
    bridge.async_get_items = AsyncMock(return_value=[])
    bridge.async_add_item = AsyncMock()
    bridge.async_update_item = AsyncMock()
    bridge.async_mark_complete = AsyncMock()
    bridge.async_delete_item = AsyncMock(return_value=True)
    bridge.async_validate_available = AsyncMock(return_value=True)
    bridge.compute_snapshot_hash = MagicMock(return_value="def456")
    return bridge


@pytest.fixture
def sync_engine(mock_hass, mock_amazon_client, mock_ha_bridge):
    """Create a SyncEngine with mocks."""
    engine = SyncEngine(
        hass=mock_hass,
        amazon_client=mock_amazon_client,
        ha_bridge=mock_ha_bridge,
        sync_mode=SyncMode.TWO_WAY,
        initial_sync_mode=InitialSyncMode.MERGE_UNION,
        preserve_duplicates=True,
        mirror_completed=True,
    )
    # Patch storage to avoid file I/O
    engine._store = MagicMock()
    engine._store.async_load = AsyncMock(return_value=None)
    engine._store.async_save = AsyncMock()
    return engine


def make_alexa_item(
    item_id: str = "alexa-1",
    name: str = "Milk",
    complete: bool = False,
) -> AlexaShoppingItem:
    """Create an AlexaShoppingItem for testing."""
    return AlexaShoppingItem(
        item_id=item_id,
        name=name,
        complete=complete,
    )


def make_ha_item(
    item_id: str = "ha-1",
    name: str = "Milk",
    complete: bool = False,
) -> HAShoppingItem:
    """Create an HAShoppingItem for testing."""
    return HAShoppingItem(
        item_id=item_id,
        name=name,
        complete=complete,
    )

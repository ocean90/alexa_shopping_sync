"""Tests for Amazon client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.alexa_shopping_sync.amazon_client import AmazonShoppingClient
from custom_components.alexa_shopping_sync.exceptions import (
    AmazonListNotFoundError,
)
from custom_components.alexa_shopping_sync.models import AlexaShoppingItem


@pytest.fixture
def client(mock_auth_manager):
    """Create an AmazonShoppingClient with mock auth."""
    return AmazonShoppingClient(mock_auth_manager)


# Verified response format from live API (2026-03)
SAMPLE_API_RESPONSE = {
    "YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0=": {
        "listInfo": {
            "listId": "YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0=",
            "listOfListIds": ["YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0="],
            "listName": "",
            "defaultList": True,
            "listType": "SHOPPING_LIST",
            "archivedList": False,
            "customerId": "TESTCUSTOMER",
            "version": 1,
            "createAt": 1634362711219,
            "updateAt": 1634362711219,
        },
        "listItems": [
            {
                "id": "28968840-d612-4baa-b6ae-0228dd9960ac",
                "listId": "YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0=",
                "value": "milch",
                "updatedDateTime": 1753263753213,
                "createdDateTime": 1753263753213,
                "categoryValue": "Dairy",
                "customerId": "TESTCUSTOMER",
                "version": 1,
                "completed": False,
                "itemType": "KEYWORD",
            },
            {
                "id": "3d27eae1-741f-4c53-a5e1-787e18f0a7c3",
                "listId": "YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0=",
                "value": "kaffee",
                "updatedDateTime": 1750829217433,
                "createdDateTime": 1750829217433,
                "categoryValue": "Other",
                "customerId": "TESTCUSTOMER",
                "version": 1,
                "completed": False,
                "itemType": "KEYWORD",
            },
            {
                "id": "952742b7-a03a-4e41-95da-bc1c7e08a672",
                "listId": "YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0=",
                "value": "aufschnitt",
                "updatedDateTime": 1738401759258,
                "createdDateTime": 1738399707424,
                "categoryValue": "Other",
                "customerId": "TESTCUSTOMER",
                "version": 2,
                "completed": True,
                "itemType": "KEYWORD",
            },
        ],
        "listMetadata": [],
    }
}


class TestShoppingListDiscovery:
    """Tests for shopping list ID discovery."""

    @pytest.mark.asyncio
    async def test_discover_by_type(self, client, mock_auth_manager):
        """Find list by SHOPPING_LIST type from real response format."""
        mock_session = AsyncMock()
        mock_auth_manager.async_get_authenticated_session.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_API_RESPONSE
        mock_session.request = AsyncMock(return_value=mock_response)

        list_id = await client.async_discover_shopping_list_id()

        assert list_id == "YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0="
        assert client.shopping_list_id == list_id

    @pytest.mark.asyncio
    async def test_discover_not_found(self, client, mock_auth_manager):
        """Raise when no shopping list found."""
        mock_session = AsyncMock()
        mock_auth_manager.async_get_authenticated_session.return_value = mock_session

        # Empty response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_session.request = AsyncMock(return_value=mock_response)

        with pytest.raises(AmazonListNotFoundError):
            await client.async_discover_shopping_list_id()


class TestGetSnapshot:
    """Tests for snapshot retrieval."""

    @pytest.mark.asyncio
    async def test_parse_real_response(self, client, mock_auth_manager):
        """Parse items from real API response format."""
        mock_session = AsyncMock()
        mock_auth_manager.async_get_authenticated_session.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_API_RESPONSE
        mock_session.request = AsyncMock(return_value=mock_response)

        items = await client.async_get_snapshot()

        assert len(items) == 3
        assert items[0].item_id == "28968840-d612-4baa-b6ae-0228dd9960ac"
        assert items[0].name == "milch"
        assert items[0].complete is False
        assert items[0].version == 1

        assert items[1].name == "kaffee"
        assert items[1].complete is False

        assert items[2].name == "aufschnitt"
        assert items[2].complete is True
        assert items[2].version == 2

    @pytest.mark.asyncio
    async def test_caches_full_item_dicts(self, client, mock_auth_manager):
        """Full item dicts should be cached for update/delete operations."""
        mock_session = AsyncMock()
        mock_auth_manager.async_get_authenticated_session.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_API_RESPONSE
        mock_session.request = AsyncMock(return_value=mock_response)

        await client.async_get_snapshot()

        # Verify item cache populated
        assert "28968840-d612-4baa-b6ae-0228dd9960ac" in client._item_cache
        cached = client._item_cache["28968840-d612-4baa-b6ae-0228dd9960ac"]
        assert cached["value"] == "milch"
        assert cached["listId"] == "YW16bjEuYWNjb3VudC5URVNULVNIT1BQSU5HX0lURU0="


class TestSnapshotHash:
    """Tests for snapshot hashing."""

    def test_same_items_same_hash(self, client):
        items = [
            AlexaShoppingItem("a1", "Milk", False),
            AlexaShoppingItem("a2", "Bread", True),
        ]
        hash1 = client.compute_snapshot_hash(items)
        hash2 = client.compute_snapshot_hash(items)
        assert hash1 == hash2

    def test_different_items_different_hash(self, client):
        items1 = [AlexaShoppingItem("a1", "Milk", False)]
        items2 = [AlexaShoppingItem("a1", "Bread", False)]
        assert client.compute_snapshot_hash(items1) != client.compute_snapshot_hash(items2)

    def test_order_independent(self, client):
        items1 = [
            AlexaShoppingItem("a1", "Milk", False),
            AlexaShoppingItem("a2", "Bread", True),
        ]
        items2 = [
            AlexaShoppingItem("a2", "Bread", True),
            AlexaShoppingItem("a1", "Milk", False),
        ]
        assert client.compute_snapshot_hash(items1) == client.compute_snapshot_hash(items2)

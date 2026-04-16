"""Bridge to Home Assistant's built-in Shopping List integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .exceptions import ShoppingListMissingError
from .ha_list_bridge import compute_snapshot_hash
from .models import HAShoppingItem

_LOGGER = logging.getLogger(__name__)


class ShoppingListBridge:
    """Bridge to HA's built-in shopping list.

    Decision: We access the shopping list through the internal
    ShoppingData component. This is not a public API but is the same
    approach used by other integrations. We wrap it to isolate our
    code from internal changes.

    The official shopping_list integration stores items as:
    {"id": str, "name": str, "complete": bool}
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the bridge."""
        self._hass = hass

    def _get_shopping_data(self) -> Any:
        """Get the ShoppingData instance.

        Raises ShoppingListMissingError if not available.
        """
        shopping_data = self._hass.data.get("shopping_list")
        if shopping_data is None:
            raise ShoppingListMissingError(
                "Home Assistant Shopping List integration is not configured. "
                "Please add it via Settings → Integrations."
            )
        return shopping_data

    async def async_validate_available(self) -> bool:
        """Check if the HA shopping list is available."""
        try:
            self._get_shopping_data()
            return True
        except ShoppingListMissingError:
            return False

    async def async_get_items(self) -> list[HAShoppingItem]:
        """Get all items from the HA shopping list."""
        shopping_data = self._get_shopping_data()
        items = []
        for item in shopping_data.items:
            items.append(
                HAShoppingItem(
                    item_id=item["id"],
                    name=item["name"],
                    complete=item["complete"],
                )
            )
        return items

    async def async_add_item(self, name: str, complete: bool = False) -> HAShoppingItem | None:
        """Add an item to the HA shopping list."""
        shopping_data = self._get_shopping_data()

        # Use the internal async_add method
        item = await shopping_data.async_add(name)

        if item is None:
            _LOGGER.error("Failed to add item '%s' to HA shopping list", name)
            return None

        # If we need to mark it complete, do so
        if complete and item:
            item = await shopping_data.async_update(item["id"], {"complete": True})

        if item:
            return HAShoppingItem(
                item_id=item["id"],
                name=item["name"],
                complete=item["complete"],
            )
        return None

    async def async_update_item(
        self,
        item_id: str,
        name: str | None = None,
        complete: bool | None = None,
    ) -> HAShoppingItem | None:
        """Update an existing HA shopping list item."""
        shopping_data = self._get_shopping_data()

        update_data: dict[str, Any] = {}
        if name is not None:
            update_data["name"] = name
        if complete is not None:
            update_data["complete"] = complete

        if not update_data:
            return None

        item = await shopping_data.async_update(item_id, update_data)

        if item:
            return HAShoppingItem(
                item_id=item["id"],
                name=item["name"],
                complete=item["complete"],
            )
        return None

    async def async_mark_complete(self, item_id: str, complete: bool) -> HAShoppingItem | None:
        """Toggle completion status of an HA item."""
        return await self.async_update_item(item_id, complete=complete)

    async def async_delete_item(self, item_id: str) -> bool:
        """Delete an item from the HA shopping list."""
        shopping_data = self._get_shopping_data()

        try:
            await shopping_data.async_remove(item_id)
            return True
        except Exception as err:
            _LOGGER.error("Failed to delete HA item %s: %s", item_id, err)
            return False

    async def async_clear_completed(self) -> None:
        """Clear all completed items from the HA shopping list."""
        shopping_data = self._get_shopping_data()
        await shopping_data.async_clear_completed()

    def compute_snapshot_hash(self, items: list[HAShoppingItem]) -> str:
        """Compute a hash of the snapshot for change detection."""
        return compute_snapshot_hash(items)

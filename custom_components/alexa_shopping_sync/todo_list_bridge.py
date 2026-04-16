"""Bridge to any Home Assistant todo entity."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .models import HAShoppingItem

_LOGGER = logging.getLogger(__name__)


class TodoListBridge:
    """Bridge to a HA todo entity via service calls.

    Uses the public todo platform services (add_item, update_item,
    remove_item, get_items) so it works with any todo integration
    (Cookidoo, Google Tasks, local, etc.).
    """

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Initialize the bridge."""
        self._hass = hass
        self._entity_id = entity_id

    async def _async_call_service(
        self,
        service: str,
        data: dict[str, Any] | None = None,
        return_response: bool = False,
    ) -> dict[str, Any] | None:
        """Call a todo service."""
        service_data = {"entity_id": self._entity_id}
        if data:
            service_data.update(data)
        return await self._hass.services.async_call(
            "todo",
            service,
            service_data,
            blocking=True,
            return_response=return_response,
        )

    async def async_validate_available(self) -> bool:
        """Check if the todo entity exists."""
        state = self._hass.states.get(self._entity_id)
        return state is not None

    async def async_get_items(self) -> list[HAShoppingItem]:
        """Get all items from the todo entity."""
        response = await self._async_call_service(
            "get_items",
            data={"status": ["needs_action", "completed"]},
            return_response=True,
        )

        if not response:
            return []

        # Response format: {"todo.entity_id": {"items": [...]}}
        entity_data = response.get(self._entity_id, {})
        raw_items: list[dict[str, Any]] = entity_data.get("items", [])

        items: list[HAShoppingItem] = []
        for raw in raw_items:
            uid = raw.get("uid", "")
            summary = raw.get("summary", "")
            status = raw.get("status", "needs_action")
            if uid and summary:
                items.append(
                    HAShoppingItem(
                        item_id=uid,
                        name=summary,
                        complete=status == "completed",
                    )
                )
        return items

    async def async_add_item(
        self, name: str, complete: bool = False
    ) -> HAShoppingItem | None:
        """Add an item to the todo entity.

        Uses diff approach: snapshot before → add → snapshot after → find
        new UID by set difference.
        """
        before = await self.async_get_items()
        before_uids = {item.item_id for item in before}

        await self._async_call_service("add_item", data={"item": name})

        after = await self.async_get_items()
        new_items = [item for item in after if item.item_id not in before_uids]

        if not new_items:
            _LOGGER.warning(
                "Could not identify new item after adding '%s' to %s",
                name,
                self._entity_id,
            )
            return None

        new_item = new_items[0]

        # Mark complete if needed (add_item always creates as needs_action)
        if complete:
            await self._async_call_service(
                "update_item",
                data={"item": new_item.item_id, "status": "completed"},
            )
            new_item = HAShoppingItem(
                item_id=new_item.item_id,
                name=new_item.name,
                complete=True,
            )

        return new_item

    async def async_update_item(
        self,
        item_id: str,
        name: str | None = None,
        complete: bool | None = None,
    ) -> HAShoppingItem | None:
        """Update an existing todo item."""
        data: dict[str, Any] = {"item": item_id}
        if name is not None:
            data["rename"] = name
        if complete is not None:
            data["status"] = "completed" if complete else "needs_action"

        if len(data) <= 1:
            return None

        await self._async_call_service("update_item", data=data)

        # Return updated item representation
        items = await self.async_get_items()
        for item in items:
            if item.item_id == item_id:
                return item
        return None

    async def async_mark_complete(
        self, item_id: str, complete: bool
    ) -> HAShoppingItem | None:
        """Toggle completion status of a todo item."""
        return await self.async_update_item(item_id, complete=complete)

    async def async_delete_item(self, item_id: str) -> bool:
        """Delete an item from the todo entity."""
        try:
            await self._async_call_service(
                "remove_item", data={"item": [item_id]}
            )
            return True
        except Exception as err:
            _LOGGER.error(
                "Failed to delete todo item %s from %s: %s",
                item_id,
                self._entity_id,
                err,
            )
            return False

    async def async_clear_completed(self) -> None:
        """Clear all completed items."""
        await self._async_call_service("remove_completed_items")

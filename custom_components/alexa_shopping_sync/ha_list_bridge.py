"""Protocol and utilities for HA list bridges."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .models import HAShoppingItem


class HAListBridge(Protocol):
    """Protocol defining the interface for HA list backends.

    Implemented by ShoppingListBridge (built-in shopping list)
    and TodoListBridge (any todo.* entity).
    """

    async def async_get_items(self) -> list[HAShoppingItem]: ...

    async def async_add_item(self, name: str, complete: bool = False) -> HAShoppingItem | None: ...

    async def async_update_item(
        self,
        item_id: str,
        name: str | None = None,
        complete: bool | None = None,
    ) -> HAShoppingItem | None: ...

    async def async_mark_complete(self, item_id: str, complete: bool) -> HAShoppingItem | None: ...

    async def async_delete_item(self, item_id: str) -> bool: ...

    async def async_clear_completed(self) -> None: ...

    async def async_validate_available(self) -> bool: ...


def compute_snapshot_hash(items: list[HAShoppingItem]) -> str:
    """Compute a hash of the snapshot for change detection."""
    content = json.dumps(
        [
            {"id": i.item_id, "name": i.name, "complete": i.complete}
            for i in sorted(items, key=lambda x: x.item_id)
        ],
        sort_keys=True,
    )
    return hashlib.sha256(content.encode()).hexdigest()[:16]

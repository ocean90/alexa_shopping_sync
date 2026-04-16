"""Amazon Alexa Shopping List API client.

Verified against live Amazon API (2026-03).

API Base: https://www.amazon.de/alexashoppinglists/api/
Endpoints:
  GET  /getlistitems                    - Returns all lists with items
  POST /addlistitem/{list_id}           - Add item (payload: {value, type})
  PUT  /updatelistitem                  - Update item (payload: full item dict)
  DELETE /deletelistitem                - Delete item (payload: full item dict)

Authentication: Cookie-based (session cookies from Amazon login).
The API requires a PitanguiBridge User-Agent header.

Response format for getlistitems:
{
  "<list_id>": {
    "listInfo": { "listId", "listType", "defaultList", ... },
    "listItems": [ { "id", "value", "completed", "version", ... }, ... ]
  }
}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

import httpx

from .auth import AuthManager
from .const import (
    AMAZON_API_ADD_LIST_ITEM,
    AMAZON_API_DELETE_LIST_ITEM,
    AMAZON_API_GET_LIST_ITEMS,
    AMAZON_API_UPDATE_LIST_ITEM,
    AMAZON_SHOPPING_API_BASE,
    HTTP_BACKOFF_FACTOR,
    HTTP_RETRY_COUNT,
)
from .exceptions import (
    AmazonListNotFoundError,
    ConnectionError,
    SessionExpiredError,
    ThrottledError,
)
from .models import AlexaShoppingItem

_LOGGER = logging.getLogger(__name__)


class AmazonShoppingClient:
    """Client for Amazon Alexa Shopping List API.

    Uses the authenticated session from AuthManager.
    All Amazon API calls go through this client.
    """

    def __init__(self, auth_manager: AuthManager) -> None:
        """Initialize the client."""
        self._auth = auth_manager
        self._shopping_list_id: str | None = None
        self._api_base = AMAZON_SHOPPING_API_BASE.format(domain=auth_manager.amazon_domain)
        self._consecutive_auth_failures = 0
        # Cache of full item dicts from API (needed for update/delete payloads)
        self._item_cache: dict[str, dict[str, Any]] = {}

    @property
    def shopping_list_id(self) -> str | None:
        """Return the discovered shopping list ID."""
        return self._shopping_list_id

    @shopping_list_id.setter
    def shopping_list_id(self, value: str | None) -> None:
        """Set shopping list ID (e.g. from persisted state)."""
        self._shopping_list_id = value

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | list[dict[str, Any]] | None = None,
        retry_count: int = HTTP_RETRY_COUNT,
    ) -> dict[str, Any] | list[Any]:
        """Make an authenticated request to Amazon API with retry/backoff.

        Retry on 5xx and network errors, NOT on 4xx (except 429).
        401/403 -> session expired, trigger reauth.
        429 -> throttled, backoff and retry.
        """
        session = await self._auth.async_get_authenticated_session()

        url = f"{self._api_base}{path}"
        last_error: Exception | None = None

        for attempt in range(retry_count + 1):
            try:
                headers: dict[str, str] = {
                    "Accept": "*/*",
                    "Accept-Language": "*",
                    "DNT": "1",
                }
                kwargs: dict[str, Any] = {"headers": headers}
                if json_data is not None:
                    kwargs["json"] = json_data

                resp = await session.request(method, url, **kwargs)
                status = resp.status_code

                if status == 200:
                    self._consecutive_auth_failures = 0
                    return resp.json()

                if status == 204:
                    self._consecutive_auth_failures = 0
                    return {}

                if status in (401, 403):
                    self._consecutive_auth_failures += 1
                    self._auth.mark_session_expired()
                    raise SessionExpiredError(f"Amazon returned {status} - session expired")

                if status == 429:
                    if attempt < retry_count:
                        wait = HTTP_BACKOFF_FACTOR * (2**attempt)
                        _LOGGER.warning("Amazon rate limit (429), backing off %.1fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    raise ThrottledError("Amazon rate limit exceeded (429)")

                if status >= 500 and attempt < retry_count:
                    wait = HTTP_BACKOFF_FACTOR * (2**attempt)
                    _LOGGER.warning("Amazon server error %d, retrying in %.1fs", status, wait)
                    await asyncio.sleep(wait)
                    continue

                _LOGGER.error(
                    "Amazon API error %d for %s: %s",
                    status,
                    path,
                    resp.text[:200],
                )
                raise httpx.HTTPStatusError(
                    message=f"API error: {status}",
                    request=resp.request,
                    response=resp,
                )

            except (SessionExpiredError, ThrottledError):
                raise
            except httpx.HTTPStatusError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last_error = err
                if attempt < retry_count:
                    wait = HTTP_BACKOFF_FACTOR * (2**attempt)
                    _LOGGER.debug(
                        "Request to %s failed (%s), retrying in %.1fs",
                        path,
                        err,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue

        raise ConnectionError(
            f"Request to {path} failed after {retry_count + 1} attempts: {last_error}"
        )

    async def async_discover_shopping_list_id(self) -> str:
        """Discover the Alexa shopping list ID from getlistitems response.

        The getlistitems endpoint returns a dict keyed by list ID.
        Each value has a listInfo with listType="SHOPPING_LIST".
        The list ID is a base64-encoded string containing the account ID.
        """
        try:
            data = await self._async_request("GET", AMAZON_API_GET_LIST_ITEMS)
        except Exception as err:
            _LOGGER.error("Failed to discover shopping list: %s", err)
            raise AmazonListNotFoundError("Could not retrieve Alexa shopping lists") from err

        if not isinstance(data, dict):
            raise AmazonListNotFoundError("Unexpected response format from Amazon API")

        # The response is keyed by list ID
        for list_id, list_data in data.items():
            if not isinstance(list_data, dict):
                continue
            list_info = list_data.get("listInfo", {})

            # Strategy 1: Match by listType == SHOPPING_LIST
            if list_info.get("listType", "").upper() == "SHOPPING_LIST":
                self._shopping_list_id = list_id
                _LOGGER.debug("Found shopping list by type: %s", list_id)
                return list_id

        # Strategy 2: Match by defaultList flag
        for list_id, list_data in data.items():
            if not isinstance(list_data, dict):
                continue
            list_info = list_data.get("listInfo", {})
            if list_info.get("defaultList", False):
                self._shopping_list_id = list_id
                _LOGGER.debug("Found shopping list by default flag: %s", list_id)
                return list_id

        raise AmazonListNotFoundError(f"No shopping list found among {len(data)} lists.")

    async def async_get_snapshot(self) -> list[AlexaShoppingItem]:
        """Get current snapshot of Alexa shopping list items.

        Calls getlistitems and extracts items for the shopping list.
        Also caches full item dicts (needed for update/delete payloads).
        """
        data = await self._async_request("GET", AMAZON_API_GET_LIST_ITEMS)

        if not isinstance(data, dict):
            _LOGGER.error("Unexpected response format: %s", type(data))
            return []

        # Discover list ID if not set
        if not self._shopping_list_id:
            for list_id, list_data in data.items():
                if not isinstance(list_data, dict):
                    continue
                list_info = list_data.get("listInfo", {})
                if list_info.get("listType", "").upper() == "SHOPPING_LIST":
                    self._shopping_list_id = list_id
                    break

        if not self._shopping_list_id:
            _LOGGER.error("Shopping list ID not found in response")
            return []

        list_data = data.get(self._shopping_list_id, {})
        items_data = list_data.get("listItems", [])

        # Cache full item dicts and parse into models
        items: list[AlexaShoppingItem] = []
        self._item_cache.clear()
        for item_data in items_data:
            try:
                item = AlexaShoppingItem.from_api_response(item_data)
                items.append(item)
                # Cache full dict for update/delete operations
                self._item_cache[item.item_id] = item_data
            except Exception as err:
                _LOGGER.warning("Failed to parse Alexa item: %s", err)

        _LOGGER.debug("Loaded %d items from Alexa shopping list", len(items))
        return items

    async def async_add_item(
        self, summary: str, complete: bool = False
    ) -> AlexaShoppingItem | None:
        """Add an item to the Alexa shopping list.

        Endpoint: POST /addlistitem/{list_id}
        Payload: {"value": "item text", "type": "TASK"}
        """
        if not self._shopping_list_id:
            await self.async_discover_shopping_list_id()

        assert self._shopping_list_id is not None

        path = AMAZON_API_ADD_LIST_ITEM.format(list_id=self._shopping_list_id)
        payload = {
            "value": summary,
            "type": "TASK",
        }

        data = await self._async_request("POST", path, json_data=payload)

        if isinstance(data, dict):
            # If the item was created, it may need to be marked complete separately
            item = AlexaShoppingItem.from_api_response(data)
            if complete and not item.complete:
                return await self.async_mark_complete(item.item_id, True)
            return item
        return None

    async def async_update_item(
        self,
        item_id: str,
        summary: str | None = None,
        complete: bool | None = None,
        version: int | None = None,
    ) -> AlexaShoppingItem | None:
        """Update an existing Alexa shopping list item.

        Endpoint: PUT /updatelistitem
        Payload: Full item dict with updated fields.
        The Amazon API requires the complete item object, not just changed fields.
        """
        # Get cached item dict - full object is REQUIRED (400 InputFailure otherwise)
        item_dict = self._item_cache.get(item_id, {}).copy()

        if not item_dict:
            # Cache miss: fetch fresh snapshot to populate cache
            _LOGGER.debug("Item %s not in cache, fetching snapshot", item_id)
            await self.async_get_snapshot()
            item_dict = self._item_cache.get(item_id, {}).copy()
            if not item_dict:
                _LOGGER.error("Item %s not found after snapshot refresh", item_id)
                return None

        if summary is not None:
            item_dict["value"] = summary
        if complete is not None:
            item_dict["completed"] = complete
        if version is not None:
            item_dict["version"] = version

        data = await self._async_request(
            "PUT",
            AMAZON_API_UPDATE_LIST_ITEM,
            json_data=item_dict,
        )

        if isinstance(data, dict) and "id" in data:
            self._item_cache[item_id] = data
            return AlexaShoppingItem.from_api_response(data)
        return None

    async def async_mark_complete(self, item_id: str, complete: bool) -> AlexaShoppingItem | None:
        """Toggle completion status of an Alexa item."""
        return await self.async_update_item(item_id, complete=complete)

    async def async_delete_item(self, item_id: str) -> bool:
        """Delete an item from the Alexa shopping list.

        Endpoint: DELETE /deletelistitem
        Payload: Full item dict.
        """
        # Full object is REQUIRED for delete (400 InputFailure otherwise)
        item_dict = self._item_cache.get(item_id, {}).copy()

        if not item_dict:
            _LOGGER.debug("Item %s not in cache for delete, fetching snapshot", item_id)
            await self.async_get_snapshot()
            item_dict = self._item_cache.get(item_id, {}).copy()
            if not item_dict:
                _LOGGER.warning("Item %s not found for deletion - may already be deleted", item_id)
                return True  # Treat as success if item doesn't exist

        try:
            await self._async_request(
                "DELETE",
                AMAZON_API_DELETE_LIST_ITEM,
                json_data=item_dict,
            )
            self._item_cache.pop(item_id, None)
            return True
        except Exception as err:
            _LOGGER.error("Failed to delete Alexa item %s: %s", item_id, err)
            return False

    def compute_snapshot_hash(self, items: list[AlexaShoppingItem]) -> str:
        """Compute a hash of the snapshot for change detection."""
        content = json.dumps(
            [
                {"id": i.item_id, "name": i.name, "complete": i.complete}
                for i in sorted(items, key=lambda x: x.item_id)
            ],
            sort_keys=True,
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]

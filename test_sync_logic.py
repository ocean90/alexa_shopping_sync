#!/usr/bin/env python3
"""Standalone test for sync engine logic — no Home Assistant required.

Tests the critical sync scenarios: initial sync, warm start, full resync.
Run with: python3 test_sync_logic.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so sync_engine.py can be imported without homeassistant
# ---------------------------------------------------------------------------
class _FakeStore:
    def __init__(self, *a, **kw):
        self._data: dict | None = None

    def __class_getitem__(cls, item):
        return cls

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data


def _make_ha_mocks():
    hass_mod = MagicMock()
    hass_mod.core = MagicMock()
    hass_mod.core.HomeAssistant = object
    storage_mod = MagicMock()
    storage_mod.Store = _FakeStore

    for mod in [
        "homeassistant",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.storage",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.exceptions",
        "homeassistant.components",
        "homeassistant.components.http",
        "homeassistant.components.http.view",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.issue_registry",
        "homeassistant.helpers.network",
        "homeassistant.data_entry_flow",
    ]:
        sys.modules.setdefault(mod, MagicMock())

    sys.modules["homeassistant.helpers.storage"] = storage_mod


_make_ha_mocks()

# Now we can import our modules
sys.path.insert(0, ".")
from custom_components.alexa_shopping_sync.const import (
    InitialSyncMode,
    SyncMode,
)
from custom_components.alexa_shopping_sync.models import (
    AlexaShoppingItem,
    HAShoppingItem,
    ItemSource,
)
from custom_components.alexa_shopping_sync.sync_engine import SyncEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def alexa(item_id: str, name: str, complete: bool = False) -> AlexaShoppingItem:
    return AlexaShoppingItem(item_id=item_id, name=name, complete=complete)


def ha(item_id: str, name: str, complete: bool = False) -> HAShoppingItem:
    return HAShoppingItem(item_id=item_id, name=name, complete=complete)


def make_engine(
    sync_mode=SyncMode.TWO_WAY,
    initial_sync_mode=InitialSyncMode.MERGE_UNION,
):
    mock_amazon = MagicMock()
    mock_amazon.async_add_item = AsyncMock()
    mock_amazon.async_delete_item = AsyncMock(return_value=True)
    mock_amazon.async_update_item = AsyncMock()
    mock_amazon.async_get_snapshot = AsyncMock(return_value=[])
    mock_amazon.shopping_list_id = None

    mock_ha_bridge = MagicMock()
    mock_ha_bridge.async_add_item = AsyncMock()
    mock_ha_bridge.async_delete_item = AsyncMock(return_value=True)
    mock_ha_bridge.async_update_item = AsyncMock()
    mock_ha_bridge.async_get_items = AsyncMock(return_value=[])
    mock_ha_bridge.async_mark_complete = AsyncMock()

    engine = SyncEngine(
        hass=MagicMock(),
        amazon_client=mock_amazon,
        ha_bridge=mock_ha_bridge,
        sync_mode=sync_mode,
        initial_sync_mode=initial_sync_mode,
        preserve_duplicates=True,
        mirror_completed=True,
    )
    return engine, mock_amazon, mock_ha_bridge


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global _failed
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}" + (f": {detail}" if detail else ""))
        _failed += 1


async def test_initial_sync_matching():
    """All items match by name → no adds, 4 mappings."""
    print("\n[test_initial_sync_matching]")
    engine, amazon, ha_bridge = make_engine()

    alexa_items = [alexa("a1", "Kaffee"), alexa("a2", "Salat"),
                   alexa("a3", "Bananen"), alexa("a4", "Eier")]
    ha_items = [ha("h1", "Kaffee"), ha("h2", "Salat"),
                ha("h3", "Bananen"), ha("h4", "Eier")]

    result = await engine.async_initial_sync(alexa_items, ha_items)

    check("no Alexa→HA adds", result.alexa_to_ha_adds == 0, str(result.alexa_to_ha_adds))
    check("no HA→Alexa adds", result.ha_to_alexa_adds == 0, str(result.ha_to_alexa_adds))
    check("4 mappings created", len(engine.state.mappings) == 4, str(len(engine.state.mappings)))
    check("amazon.async_add_item not called", amazon.async_add_item.call_count == 0)
    check("ha_bridge.async_add_item not called", ha_bridge.async_add_item.call_count == 0)


async def test_warm_start_no_duplicates():
    """After restart (empty prev, initial_sync_done=True), first poll = no adds."""
    print("\n[test_warm_start_no_duplicates]")
    engine, amazon, ha_bridge = make_engine()

    # Simulate state loaded from storage with existing mappings
    engine._initial_sync_done = True
    engine._add_mapping("a1", "h1", "Kaffee", ItemSource.ALEXA)
    engine._add_mapping("a2", "h2", "Salat", ItemSource.ALEXA)

    # _previous_alexa_items is [] (not persisted)
    alexa_items = [alexa("a1", "Kaffee"), alexa("a2", "Salat")]

    result = await engine.async_sync_alexa_to_ha(alexa_items)

    check("no adds on warm start", result.alexa_to_ha_adds == 0)
    check("ha_bridge.async_add_item not called", ha_bridge.async_add_item.call_count == 0)
    check("previous_alexa_items set as baseline",
          len(engine._previous_alexa_items) == 2)


async def test_warm_start_ha_side():
    """HA warm start: first event after restart doesn't push all items to Alexa."""
    print("\n[test_warm_start_ha_side]")
    engine, amazon, ha_bridge = make_engine()

    engine._initial_sync_done = True
    engine._add_mapping("a1", "h1", "Kaffee", ItemSource.ALEXA)
    engine._add_mapping("a2", "h2", "Salat", ItemSource.ALEXA)

    ha_items = [ha("h1", "Kaffee"), ha("h2", "Salat")]

    result = await engine.async_sync_ha_to_alexa(ha_items)

    check("no adds on warm start", result.ha_to_alexa_adds == 0)
    check("amazon.async_add_item not called", amazon.async_add_item.call_count == 0)


async def test_full_resync_no_duplicates():
    """Full resync with matching items → 0 adds."""
    print("\n[test_full_resync_no_duplicates]")
    engine, amazon, ha_bridge = make_engine()

    alexa_items = [alexa("a1", "Kaffee"), alexa("a2", "Salat"),
                   alexa("a3", "Bananen"), alexa("a4", "Eier")]
    ha_items = [ha("h1", "Kaffee"), ha("h2", "Salat"),
                ha("h3", "Bananen"), ha("h4", "Eier")]

    amazon.async_get_snapshot.return_value = alexa_items
    ha_bridge.async_get_items.return_value = ha_items

    result = await engine.async_full_resync()

    check("no Alexa→HA adds", result.alexa_to_ha_adds == 0, str(result.alexa_to_ha_adds))
    check("no HA→Alexa adds", result.ha_to_alexa_adds == 0, str(result.ha_to_alexa_adds))
    check("4 mappings", len(engine.state.mappings) == 4, str(len(engine.state.mappings)))
    check("previous_alexa_items set", len(engine._previous_alexa_items) == 4)
    check("previous_ha_items set", len(engine._previous_ha_items) == 4)


async def test_poll_after_full_resync_no_duplicates():
    """Poll immediately after full_resync should not produce any adds."""
    print("\n[test_poll_after_full_resync_no_duplicates]")
    engine, amazon, ha_bridge = make_engine()

    alexa_items = [alexa("a1", "Kaffee"), alexa("a2", "Salat"),
                   alexa("a3", "Bananen"), alexa("a4", "Eier")]
    ha_items = [ha("h1", "Kaffee"), ha("h2", "Salat"),
                ha("h3", "Bananen"), ha("h4", "Eier")]

    amazon.async_get_snapshot.return_value = alexa_items
    ha_bridge.async_get_items.return_value = ha_items

    await engine.async_full_resync()

    # Simulate next poll (same Alexa items, no changes)
    ha_bridge.async_add_item.reset_mock()
    amazon.async_add_item.reset_mock()

    result = await engine.async_sync_alexa_to_ha(alexa_items)

    check("poll after resync: no adds", result.alexa_to_ha_adds == 0)
    check("ha_bridge not called again", ha_bridge.async_add_item.call_count == 0)


async def test_warm_start_unmapped_alexa_item():
    """Warm start with an unmapped Alexa item → it should sync to HA."""
    print("\n[test_warm_start_unmapped_alexa_item]")
    engine, amazon, ha_bridge = make_engine()

    engine._initial_sync_done = True
    engine._add_mapping("a1", "h1", "Kaffee", ItemSource.ALEXA)
    # a2 has NO mapping — added after last restart, before first poll

    ha_bridge.async_add_item.return_value = ha("h2", "Milch")
    alexa_items = [alexa("a1", "Kaffee"), alexa("a2", "Milch")]

    result = await engine.async_sync_alexa_to_ha(alexa_items)

    check("unmapped item synced to HA", result.alexa_to_ha_adds == 1, str(result.alexa_to_ha_adds))
    ha_bridge.async_add_item.assert_called_once_with("Milch", False)
    check("2 mappings after warm start", len(engine.state.mappings) == 2, str(len(engine.state.mappings)))


async def test_warm_start_unmapped_ha_item():
    """Warm start with an unmapped HA item → it should sync to Alexa."""
    print("\n[test_warm_start_unmapped_ha_item]")
    engine, amazon, ha_bridge = make_engine()

    engine._initial_sync_done = True
    engine._add_mapping("a1", "h1", "Kaffee", ItemSource.ALEXA)
    # h2 has NO mapping — added after last restart, before first event

    amazon.async_add_item.return_value = alexa("a2", "Milch")
    ha_items = [ha("h1", "Kaffee"), ha("h2", "Milch")]

    result = await engine.async_sync_ha_to_alexa(ha_items)

    check("unmapped item synced to Alexa", result.ha_to_alexa_adds == 1, str(result.ha_to_alexa_adds))
    amazon.async_add_item.assert_called_once_with("Milch", False)
    check("2 mappings after warm start", len(engine.state.mappings) == 2, str(len(engine.state.mappings)))


async def test_new_item_after_warm_start():
    """After warm start, NEW items added to Alexa should sync to HA."""
    print("\n[test_new_item_after_warm_start]")
    engine, amazon, ha_bridge = make_engine()

    # Warm start
    engine._initial_sync_done = True
    engine._add_mapping("a1", "h1", "Kaffee", ItemSource.ALEXA)
    baseline = [alexa("a1", "Kaffee")]
    await engine.async_sync_alexa_to_ha(baseline)  # sets baseline

    # New item appears in Alexa
    ha_bridge.async_add_item.return_value = ha("h2", "Milch")
    new_items = [alexa("a1", "Kaffee"), alexa("a2", "Milch")]
    result = await engine.async_sync_alexa_to_ha(new_items)

    check("new item synced to HA", result.alexa_to_ha_adds == 1, str(result.alexa_to_ha_adds))
    ha_bridge.async_add_item.assert_called_once_with("Milch", False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_all():
    await test_initial_sync_matching()
    await test_warm_start_no_duplicates()
    await test_warm_start_ha_side()
    await test_warm_start_unmapped_alexa_item()
    await test_warm_start_unmapped_ha_item()
    await test_full_resync_no_duplicates()
    await test_poll_after_full_resync_no_duplicates()
    await test_new_item_after_warm_start()

    print()
    if _failed == 0:
        print(f"\033[32mAll tests passed.\033[0m")
    else:
        print(f"\033[31m{_failed} test(s) failed.\033[0m")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_all())

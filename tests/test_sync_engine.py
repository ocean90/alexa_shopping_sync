"""Tests for the sync engine."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.alexa_shopping_sync.const import (
    PENDING_OP_GRACE_SECONDS,
    InitialSyncMode,
    PendingOpType,
    SyncMode,
)
from custom_components.alexa_shopping_sync.models import (
    AlexaShoppingItem,
    HAShoppingItem,
    ItemSource,
)
from custom_components.alexa_shopping_sync.sync_engine import SyncEngine

from .conftest import make_alexa_item, make_ha_item


class TestInitialSyncMergeUnion:
    """Tests for initial sync with merge_union strategy."""

    @pytest.mark.asyncio
    async def test_empty_lists(self, sync_engine):
        """Both lists empty -> no changes."""
        result = await sync_engine.async_initial_sync([], [])
        assert result.alexa_to_ha_adds == 0
        assert result.ha_to_alexa_adds == 0

    @pytest.mark.asyncio
    async def test_alexa_only_items(self, sync_engine, mock_ha_bridge):
        """Alexa has items, HA empty -> items added to HA."""
        alexa_items = [
            make_alexa_item("a1", "Milk"),
            make_alexa_item("a2", "Bread"),
        ]
        mock_ha_bridge.async_add_item.side_effect = [
            make_ha_item("h1", "Milk"),
            make_ha_item("h2", "Bread"),
        ]

        result = await sync_engine.async_initial_sync(alexa_items, [])

        assert result.alexa_to_ha_adds == 2
        assert mock_ha_bridge.async_add_item.call_count == 2

    @pytest.mark.asyncio
    async def test_ha_only_items(self, sync_engine, mock_amazon_client):
        """HA has items, Alexa empty -> items added to Alexa."""
        ha_items = [
            make_ha_item("h1", "Eggs"),
        ]
        mock_amazon_client.async_add_item.return_value = make_alexa_item("a1", "Eggs")

        result = await sync_engine.async_initial_sync([], ha_items)

        assert result.ha_to_alexa_adds == 1
        assert mock_amazon_client.async_add_item.call_count == 1

    @pytest.mark.asyncio
    async def test_matching_items_by_name(self, sync_engine):
        """Items with same name are matched, not duplicated."""
        alexa_items = [make_alexa_item("a1", "Milk")]
        ha_items = [make_ha_item("h1", "Milk")]

        result = await sync_engine.async_initial_sync(alexa_items, ha_items)

        assert result.alexa_to_ha_adds == 0
        assert result.ha_to_alexa_adds == 0
        assert len(sync_engine.state.mappings) == 1
        assert sync_engine.state.mappings[0].alexa_id == "a1"
        assert sync_engine.state.mappings[0].ha_id == "h1"

    @pytest.mark.asyncio
    async def test_matching_case_insensitive(self, sync_engine):
        """Name matching is case-insensitive."""
        alexa_items = [make_alexa_item("a1", "MILK")]
        ha_items = [make_ha_item("h1", "milk")]

        result = await sync_engine.async_initial_sync(alexa_items, ha_items)

        assert result.alexa_to_ha_adds == 0
        assert result.ha_to_alexa_adds == 0
        assert len(sync_engine.state.mappings) == 1

    @pytest.mark.asyncio
    async def test_no_blind_deletions(self, sync_engine, mock_ha_bridge, mock_amazon_client):
        """Merge union must not delete anything."""
        alexa_items = [make_alexa_item("a1", "Milk")]
        ha_items = [make_ha_item("h1", "Bread")]

        mock_ha_bridge.async_add_item.return_value = make_ha_item("h2", "Milk")
        mock_amazon_client.async_add_item.return_value = make_alexa_item("a2", "Bread")

        result = await sync_engine.async_initial_sync(alexa_items, ha_items)

        assert result.alexa_to_ha_deletes == 0
        assert result.ha_to_alexa_deletes == 0
        # Both unique items should be added to the other side
        assert result.alexa_to_ha_adds == 1
        assert result.ha_to_alexa_adds == 1


class TestAlexaToHaSync:
    """Tests for Alexa -> HA sync during regular operation."""

    @pytest.mark.asyncio
    async def test_new_alexa_item(self, sync_engine, mock_ha_bridge):
        """New Alexa item should be added to HA."""
        # First do initial sync with empty lists
        sync_engine._initial_sync_done = True
        sync_engine._previous_alexa_items = []

        new_items = [make_alexa_item("a1", "Milk")]
        mock_ha_bridge.async_add_item.return_value = make_ha_item("h1", "Milk")

        result = await sync_engine.async_sync_alexa_to_ha(new_items)

        assert result.alexa_to_ha_adds == 1
        mock_ha_bridge.async_add_item.assert_called_once_with("Milk", False)

    @pytest.mark.asyncio
    async def test_deleted_alexa_item(self, sync_engine, mock_ha_bridge):
        """Deleted Alexa item should be deleted from HA."""
        sync_engine._initial_sync_done = True

        old_item = make_alexa_item("a1", "Milk")
        sync_engine._previous_alexa_items = [old_item]
        sync_engine._add_mapping("a1", "h1", "Milk", ItemSource.ALEXA)

        result = await sync_engine.async_sync_alexa_to_ha([])

        assert result.alexa_to_ha_deletes == 1
        mock_ha_bridge.async_delete_item.assert_called_once_with("h1")

    @pytest.mark.asyncio
    async def test_completed_alexa_item(self, sync_engine, mock_ha_bridge):
        """Completed status change should sync to HA."""
        sync_engine._initial_sync_done = True

        old_item = make_alexa_item("a1", "Milk", complete=False)
        new_item = make_alexa_item("a1", "Milk", complete=True)
        sync_engine._previous_alexa_items = [old_item]
        sync_engine._add_mapping("a1", "h1", "Milk", ItemSource.ALEXA)

        result = await sync_engine.async_sync_alexa_to_ha([new_item])

        assert result.alexa_to_ha_updates == 1
        mock_ha_bridge.async_update_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_alexa_to_ha_disabled(self, sync_engine):
        """HA_TO_ALEXA mode should skip Alexa->HA sync."""
        sync_engine._sync_mode = SyncMode.HA_TO_ALEXA
        sync_engine._initial_sync_done = True
        sync_engine._previous_alexa_items = []

        result = await sync_engine.async_sync_alexa_to_ha(
            [make_alexa_item("a1", "Milk")]
        )

        assert result.alexa_to_ha_adds == 0


class TestHaToAlexaSync:
    """Tests for HA -> Alexa sync."""

    @pytest.mark.asyncio
    async def test_new_ha_item(self, sync_engine, mock_amazon_client):
        """New HA item should be added to Alexa."""
        sync_engine._initial_sync_done = True
        sync_engine._previous_ha_items = []

        mock_amazon_client.async_add_item.return_value = make_alexa_item("a1", "Eggs")

        result = await sync_engine.async_sync_ha_to_alexa(
            [make_ha_item("h1", "Eggs")]
        )

        assert result.ha_to_alexa_adds == 1

    @pytest.mark.asyncio
    async def test_deleted_ha_item(self, sync_engine, mock_amazon_client):
        """Deleted HA item should be deleted from Alexa."""
        sync_engine._initial_sync_done = True

        sync_engine._previous_ha_items = [make_ha_item("h1", "Eggs")]
        sync_engine._add_mapping("a1", "h1", "Eggs", ItemSource.HA)

        result = await sync_engine.async_sync_ha_to_alexa([])

        assert result.ha_to_alexa_deletes == 1
        mock_amazon_client.async_delete_item.assert_called_once_with("a1")

    @pytest.mark.asyncio
    async def test_ha_to_alexa_disabled(self, sync_engine):
        """ALEXA_TO_HA mode should skip HA->Alexa sync."""
        sync_engine._sync_mode = SyncMode.ALEXA_TO_HA
        sync_engine._initial_sync_done = True
        sync_engine._previous_ha_items = []

        result = await sync_engine.async_sync_ha_to_alexa(
            [make_ha_item("h1", "Eggs")]
        )

        assert result.ha_to_alexa_adds == 0


class TestEchoSuppression:
    """Tests for echo suppression / pending ops."""

    @pytest.mark.asyncio
    async def test_own_write_not_reflected_back(self, sync_engine, mock_ha_bridge):
        """Items we wrote to Alexa should not bounce back as new HA items."""
        sync_engine._initial_sync_done = True
        sync_engine._previous_alexa_items = []

        # Simulate a pending op (we just wrote "Eggs" to Alexa)
        sync_engine.add_pending_op(
            PendingOpType.ADD, ItemSource.HA, "Eggs", "a1"
        )

        # Now Alexa returns that item
        result = await sync_engine.async_sync_alexa_to_ha(
            [make_alexa_item("a1", "Eggs")]
        )

        assert result.alexa_to_ha_adds == 0
        assert result.skipped_echo == 1
        mock_ha_bridge.async_add_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_pending_op_not_suppressed(
        self, sync_engine, mock_ha_bridge
    ):
        """Expired pending ops should not suppress changes."""
        sync_engine._initial_sync_done = True
        sync_engine._previous_alexa_items = []

        # Add an expired pending op
        sync_engine.add_pending_op(
            PendingOpType.ADD, ItemSource.HA, "Eggs", "a1"
        )
        # Make it expired
        sync_engine.state.pending_ops[0].created_at = (
            time.time() - PENDING_OP_GRACE_SECONDS - 1
        )

        mock_ha_bridge.async_add_item.return_value = make_ha_item("h1", "Eggs")

        result = await sync_engine.async_sync_alexa_to_ha(
            [make_alexa_item("a1", "Eggs")]
        )

        assert result.alexa_to_ha_adds == 1

    def test_pending_op_cleanup(self, sync_engine):
        """Expired pending ops should be cleaned up."""
        # Add some ops - one fresh, one expired
        sync_engine.add_pending_op(PendingOpType.ADD, ItemSource.HA, "Fresh")
        sync_engine.add_pending_op(PendingOpType.ADD, ItemSource.HA, "Old")
        sync_engine.state.pending_ops[1].created_at = (
            time.time() - PENDING_OP_GRACE_SECONDS * 4
        )

        sync_engine._cleanup_expired_pending_ops()

        assert len(sync_engine.state.pending_ops) == 1
        assert sync_engine.state.pending_ops[0].item_name == "Fresh"


class TestConflictResolution:
    """Tests for conflict resolution scenarios."""

    @pytest.mark.asyncio
    async def test_pending_op_has_priority(self, sync_engine, mock_ha_bridge):
        """Within grace window, pending op takes priority over remote change."""
        sync_engine._initial_sync_done = True

        old_item = make_alexa_item("a1", "Milk", complete=False)
        sync_engine._previous_alexa_items = [old_item]
        sync_engine._add_mapping("a1", "h1", "Milk", ItemSource.HA)

        # We just marked it complete from HA side
        sync_engine.add_pending_op(
            PendingOpType.COMPLETE, ItemSource.HA, "Milk", "a1"
        )

        # Alexa now also shows a change (which is our own echo)
        new_item = make_alexa_item("a1", "Milk", complete=True)
        result = await sync_engine.async_sync_alexa_to_ha([new_item])

        # Should be suppressed as echo
        assert result.skipped_echo == 1
        assert result.alexa_to_ha_updates == 0


class TestDuplicateHandling:
    """Tests for duplicate item handling."""

    @pytest.mark.asyncio
    async def test_preserve_duplicates(self, sync_engine, mock_ha_bridge):
        """With preserve_duplicates=True, duplicates from Alexa are added."""
        sync_engine._initial_sync_done = True
        sync_engine._previous_alexa_items = [make_alexa_item("a1", "Milk")]
        sync_engine._add_mapping("a1", "h1", "Milk", ItemSource.ALEXA)

        # Second "Milk" appears in Alexa
        new_items = [
            make_alexa_item("a1", "Milk"),
            make_alexa_item("a2", "Milk"),
        ]
        mock_ha_bridge.async_add_item.return_value = make_ha_item("h2", "Milk")

        result = await sync_engine.async_sync_alexa_to_ha(new_items)

        assert result.alexa_to_ha_adds == 1

    @pytest.mark.asyncio
    async def test_matching_with_duplicates(self, sync_engine):
        """During initial sync, duplicate names match correctly."""
        alexa_items = [
            make_alexa_item("a1", "Milk", complete=False),
            make_alexa_item("a2", "Milk", complete=True),
        ]
        ha_items = [
            make_ha_item("h1", "Milk", complete=False),
            make_ha_item("h2", "Milk", complete=True),
        ]

        result = await sync_engine.async_initial_sync(alexa_items, ha_items)

        # Both should be matched by name+status
        assert len(sync_engine.state.mappings) == 2
        assert result.alexa_to_ha_adds == 0
        assert result.ha_to_alexa_adds == 0

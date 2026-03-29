"""Sync engine for bidirectional shopping list synchronization."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .amazon_client import AmazonShoppingClient
from .const import (
    DOMAIN,
    ECHO_SUPPRESSION_WINDOW,
    MAX_PENDING_OPS,
    PENDING_OP_GRACE_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
    InitialSyncMode,
    PendingOpType,
    SyncMode,
)
from .models import (
    AlexaShoppingItem,
    HAShoppingItem,
    ItemMapping,
    ItemSource,
    PendingOperation,
    SyncState,
    normalize_name,
)
from .shopping_list_bridge import ShoppingListBridge

_LOGGER = logging.getLogger(__name__)


@dataclass
class SyncDiff:
    """Result of diffing two snapshots."""

    added: list[Any] = field(default_factory=list)
    removed: list[Any] = field(default_factory=list)
    modified: list[tuple[Any, Any]] = field(default_factory=list)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    alexa_to_ha_adds: int = 0
    alexa_to_ha_updates: int = 0
    alexa_to_ha_deletes: int = 0
    ha_to_alexa_adds: int = 0
    ha_to_alexa_updates: int = 0
    ha_to_alexa_deletes: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_echo: int = 0


class SyncEngine:
    """Engine for bidirectional sync between Alexa and HA shopping lists.

    Core responsibilities:
    - Diffing snapshots to detect changes
    - Maintaining ID mappings between Alexa and HA items
    - Echo suppression via pending operations
    - Conflict resolution (last successful write wins)
    - Initial sync with configurable merge strategy
    """

    def __init__(
        self,
        hass: HomeAssistant,
        amazon_client: AmazonShoppingClient,
        ha_bridge: ShoppingListBridge,
        sync_mode: SyncMode,
        initial_sync_mode: InitialSyncMode,
        preserve_duplicates: bool = True,
        mirror_completed: bool = True,
    ) -> None:
        """Initialize sync engine."""
        self._hass = hass
        self._amazon = amazon_client
        self._ha = ha_bridge
        self._sync_mode = sync_mode
        self._initial_sync_mode = initial_sync_mode
        self._preserve_duplicates = preserve_duplicates
        self._mirror_completed = mirror_completed

        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._state = SyncState()
        self._previous_alexa_items: list[AlexaShoppingItem] = []
        self._previous_ha_items: list[HAShoppingItem] = []
        self._initial_sync_done = False

    @property
    def state(self) -> SyncState:
        """Return current sync state."""
        return self._state

    @property
    def sync_mode(self) -> SyncMode:
        """Return current sync mode."""
        return self._sync_mode

    @sync_mode.setter
    def sync_mode(self, value: SyncMode) -> None:
        """Update sync mode."""
        self._sync_mode = value

    async def async_load_state(self) -> None:
        """Load persisted state from storage."""
        data = await self._store.async_load()
        if data:
            self._state = SyncState.from_dict(data)
            if self._state.shopping_list_id:
                self._amazon.shopping_list_id = self._state.shopping_list_id
            if self._state.mappings:
                self._initial_sync_done = True
            _LOGGER.debug(
                "Loaded sync state: %d mappings, %d pending ops",
                len(self._state.mappings),
                len(self._state.pending_ops),
            )

    async def async_save_state(self) -> None:
        """Persist state to storage."""
        await self._store.async_save(self._state.to_dict())

    async def async_clear_state(self) -> None:
        """Clear all persisted state."""
        self._state = SyncState()
        self._initial_sync_done = False
        self._previous_alexa_items = []
        self._previous_ha_items = []
        await self._store.async_save(self._state.to_dict())

    def add_pending_op(
        self,
        op_type: PendingOpType,
        source: ItemSource,
        item_name: str,
        target_id: str | None = None,
    ) -> None:
        """Register a pending operation for echo suppression."""
        op = PendingOperation(
            op_type=op_type,
            source=source,
            item_name=item_name,
            target_id=target_id,
            created_at=time.time(),
        )
        self._state.pending_ops.append(op)

        # Trim old pending ops
        if len(self._state.pending_ops) > MAX_PENDING_OPS:
            self._state.pending_ops = self._state.pending_ops[-MAX_PENDING_OPS:]

    def _is_echo(
        self,
        op_type: PendingOpType,
        item_name: str,
        target_id: str | None = None,
    ) -> bool:
        """Check if an incoming change is an echo of our own write.

        A change is an echo if there's a matching pending op within
        the grace window that hasn't been confirmed yet.
        """
        now = time.time()
        normalized = normalize_name(item_name)

        for op in self._state.pending_ops:
            if op.confirmed:
                continue
            if now - op.created_at > PENDING_OP_GRACE_SECONDS:
                continue
            if op.op_type != op_type:
                continue

            op_normalized = normalize_name(op.item_name)
            if op_normalized == normalized:
                op.confirmed = True
                return True

            if target_id and op.target_id == target_id:
                op.confirmed = True
                return True

        return False

    def _cleanup_expired_pending_ops(self) -> None:
        """Remove expired pending operations."""
        now = time.time()
        self._state.pending_ops = [
            op
            for op in self._state.pending_ops
            if now - op.created_at < PENDING_OP_GRACE_SECONDS * 3
        ]

    def _find_mapping_by_alexa_id(self, alexa_id: str) -> ItemMapping | None:
        """Find mapping by Alexa item ID."""
        for m in self._state.mappings:
            if m.alexa_id == alexa_id:
                return m
        return None

    def _find_mapping_by_ha_id(self, ha_id: str) -> ItemMapping | None:
        """Find mapping by HA item ID."""
        for m in self._state.mappings:
            if m.ha_id == ha_id:
                return m
        return None

    def _find_mapping_by_name(self, name: str) -> ItemMapping | None:
        """Find mapping by normalized name."""
        normalized = normalize_name(name)
        for m in self._state.mappings:
            if normalize_name(m.name) == normalized:
                return m
        return None

    def _add_mapping(
        self, alexa_id: str, ha_id: str, name: str, source: ItemSource
    ) -> ItemMapping:
        """Add a new mapping."""
        mapping = ItemMapping(
            alexa_id=alexa_id,
            ha_id=ha_id,
            name=name,
            last_synced=str(time.time()),
            source=source,
        )
        self._state.mappings.append(mapping)
        return mapping

    def _remove_mapping_by_alexa_id(self, alexa_id: str) -> None:
        """Remove mapping by Alexa ID."""
        self._state.mappings = [
            m for m in self._state.mappings if m.alexa_id != alexa_id
        ]

    def _remove_mapping_by_ha_id(self, ha_id: str) -> None:
        """Remove mapping by HA ID."""
        self._state.mappings = [
            m for m in self._state.mappings if m.ha_id != ha_id
        ]

    def _match_item_by_name(
        self,
        name: str,
        complete: bool,
        candidates: list[AlexaShoppingItem] | list[HAShoppingItem],
        exclude_ids: set[str] | None = None,
    ) -> AlexaShoppingItem | HAShoppingItem | None:
        """Match an item by normalized name + completion status.

        Used during initial sync when IDs don't exist yet.
        """
        normalized = normalize_name(name)
        exclude = exclude_ids or set()

        for candidate in candidates:
            if candidate.item_id in exclude:
                continue
            if candidate.normalized_name == normalized:
                if not self._preserve_duplicates:
                    return candidate
                # With preserve_duplicates, also match on status
                if candidate.complete == complete:
                    return candidate

        # Fallback: match just by name regardless of status
        for candidate in candidates:
            if candidate.item_id in exclude:
                continue
            if candidate.normalized_name == normalized:
                return candidate

        return None

    def _diff_alexa_snapshots(
        self,
        old: list[AlexaShoppingItem],
        new: list[AlexaShoppingItem],
    ) -> SyncDiff:
        """Diff two Alexa snapshots."""
        old_by_id = {i.item_id: i for i in old}
        new_by_id = {i.item_id: i for i in new}

        diff = SyncDiff()

        # Added items
        for item_id, item in new_by_id.items():
            if item_id not in old_by_id:
                diff.added.append(item)

        # Removed items
        for item_id, item in old_by_id.items():
            if item_id not in new_by_id:
                diff.removed.append(item)

        # Modified items
        for item_id in new_by_id:
            if item_id in old_by_id:
                old_item = old_by_id[item_id]
                new_item = new_by_id[item_id]
                if (
                    old_item.name != new_item.name
                    or old_item.complete != new_item.complete
                ):
                    diff.modified.append((old_item, new_item))

        return diff

    def _diff_ha_snapshots(
        self,
        old: list[HAShoppingItem],
        new: list[HAShoppingItem],
    ) -> SyncDiff:
        """Diff two HA snapshots."""
        old_by_id = {i.item_id: i for i in old}
        new_by_id = {i.item_id: i for i in new}

        diff = SyncDiff()

        for item_id, item in new_by_id.items():
            if item_id not in old_by_id:
                diff.added.append(item)

        for item_id, item in old_by_id.items():
            if item_id not in new_by_id:
                diff.removed.append(item)

        for item_id in new_by_id:
            if item_id in old_by_id:
                old_item = old_by_id[item_id]
                new_item = new_by_id[item_id]
                if (
                    old_item.name != new_item.name
                    or old_item.complete != new_item.complete
                ):
                    diff.modified.append((old_item, new_item))

        return diff

    async def async_initial_sync(
        self,
        alexa_items: list[AlexaShoppingItem],
        ha_items: list[HAShoppingItem],
    ) -> SyncResult:
        """Perform initial sync based on configured strategy.

        Decision: No blind deletions during initial merge.
        merge_union = union of both lists (default, safest).
        alexa_wins = HA is overwritten with Alexa items.
        ha_wins = Alexa is overwritten with HA items.
        """
        result = SyncResult()

        if self._initial_sync_mode == InitialSyncMode.MERGE_UNION:
            result = await self._async_initial_merge_union(alexa_items, ha_items)
        elif self._initial_sync_mode == InitialSyncMode.ALEXA_WINS:
            result = await self._async_initial_alexa_wins(alexa_items, ha_items)
        elif self._initial_sync_mode == InitialSyncMode.HA_WINS:
            result = await self._async_initial_ha_wins(alexa_items, ha_items)

        self._initial_sync_done = True
        return result

    async def _async_initial_merge_union(
        self,
        alexa_items: list[AlexaShoppingItem],
        ha_items: list[HAShoppingItem],
    ) -> SyncResult:
        """Merge union: items from both sides are preserved.

        1. Match existing items by normalized name
        2. Create mappings for matched items
        3. Add unmatched Alexa items to HA
        4. Add unmatched HA items to Alexa
        """
        result = SyncResult()
        matched_alexa_ids: set[str] = set()
        matched_ha_ids: set[str] = set()

        # Step 1: Match by normalized name
        for alexa_item in alexa_items:
            match = self._match_item_by_name(
                alexa_item.name,
                alexa_item.complete,
                ha_items,
                matched_ha_ids,
            )
            if match:
                assert isinstance(match, HAShoppingItem)
                self._add_mapping(
                    alexa_item.item_id,
                    match.item_id,
                    alexa_item.name,
                    ItemSource.ALEXA,
                )
                matched_alexa_ids.add(alexa_item.item_id)
                matched_ha_ids.add(match.item_id)

                # Sync completion status (alexa wins for matched items)
                if alexa_item.complete != match.complete and self._mirror_completed:
                    try:
                        await self._ha.async_mark_complete(
                            match.item_id, alexa_item.complete
                        )
                        result.alexa_to_ha_updates += 1
                    except Exception as err:
                        result.errors.append(f"Update HA item: {err}")

        # Step 2: Add unmatched Alexa items to HA
        if self._sync_mode in (SyncMode.TWO_WAY, SyncMode.ALEXA_TO_HA):
            for alexa_item in alexa_items:
                if alexa_item.item_id in matched_alexa_ids:
                    continue
                if not self._mirror_completed and alexa_item.complete:
                    continue
                try:
                    ha_item = await self._ha.async_add_item(
                        alexa_item.name, alexa_item.complete
                    )
                    if ha_item:
                        self._add_mapping(
                            alexa_item.item_id,
                            ha_item.item_id,
                            alexa_item.name,
                            ItemSource.ALEXA,
                        )
                        self.add_pending_op(
                            PendingOpType.ADD,
                            ItemSource.ALEXA,
                            alexa_item.name,
                            ha_item.item_id,
                        )
                        result.alexa_to_ha_adds += 1
                except Exception as err:
                    result.errors.append(f"Add to HA: {err}")

        # Step 3: Add unmatched HA items to Alexa
        if self._sync_mode in (SyncMode.TWO_WAY, SyncMode.HA_TO_ALEXA):
            for ha_item in ha_items:
                if ha_item.item_id in matched_ha_ids:
                    continue
                if not self._mirror_completed and ha_item.complete:
                    continue
                try:
                    alexa_item = await self._amazon.async_add_item(
                        ha_item.name, ha_item.complete
                    )
                    if alexa_item:
                        self._add_mapping(
                            alexa_item.item_id,
                            ha_item.item_id,
                            ha_item.name,
                            ItemSource.HA,
                        )
                        self.add_pending_op(
                            PendingOpType.ADD,
                            ItemSource.HA,
                            ha_item.name,
                            alexa_item.item_id,
                        )
                        result.ha_to_alexa_adds += 1
                except Exception as err:
                    result.errors.append(f"Add to Alexa: {err}")

        return result

    async def _async_initial_alexa_wins(
        self,
        alexa_items: list[AlexaShoppingItem],
        ha_items: list[HAShoppingItem],
    ) -> SyncResult:
        """Alexa wins: HA list is replaced with Alexa items."""
        result = SyncResult()

        # Remove all existing HA items
        for ha_item in ha_items:
            try:
                await self._ha.async_delete_item(ha_item.item_id)
                result.alexa_to_ha_deletes += 1
            except Exception as err:
                result.errors.append(f"Delete HA item: {err}")

        # Add all Alexa items to HA
        for alexa_item in alexa_items:
            if not self._mirror_completed and alexa_item.complete:
                continue
            try:
                ha_item = await self._ha.async_add_item(
                    alexa_item.name, alexa_item.complete
                )
                if ha_item:
                    self._add_mapping(
                        alexa_item.item_id,
                        ha_item.item_id,
                        alexa_item.name,
                        ItemSource.ALEXA,
                    )
                    result.alexa_to_ha_adds += 1
            except Exception as err:
                result.errors.append(f"Add to HA: {err}")

        return result

    async def _async_initial_ha_wins(
        self,
        alexa_items: list[AlexaShoppingItem],
        ha_items: list[HAShoppingItem],
    ) -> SyncResult:
        """HA wins: Alexa list is replaced with HA items."""
        result = SyncResult()

        # Delete all Alexa items
        for alexa_item in alexa_items:
            try:
                await self._amazon.async_delete_item(alexa_item.item_id)
                result.ha_to_alexa_deletes += 1
            except Exception as err:
                result.errors.append(f"Delete Alexa item: {err}")

        # Add all HA items to Alexa
        for ha_item in ha_items:
            if not self._mirror_completed and ha_item.complete:
                continue
            try:
                alexa_item = await self._amazon.async_add_item(
                    ha_item.name, ha_item.complete
                )
                if alexa_item:
                    self._add_mapping(
                        alexa_item.item_id,
                        ha_item.item_id,
                        ha_item.name,
                        ItemSource.HA,
                    )
                    result.ha_to_alexa_adds += 1
            except Exception as err:
                result.errors.append(f"Add to Alexa: {err}")

        return result

    async def async_sync_alexa_to_ha(
        self,
        alexa_items: list[AlexaShoppingItem],
    ) -> SyncResult:
        """Sync changes from Alexa to HA.

        Called when polling detects Alexa changes.
        """
        result = SyncResult()

        if not self._initial_sync_done:
            ha_items = await self._ha.async_get_items()
            result = await self.async_initial_sync(alexa_items, ha_items)
            self._previous_alexa_items = alexa_items
            self._previous_ha_items = ha_items
            return result

        if self._sync_mode == SyncMode.HA_TO_ALEXA:
            self._previous_alexa_items = alexa_items
            return result

        # Warm start: after HA restart _previous_alexa_items is empty but
        # initial sync already ran (state loaded from storage).
        # Items with an existing mapping are already known — skip them.
        # Items WITHOUT a mapping are genuinely new and must be synced.
        if not self._previous_alexa_items:
            unmapped = [
                item for item in alexa_items
                if not self._find_mapping_by_alexa_id(item.item_id)
            ]
            _LOGGER.debug(
                "Warm start: %d Alexa items total, %d unmapped (will sync)",
                len(alexa_items),
                len(unmapped),
            )
            self._previous_alexa_items = alexa_items
            if not unmapped:
                return result
            self._cleanup_expired_pending_ops()
            for item in unmapped:
                if self._is_echo(PendingOpType.ADD, item.name, item.item_id):
                    result.skipped_echo += 1
                    continue
                if not self._mirror_completed and item.complete:
                    continue
                try:
                    ha_item = await self._ha.async_add_item(item.name, item.complete)
                    if ha_item:
                        self._add_mapping(
                            item.item_id, ha_item.item_id, item.name, ItemSource.ALEXA
                        )
                        self.add_pending_op(
                            PendingOpType.ADD, ItemSource.ALEXA, item.name, ha_item.item_id
                        )
                        result.alexa_to_ha_adds += 1
                except Exception as err:
                    result.errors.append(f"Add to HA (warm start): {err}")
            return result

        diff = self._diff_alexa_snapshots(self._previous_alexa_items, alexa_items)
        self._cleanup_expired_pending_ops()

        # Handle added items
        for item in diff.added:
            if self._is_echo(PendingOpType.ADD, item.name, item.item_id):
                result.skipped_echo += 1
                # Still create mapping if not exists
                if not self._find_mapping_by_alexa_id(item.item_id):
                    # Find the HA item by name to create mapping
                    ha_items = await self._ha.async_get_items()
                    ha_match = self._match_item_by_name(
                        item.name, item.complete, ha_items
                    )
                    if ha_match:
                        self._add_mapping(
                            item.item_id,
                            ha_match.item_id,
                            item.name,
                            ItemSource.HA,
                        )
                continue

            if not self._mirror_completed and item.complete:
                continue

            try:
                ha_item = await self._ha.async_add_item(item.name, item.complete)
                if ha_item:
                    self._add_mapping(
                        item.item_id,
                        ha_item.item_id,
                        item.name,
                        ItemSource.ALEXA,
                    )
                    self.add_pending_op(
                        PendingOpType.ADD,
                        ItemSource.ALEXA,
                        item.name,
                        ha_item.item_id,
                    )
                    result.alexa_to_ha_adds += 1
            except Exception as err:
                result.errors.append(f"Add to HA: {err}")

        # Handle removed items
        for item in diff.removed:
            if self._is_echo(PendingOpType.DELETE, item.name, item.item_id):
                result.skipped_echo += 1
                self._remove_mapping_by_alexa_id(item.item_id)
                continue

            mapping = self._find_mapping_by_alexa_id(item.item_id)
            if mapping:
                try:
                    await self._ha.async_delete_item(mapping.ha_id)
                    self._remove_mapping_by_alexa_id(item.item_id)
                    self.add_pending_op(
                        PendingOpType.DELETE,
                        ItemSource.ALEXA,
                        item.name,
                        mapping.ha_id,
                    )
                    result.alexa_to_ha_deletes += 1
                except Exception as err:
                    result.errors.append(f"Delete from HA: {err}")

        # Handle modified items
        for old_item, new_item in diff.modified:
            if self._is_echo(PendingOpType.UPDATE, new_item.name, new_item.item_id):
                result.skipped_echo += 1
                continue
            if self._is_echo(
                PendingOpType.COMPLETE, new_item.name, new_item.item_id
            ):
                result.skipped_echo += 1
                continue

            mapping = self._find_mapping_by_alexa_id(new_item.item_id)
            if mapping:
                update_name = (
                    new_item.name if old_item.name != new_item.name else None
                )
                update_complete = (
                    new_item.complete
                    if old_item.complete != new_item.complete
                    else None
                )

                if not self._mirror_completed and update_complete is not None:
                    update_complete = None

                if update_name is not None or update_complete is not None:
                    try:
                        await self._ha.async_update_item(
                            mapping.ha_id,
                            name=update_name,
                            complete=update_complete,
                        )
                        op_type = (
                            PendingOpType.COMPLETE
                            if update_complete is not None and update_name is None
                            else PendingOpType.UPDATE
                        )
                        self.add_pending_op(
                            op_type,
                            ItemSource.ALEXA,
                            new_item.name,
                            mapping.ha_id,
                        )
                        if update_name:
                            mapping.name = new_item.name
                        mapping.last_synced = str(time.time())
                        result.alexa_to_ha_updates += 1
                    except Exception as err:
                        result.errors.append(f"Update HA item: {err}")

        self._previous_alexa_items = alexa_items
        return result

    async def async_sync_ha_to_alexa(
        self,
        ha_items: list[HAShoppingItem],
    ) -> SyncResult:
        """Sync changes from HA to Alexa.

        Called when HA shopping_list_updated event fires.
        """
        result = SyncResult()

        if not self._initial_sync_done:
            # Defer to next poll cycle which handles initial sync
            self._previous_ha_items = ha_items
            return result

        if self._sync_mode == SyncMode.ALEXA_TO_HA:
            self._previous_ha_items = ha_items
            return result

        # Warm start: same as Alexa side — items with a mapping are known,
        # items WITHOUT a mapping are genuinely new and must be synced.
        if not self._previous_ha_items:
            unmapped = [
                item for item in ha_items
                if not self._find_mapping_by_ha_id(item.item_id)
            ]
            _LOGGER.debug(
                "Warm start: %d HA items total, %d unmapped (will sync)",
                len(ha_items),
                len(unmapped),
            )
            self._previous_ha_items = ha_items
            if not unmapped:
                return result
            self._cleanup_expired_pending_ops()
            for item in unmapped:
                if self._is_echo(PendingOpType.ADD, item.name, item.item_id):
                    result.skipped_echo += 1
                    continue
                if not self._mirror_completed and item.complete:
                    continue
                try:
                    alexa_item = await self._amazon.async_add_item(item.name, item.complete)
                    if alexa_item:
                        self._add_mapping(
                            alexa_item.item_id, item.item_id, item.name, ItemSource.HA
                        )
                        self.add_pending_op(
                            PendingOpType.ADD, ItemSource.HA, item.name, alexa_item.item_id
                        )
                        result.ha_to_alexa_adds += 1
                except Exception as err:
                    result.errors.append(f"Add to Alexa (warm start): {err}")
            return result

        diff = self._diff_ha_snapshots(self._previous_ha_items, ha_items)
        self._cleanup_expired_pending_ops()

        # Handle added items
        for item in diff.added:
            if self._is_echo(PendingOpType.ADD, item.name, item.item_id):
                result.skipped_echo += 1
                continue

            if not self._mirror_completed and item.complete:
                continue

            try:
                alexa_item = await self._amazon.async_add_item(
                    item.name, item.complete
                )
                if alexa_item:
                    self._add_mapping(
                        alexa_item.item_id,
                        item.item_id,
                        item.name,
                        ItemSource.HA,
                    )
                    self.add_pending_op(
                        PendingOpType.ADD,
                        ItemSource.HA,
                        item.name,
                        alexa_item.item_id,
                    )
                    result.ha_to_alexa_adds += 1
            except Exception as err:
                result.errors.append(f"Add to Alexa: {err}")

        # Handle removed items
        for item in diff.removed:
            if self._is_echo(PendingOpType.DELETE, item.name, item.item_id):
                result.skipped_echo += 1
                self._remove_mapping_by_ha_id(item.item_id)
                continue

            mapping = self._find_mapping_by_ha_id(item.item_id)
            if mapping:
                try:
                    success = await self._amazon.async_delete_item(mapping.alexa_id)
                    if success:
                        self._remove_mapping_by_ha_id(item.item_id)
                        self.add_pending_op(
                            PendingOpType.DELETE,
                            ItemSource.HA,
                            item.name,
                            mapping.alexa_id,
                        )
                        result.ha_to_alexa_deletes += 1
                except Exception as err:
                    result.errors.append(f"Delete from Alexa: {err}")

        # Handle modified items
        for old_item, new_item in diff.modified:
            if self._is_echo(PendingOpType.UPDATE, new_item.name, new_item.item_id):
                result.skipped_echo += 1
                continue
            if self._is_echo(
                PendingOpType.COMPLETE, new_item.name, new_item.item_id
            ):
                result.skipped_echo += 1
                continue

            mapping = self._find_mapping_by_ha_id(new_item.item_id)
            if mapping:
                update_name = (
                    new_item.name if old_item.name != new_item.name else None
                )
                update_complete = (
                    new_item.complete
                    if old_item.complete != new_item.complete
                    else None
                )

                if not self._mirror_completed and update_complete is not None:
                    update_complete = None

                if update_name is not None or update_complete is not None:
                    try:
                        await self._amazon.async_update_item(
                            mapping.alexa_id,
                            summary=update_name,
                            complete=update_complete,
                        )
                        op_type = (
                            PendingOpType.COMPLETE
                            if update_complete is not None and update_name is None
                            else PendingOpType.UPDATE
                        )
                        self.add_pending_op(
                            op_type,
                            ItemSource.HA,
                            new_item.name,
                            mapping.alexa_id,
                        )
                        if update_name:
                            mapping.name = new_item.name
                        mapping.last_synced = str(time.time())
                        result.ha_to_alexa_updates += 1
                    except Exception as err:
                        result.errors.append(f"Update Alexa item: {err}")

        self._previous_ha_items = ha_items
        return result

    async def async_full_resync(self) -> SyncResult:
        """Clear mappings and perform a complete resync.

        Fetches both lists FIRST before clearing state so that a failed
        API call does not leave the state file empty (which would trigger
        a broken initial sync on next reload).
        """
        _LOGGER.info("Performing full resync")

        # Fetch both sides before touching state — if either fails we keep
        # the existing state intact and the exception propagates.
        alexa_items = await self._amazon.async_get_snapshot()
        ha_items = await self._ha.async_get_items()

        _LOGGER.debug(
            "Full resync: %d Alexa items, %d HA items",
            len(alexa_items),
            len(ha_items),
        )

        # Safe to clear now — both fetches succeeded.
        await self.async_clear_state()

        result = await self.async_initial_sync(alexa_items, ha_items)

        self._previous_alexa_items = alexa_items
        self._previous_ha_items = ha_items

        await self.async_save_state()

        _LOGGER.info(
            "Full resync complete: +%d HA, +%d Alexa, %d mappings",
            result.alexa_to_ha_adds,
            result.ha_to_alexa_adds,
            len(self._state.mappings),
        )
        return result

"""Data update coordinator for Alexa Shopping List Sync."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .amazon_client import AmazonShoppingClient
from .auth import AuthManager
from .const import (
    CONF_AMAZON_DOMAIN,
    CONF_DEBUG_MODE,
    CONF_EMAIL,
    CONF_INITIAL_SYNC_MODE,
    CONF_MIRROR_COMPLETED,
    CONF_OTP_SECRET,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_PRESERVE_DUPLICATES,
    CONF_SYNC_MODE,
    CONF_TARGET_LIST,
    DEFAULT_AMAZON_DOMAIN,
    DEFAULT_MIRROR_COMPLETED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PRESERVE_DUPLICATES,
    DOMAIN,
    MIN_POLL_INTERVAL,
    TARGET_SHOPPING_LIST,
    InitialSyncMode,
    SyncMode,
)
from .exceptions import SessionExpiredError, ThrottledError
from .ha_list_bridge import HAListBridge, compute_snapshot_hash
from .shopping_list_bridge import ShoppingListBridge
from .sync_engine import SyncEngine, SyncResult
from .todo_list_bridge import TodoListBridge

_LOGGER = logging.getLogger(__name__)


class AlexaShoppingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Alexa Shopping List Sync.

    Manages:
    - Polling Alexa for changes
    - Listening to HA shopping_list_updated events
    - Queuing HA->Alexa mutations
    - Auth state and reauth triggers
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self._entry = entry
        self._auth_manager: AuthManager | None = None
        self._amazon_client: AmazonShoppingClient | None = None
        self._ha_bridge: HAListBridge | None = None
        self._sync_engine: SyncEngine | None = None
        self._target_list: str = entry.data.get(CONF_TARGET_LIST, TARGET_SHOPPING_LIST)
        self._event_unsub: CALLBACK_TYPE | None = None
        self._mutation_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._mutation_task: asyncio.Task[None] | None = None
        self._sync_lock = asyncio.Lock()
        self._consecutive_errors = 0
        self._last_error: str = ""
        self._last_success: str = ""
        self._connected = False
        self._alexa_item_count = 0
        self._ha_item_count = 0
        self._silent_refresh_tried = False
        self._sync_enabled = entry.data.get("_sync_enabled", True)
        self._force_once = False

        poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        poll_interval = max(poll_interval, MIN_POLL_INTERVAL)
        # Add jitter: ±10%
        jitter = poll_interval * 0.1
        effective_interval = poll_interval + random.uniform(-jitter, jitter)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=effective_interval),
        )

    @property
    def target_list(self) -> str:
        """Return the configured target list identifier."""
        return self._target_list

    @property
    def sync_enabled(self) -> bool:
        """Return whether sync is enabled."""
        return self._sync_enabled

    @sync_enabled.setter
    def sync_enabled(self, value: bool) -> None:
        """Enable or disable sync."""
        self._sync_enabled = value
        _LOGGER.info("Sync %s", "enabled" if value else "disabled")

    @property
    def connected(self) -> bool:
        """Return whether we're connected to Amazon."""
        return self._connected

    @property
    def last_error(self) -> str:
        """Return last error message."""
        return self._last_error

    @property
    def last_success(self) -> str:
        """Return last success timestamp."""
        return self._last_success

    @property
    def pending_operations_count(self) -> int:
        """Return number of pending operations."""
        if self._sync_engine:
            return len(self._sync_engine.state.pending_ops)
        return 0

    @property
    def alexa_item_count(self) -> int:
        """Return number of Alexa items."""
        return self._alexa_item_count

    @property
    def ha_item_count(self) -> int:
        """Return number of HA items."""
        return self._ha_item_count

    @property
    def sync_engine(self) -> SyncEngine | None:
        """Return sync engine."""
        return self._sync_engine

    @property
    def auth_manager(self) -> AuthManager | None:
        """Return auth manager."""
        return self._auth_manager

    async def async_initialize(self) -> None:
        """Initialize all components."""
        data = self._entry.data
        options = self._entry.options

        # Auth manager
        self._auth_manager = AuthManager(
            hass=self.hass,
            amazon_domain=data.get(CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN),
            email=data[CONF_EMAIL],
            password=data[CONF_PASSWORD],
            otp_secret=data[CONF_OTP_SECRET],
        )

        # Amazon client
        self._amazon_client = AmazonShoppingClient(self._auth_manager)

        # HA bridge — shopping list or todo entity
        if self._target_list == TARGET_SHOPPING_LIST:
            self._ha_bridge = ShoppingListBridge(self.hass)
        else:
            self._ha_bridge = TodoListBridge(self.hass, self._target_list)

        # Sync engine
        sync_mode = SyncMode(options.get(CONF_SYNC_MODE, SyncMode.TWO_WAY))
        initial_sync_mode = InitialSyncMode(
            options.get(CONF_INITIAL_SYNC_MODE, InitialSyncMode.MERGE_UNION)
        )

        self._sync_engine = SyncEngine(
            hass=self.hass,
            amazon_client=self._amazon_client,
            ha_bridge=self._ha_bridge,
            sync_mode=sync_mode,
            initial_sync_mode=initial_sync_mode,
            preserve_duplicates=options.get(CONF_PRESERVE_DUPLICATES, DEFAULT_PRESERVE_DUPLICATES),
            mirror_completed=options.get(CONF_MIRROR_COMPLETED, DEFAULT_MIRROR_COMPLETED),
        )

        # Load persisted state
        await self._sync_engine.async_load_state()

        # If target list was changed via options flow, clear all mappings
        # so the next poll triggers a fresh initial sync with the new list.
        if data.get("_target_list_changed"):
            _LOGGER.warning(
                "Target list changed to %s — clearing mappings for full resync",
                self._target_list,
            )
            await self._sync_engine.async_clear_state()
            # Remove the flag so we don't clear again on next reload
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={k: v for k, v in data.items() if k != "_target_list_changed"},
            )

        # Create session (auth will happen via proxy in config flow)
        await self._auth_manager.async_create_session()

        # Restore device credentials for silent token exchange
        refresh_token = data.get("_refresh_token", "")
        device_serial = data.get("_device_serial", "")
        if refresh_token and device_serial:
            self._auth_manager.set_device_credentials(refresh_token, device_serial)
            _LOGGER.debug("Restored device credentials for silent session renewal")

        # Restore session cookies captured during config flow proxy login
        saved_cookies: dict[str, str] = data.get("_cookies", {})
        if saved_cookies:
            self._auth_manager.mark_authenticated(saved_cookies)
            _LOGGER.debug("Restored %d session cookies from config entry", len(saved_cookies))
        elif self._sync_engine.state.shopping_list_id:
            # Fallback: if we have a known shopping list ID from previous runs,
            # optimistically mark authenticated; first poll will verify.
            self._auth_manager.mark_authenticated()

    @callback
    def async_start_event_listener(self) -> None:
        """Start listening for HA list change events."""
        if self._event_unsub is not None:
            return

        @callback
        def _on_list_changed(event: Event) -> None:
            """Handle list change event (shopping_list_updated or state_changed)."""
            self._mutation_queue.put_nowait({"event": "list_changed"})
            if self._mutation_task is None or self._mutation_task.done():
                self._mutation_task = self.hass.async_create_task(
                    self._async_process_mutation_queue()
                )

        if self._target_list == TARGET_SHOPPING_LIST:
            self._event_unsub = self.hass.bus.async_listen(
                "shopping_list_updated", _on_list_changed
            )
            _LOGGER.debug("Started listening for shopping_list_updated events")
        else:
            self._event_unsub = async_track_state_change_event(
                self.hass, [self._target_list], _on_list_changed
            )
            _LOGGER.debug("Started listening for state_changed on %s", self._target_list)

    @callback
    def async_stop_event_listener(self) -> None:
        """Stop listening for events."""
        if self._event_unsub:
            self._event_unsub()
            self._event_unsub = None

        if self._mutation_task and not self._mutation_task.done():
            self._mutation_task.cancel()
            self._mutation_task = None

    async def _async_process_mutation_queue(self) -> None:
        """Process HA->Alexa mutations from the queue.

        Decision: We debounce mutations by processing the queue
        with a short delay, so rapid successive events (e.g. bulk
        edits) are batched into one sync cycle.
        """
        # Small delay to debounce rapid events
        await asyncio.sleep(1.0)

        # Drain the queue
        events: list[dict[str, Any]] = []
        while not self._mutation_queue.empty():
            try:
                events.append(self._mutation_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not events or not self._sync_engine or not self._ha_bridge:
            return

        if not self._sync_enabled:
            _LOGGER.debug("Skipping HA->Alexa sync: sync disabled")
            return

        if not self._auth_manager or not self._auth_manager.authenticated:
            _LOGGER.debug("Skipping HA->Alexa sync: not authenticated")
            return

        try:
            async with self._sync_lock:
                ha_items = await self._ha_bridge.async_get_items()
                self._ha_item_count = len(ha_items)
                result = await self._sync_engine.async_sync_ha_to_alexa(ha_items)
                await self._sync_engine.async_save_state()

                # Update alexa count after mutations so sensors reflect
                # the new state immediately instead of waiting for next poll.
                self._alexa_item_count += result.ha_to_alexa_adds
                self._alexa_item_count -= result.ha_to_alexa_deletes

            if result.errors:
                _LOGGER.warning(
                    "HA->Alexa sync had %d errors: %s",
                    len(result.errors),
                    result.errors[:3],
                )

            _LOGGER.debug(
                "HA->Alexa sync: +%d ~%d -%d (echo=%d)",
                result.ha_to_alexa_adds,
                result.ha_to_alexa_updates,
                result.ha_to_alexa_deletes,
                result.skipped_echo,
            )

            # Notify entities so sensors update without waiting for next poll
            self.async_set_updated_data(self.data or {})
        except SessionExpiredError:
            if not await self._async_try_silent_refresh():
                self._trigger_reauth()
        except Exception as err:
            _LOGGER.error("HA->Alexa sync failed: %s", err, exc_info=True)

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll Alexa and sync changes to HA.

        This is called by DataUpdateCoordinator on each poll interval.
        The sync lock prevents concurrent execution with mutation queue
        processing or full_resync, which could cause duplicate items.
        """
        async with self._sync_lock:
            force_once = self._force_once
            self._force_once = False

            if not self._sync_enabled and not force_once:
                return {
                    "alexa_items": self._alexa_item_count,
                    "ha_items": self._ha_item_count,
                    "last_sync": self._last_success,
                    "connected": self._connected,
                    "sync_enabled": False,
                }

            if not self._auth_manager or not self._amazon_client or not self._sync_engine:
                raise UpdateFailed("Integration not fully initialized")

            if not self._auth_manager.authenticated:
                self._connected = False
                # Try silent refresh once when session is lost between polls.
                # The SessionExpiredError handler covers the initial 401;
                # this covers every subsequent poll while authenticated=False.
                if not self._silent_refresh_tried:
                    self._silent_refresh_tried = True
                    _LOGGER.info(
                        "Session not authenticated, attempting silent re-auth before reauth flow"
                    )
                    if await self._async_try_silent_refresh():
                        _LOGGER.warning("Silent re-auth succeeded, continuing update")
                        # fall through to the fetch loop below
                    else:
                        _LOGGER.warning("Silent re-auth failed, triggering manual reauth")
                        self._last_error = "Session expired — re-authentication required"
                        self._trigger_reauth()
                        raise UpdateFailed("Not authenticated - reauth required")
                else:
                    self._trigger_reauth()
                    raise UpdateFailed("Not authenticated - reauth required")

            # Allow one silent re-auth retry within the same update cycle.
            # Without this, a successful silent refresh would still raise
            # UpdateFailed, causing an infinite retry loop.
            for _attempt in range(2):
                try:
                    alexa_items = await self._amazon_client.async_get_snapshot()
                    self._alexa_item_count = len(alexa_items)

                    result = await self._sync_engine.async_sync_alexa_to_ha(alexa_items)

                    if self._ha_bridge:
                        try:
                            ha_items = await self._ha_bridge.async_get_items()
                            self._ha_item_count = len(ha_items)

                            # Hybrid polling for todo targets: state_changed
                            # doesn't fire on renames, so also check hash on
                            # each poll cycle and sync if changed.
                            if self._target_list != TARGET_SHOPPING_LIST:
                                ha_hash = compute_snapshot_hash(ha_items)
                                if (
                                    self._sync_engine.state.last_ha_snapshot_hash
                                    and ha_hash != self._sync_engine.state.last_ha_snapshot_hash
                                ):
                                    ha_result = await self._sync_engine.async_sync_ha_to_alexa(
                                        ha_items
                                    )
                                    _LOGGER.debug(
                                        "Todo poll HA->Alexa: +%d ~%d -%d",
                                        ha_result.ha_to_alexa_adds,
                                        ha_result.ha_to_alexa_updates,
                                        ha_result.ha_to_alexa_deletes,
                                    )
                                self._sync_engine.state.last_ha_snapshot_hash = ha_hash
                        except Exception:
                            pass

                    self._sync_engine.state.last_alexa_snapshot_hash = (
                        self._amazon_client.compute_snapshot_hash(alexa_items)
                    )
                    if self._amazon_client.shopping_list_id:
                        self._sync_engine.state.shopping_list_id = (
                            self._amazon_client.shopping_list_id
                        )

                    await self._sync_engine.async_save_state()

                    self._connected = True
                    self._consecutive_errors = 0
                    self._silent_refresh_tried = False
                    self._last_success = str(time.time())
                    self._last_error = ""

                    if result.errors:
                        _LOGGER.warning(
                            "Alexa->HA sync had %d errors: %s",
                            len(result.errors),
                            result.errors[:3],
                        )
                        self._last_error = "; ".join(result.errors[:3])

                    _LOGGER.debug(
                        "Poll complete: Alexa->HA +%d ~%d -%d (echo=%d, items=%d)",
                        result.alexa_to_ha_adds,
                        result.alexa_to_ha_updates,
                        result.alexa_to_ha_deletes,
                        result.skipped_echo,
                        self._alexa_item_count,
                    )

                    return {
                        "alexa_items": self._alexa_item_count,
                        "ha_items": self._ha_item_count,
                        "last_sync": self._last_success,
                        "connected": True,
                    }

                except SessionExpiredError:
                    self._connected = False
                    if _attempt == 0 and await self._async_try_silent_refresh():
                        _LOGGER.info("Silent re-auth succeeded, retrying update immediately")
                        continue  # retry the fetch with fresh cookies
                    self._last_error = "Session expired — re-authentication required"
                    self._trigger_reauth()
                    raise UpdateFailed("Session expired - reauth required")

                except ThrottledError as err:
                    self._consecutive_errors += 1
                    self._last_error = str(err)
                    if self._consecutive_errors >= 3:
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            "rate_limited",
                            is_fixable=False,
                            severity=ir.IssueSeverity.WARNING,
                            translation_key="rate_limited",
                        )
                    raise UpdateFailed(f"Rate limited: {err}")

                except Exception as err:
                    self._connected = False
                    self._consecutive_errors += 1
                    self._last_error = str(err)

                    if self._consecutive_errors >= 5:
                        _LOGGER.error(
                            "Too many consecutive errors (%d), may need reauth",
                            self._consecutive_errors,
                        )
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            "repeated_auth_failure",
                            is_fixable=False,
                            severity=ir.IssueSeverity.ERROR,
                            translation_key="repeated_auth_failure",
                        )

                    raise UpdateFailed(f"Update failed: {err}")

    async def _async_try_silent_refresh(self) -> bool:
        """Try silent session refresh.

        Primary path: token exchange via refresh_token obtained during device
        registration.  This works reliably from server-side code (no metadata1
        browser fingerprint needed).

        Fallback path: headless form-fill login — may fail if metadata1 is
        absent, but kept as a last resort for installations that pre-date device
        registration.

        Persists new cookies to the config entry on success so they survive HA
        restarts.  Always returns False on any exception — must never raise,
        because it is called from inside an except-handler.
        """
        if not self._auth_manager:
            return False

        try:
            # Primary: token exchange (no metadata1 required)
            if self._auth_manager.has_refresh_token:
                _LOGGER.debug("Attempting silent refresh via token exchange")
                if await self._auth_manager.async_try_token_exchange():
                    new_cookies = self._auth_manager.extract_cookies_dict()
                    if new_cookies:
                        self.hass.config_entries.async_update_entry(
                            self._entry,
                            data={**self._entry.data, "_cookies": new_cookies},
                        )
                        _LOGGER.debug(
                            "Persisted %d new cookies after token exchange",
                            len(new_cookies),
                        )
                    return True
                _LOGGER.warning("Token exchange failed, falling back to programmatic login")

            # Fallback: programmatic form-fill login
            success = await self._auth_manager.async_try_silent_relogin()
            if not success:
                return False

            new_cookies = self._auth_manager.extract_cookies_dict()
            if new_cookies:
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, "_cookies": new_cookies},
                )
                _LOGGER.debug(
                    "Persisted %d new cookies after programmatic re-auth",
                    len(new_cookies),
                )

            return True

        except Exception as err:
            _LOGGER.error("Unexpected error during silent session refresh: %s", err)
            return False

    def _trigger_reauth(self) -> None:
        """Trigger reauth flow."""
        _LOGGER.warning("Triggering re-authentication")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            "reauth_needed",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="reauth_needed",
        )
        self._entry.async_start_reauth(self.hass)

    async def async_register_services(self) -> None:
        """Register integration services."""
        from .services import async_register_services

        await async_register_services(self.hass, self)

    async def async_force_refresh(self) -> None:
        """Force an immediate refresh cycle, even when sync is paused."""
        self._force_once = True
        await self.async_request_refresh()

    async def async_full_resync(self) -> SyncResult | None:
        """Perform a full resync."""
        if not self._auth_manager or not self._auth_manager.authenticated:
            raise HomeAssistantError(
                "Not authenticated with Amazon. Please complete re-authentication first."
            )
        if self._sync_engine:
            async with self._sync_lock:
                result = await self._sync_engine.async_full_resync()
            await self.async_request_refresh()
            return result
        return None

    async def async_clear_local_mapping(self) -> None:
        """Clear local mapping store."""
        if self._sync_engine:
            await self._sync_engine.async_clear_state()

    def get_diagnostics_data(self) -> dict[str, Any]:
        """Get diagnostics data (no secrets)."""
        data: dict[str, Any] = {
            "connected": self._connected,
            "last_success": self._last_success,
            "last_error": self._last_error,
            "consecutive_errors": self._consecutive_errors,
            "alexa_items": self._alexa_item_count,
            "ha_items": self._ha_item_count,
            "config": {
                "amazon_domain": self._entry.data.get(CONF_AMAZON_DOMAIN),
                "email": "***REDACTED***",
                "sync_mode": self._entry.options.get(CONF_SYNC_MODE),
                "poll_interval": self._entry.options.get(CONF_POLL_INTERVAL),
                "preserve_duplicates": self._entry.options.get(CONF_PRESERVE_DUPLICATES),
                "mirror_completed": self._entry.options.get(CONF_MIRROR_COMPLETED),
                "debug_mode": self._entry.options.get(CONF_DEBUG_MODE),
            },
        }

        if self._sync_engine:
            state = self._sync_engine.state
            data["sync_state"] = {
                "mappings_count": len(state.mappings),
                "pending_ops_count": len(state.pending_ops),
                "shopping_list_id": state.shopping_list_id or "not discovered",
                "last_alexa_hash": state.last_alexa_snapshot_hash,
                "last_successful_sync": state.last_successful_sync,
                "version": state.version,
            }

        return data

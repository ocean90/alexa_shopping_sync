"""Alexa Shopping List Sync - Bidirectional sync between Alexa and HA shopping lists."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_TARGET_LIST, DOMAIN, TARGET_SHOPPING_LIST
from .coordinator import AlexaShoppingCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

AlexaShoppingConfigEntry = ConfigEntry[AlexaShoppingCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AlexaShoppingConfigEntry) -> bool:
    """Set up Alexa Shopping List Sync from a config entry."""
    # Validate the target list is available
    target_list = entry.data.get(CONF_TARGET_LIST, TARGET_SHOPPING_LIST)
    if target_list == TARGET_SHOPPING_LIST:
        if "shopping_list" not in hass.config.components:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "shopping_list_missing",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="shopping_list_missing",
            )
            _LOGGER.error(
                "Shopping List integration not found. "
                "Please add it before setting up Alexa Shopping List Sync"
            )
            return False
    else:
        state = hass.states.get(target_list)
        if state is None:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "target_list_missing",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="target_list_missing",
            )
            _LOGGER.error(
                "Target todo entity %s not found. "
                "Please ensure the integration providing it is configured",
                target_list,
            )
            return False

    coordinator = AlexaShoppingCoordinator(hass, entry)
    await coordinator.async_initialize()

    # Fetch data before setting up platforms so that a failed first refresh
    # (ConfigEntryNotReady) leaves no platform entities behind, avoiding the
    # "already been setup" error on the subsequent retry.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    await coordinator.async_register_services()

    # Start listening for HA shopping list events
    coordinator.async_start_event_listener()

    return True


async def _async_options_updated(hass: HomeAssistant, entry: AlexaShoppingConfigEntry) -> None:
    """Handle options update — reload entry so new poll interval takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AlexaShoppingConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: AlexaShoppingCoordinator = entry.runtime_data
    coordinator.async_stop_event_listener()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version < 2:
        # v1 → v2: add target_list (existing installs always used shopping_list)
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_TARGET_LIST: TARGET_SHOPPING_LIST},
            version=2,
        )
        _LOGGER.info("Migrated config entry to version 2 (added target_list)")

    return True

"""Switch entity for Alexa Shopping List Sync."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AlexaShoppingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: AlexaShoppingCoordinator = entry.runtime_data
    async_add_entities([AlexaSyncEnabledSwitch(coordinator, entry)])


class AlexaSyncEnabledSwitch(CoordinatorEntity[AlexaShoppingCoordinator], SwitchEntity):
    """Switch to enable/disable sync."""

    _attr_has_entity_name = True
    _attr_translation_key = "sync_enabled"
    _attr_icon = "mdi:sync"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: AlexaShoppingCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_sync_enabled"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Alexa Shopping Sync ({entry.title})",
            "manufacturer": "Amazon",
            "model": "Alexa Shopping List",
        }

    @property
    def is_on(self) -> bool:
        """Return true if sync is enabled."""
        return self.coordinator.sync_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable sync."""
        self.coordinator.sync_enabled = True
        self._persist_state(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable sync."""
        self.coordinator.sync_enabled = False
        self._persist_state(False)
        self.async_write_ha_state()

    def _persist_state(self, enabled: bool) -> None:
        """Persist sync_enabled to config entry data so it survives restarts."""
        self.hass.config_entries.async_update_entry(
            self.coordinator.config_entry,
            data={**self.coordinator.config_entry.data, "_sync_enabled": enabled},
        )

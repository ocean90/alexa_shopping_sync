"""Binary sensor entities for Alexa Shopping List Sync."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AlexaShoppingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities."""
    coordinator: AlexaShoppingCoordinator = entry.runtime_data

    async_add_entities([
        AlexaShoppingConnectedSensor(coordinator, entry),
    ])


class AlexaShoppingConnectedSensor(
    CoordinatorEntity[AlexaShoppingCoordinator], BinarySensorEntity
):
    """Binary sensor for connection status."""

    _attr_has_entity_name = True
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: AlexaShoppingCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connected"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Alexa Shopping Sync ({entry.title})",
            "manufacturer": "Amazon",
            "model": "Alexa Shopping List",
        }

    @property
    def is_on(self) -> bool:
        """Return true if connected."""
        return self.coordinator.connected

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

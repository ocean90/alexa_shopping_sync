"""Button entity for Alexa Shopping List Sync."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up button entities."""
    coordinator: AlexaShoppingCoordinator = entry.runtime_data
    async_add_entities([AlexaSyncNowButton(coordinator, entry)])


class AlexaSyncNowButton(CoordinatorEntity[AlexaShoppingCoordinator], ButtonEntity):
    """Button to trigger a manual sync."""

    _attr_has_entity_name = True
    _attr_translation_key = "sync_now"
    _attr_icon = "mdi:sync"

    def __init__(
        self,
        coordinator: AlexaShoppingCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_sync_now"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Alexa Shopping Sync ({entry.title})",
            "manufacturer": "Amazon",
            "model": "Alexa Shopping List",
        }

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_force_refresh()
